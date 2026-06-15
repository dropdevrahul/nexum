"""
store.py — Nexum plugin foundation.

Single source of truth for:
- Data-directory resolution (nexum_data_dir)
- SQLite connection + schema (db)
- Shared config (get_config)
- Utility helpers: sha256, estimate_tokens
- Dedup / memo helpers: seen_output, record_output, memo_get, memo_put
- Session KV (flags + task): get_flag, set_flag, get_session_task, set_session_task
- Metrics: add_usage, usage_rows

CLI:
  python3 store.py init    — create the database and schema
  python3 store.py config  — print effective config as JSON
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional, List, Dict, Any

# ---------------------------------------------------------------------------
# Pricing: USD per 1M tokens — (input_price, output_price)
# Cache read ≈ 0.1× input; cache write ≈ 1.25× input.
# ---------------------------------------------------------------------------
PRICING: Dict[str, tuple] = {
    "opus":   (5.0,  25.0),
    "sonnet": (3.0,  15.0),
    "haiku":  (1.0,   5.0),
}

# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------
_CONFIG_DEFAULTS: Dict[str, Any] = {
    "truncate_max_lines": 200,
    "truncate_head_lines": 120,
    "truncate_tail_lines": 60,
    "truncate_min_lines_to_act": 240,
    "keep_error_regex": "(?i)(error|exception|traceback|failed|fatal|warning)",
    "compaction_threshold_tokens": 120000,
    "scan_guard_enabled": True,
    "scan_deny_paths": [
        "node_modules", ".git", "dist", "build", "target", "vendor",
        ".next", "coverage", ".venv", "__pycache__",
    ],
    "intent_guard_enabled": True,
    "intent_similarity_threshold": 0.25,
    "statusline_compaction_warn_pct": 80,
}

# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------
_DDL = [
    """
    CREATE TABLE IF NOT EXISTS outputs(
        session_id   TEXT,
        content_hash TEXT,
        tool_name    TEXT,
        summary      TEXT,
        token_count  INTEGER,
        ts           REAL,
        PRIMARY KEY(session_id, content_hash)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS memo(
        input_hash  TEXT PRIMARY KEY,
        output_text TEXT,
        ts          REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_kv(
        session_id TEXT,
        key        TEXT,
        value      TEXT,
        PRIMARY KEY(session_id, key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS usage(
        session_id    TEXT,
        model         TEXT,
        input_tok     INTEGER,
        output_tok    INTEGER,
        cache_read_tok INTEGER,
        ts            REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS savings(
        session_id TEXT,
        source     TEXT,
        saved_tok  INTEGER,
        ts         REAL
    )
    """,
]


# ---------------------------------------------------------------------------
# nexum_data_dir
# ---------------------------------------------------------------------------

def nexum_data_dir() -> str:
    """Resolve and create the nexum data directory.

    Priority:
    1. $CLAUDE_PLUGIN_DATA
    2. ${CLAUDE_PLUGIN_ROOT}/.nexum-data
    3. ./.nexum-data
    """
    env_data = os.environ.get("CLAUDE_PLUGIN_DATA", "").strip()
    if env_data:
        path = Path(env_data)
    else:
        plugin_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip()
        if plugin_root:
            path = Path(plugin_root) / ".nexum-data"
        else:
            path = Path(".nexum-data")

    path.mkdir(parents=True, exist_ok=True)
    return str(path)


# ---------------------------------------------------------------------------
# db — SQLite connection
# ---------------------------------------------------------------------------

def _apply_schema(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist."""
    with conn:
        for ddl in _DDL:
            conn.execute(ddl)


def _open_db(db_path: str) -> sqlite3.Connection:
    """Open the SQLite file, enable WAL, apply schema."""
    conn = sqlite3.connect(db_path, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _apply_schema(conn)
    return conn


def db() -> sqlite3.Connection:
    """Open nexum.db (WAL mode). Retry once on OperationalError.

    Falls back to an in-memory connection so callers never raise.
    """
    data_dir = nexum_data_dir()
    db_path = os.path.join(data_dir, "nexum.db")

    for attempt in range(2):
        try:
            return _open_db(db_path)
        except sqlite3.OperationalError:
            if attempt == 0:
                time.sleep(0.1)
                continue
            break
        except Exception:
            break

    # Fall back to in-memory so callers never raise
    try:
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.row_factory = sqlite3.Row
        _apply_schema(conn)
        return conn
    except Exception:
        # Absolute last resort — return a bare in-memory connection
        return sqlite3.connect(":memory:", check_same_thread=False)


# ---------------------------------------------------------------------------
# get_config
# ---------------------------------------------------------------------------

def get_config() -> Dict[str, Any]:
    """Return defaults merged with config.json (file values win)."""
    cfg = dict(_CONFIG_DEFAULTS)
    try:
        config_path = os.path.join(nexum_data_dir(), "config.json")
        if os.path.isfile(config_path):
            with open(config_path, "r", encoding="utf-8") as fh:
                overrides = json.load(fh)
            cfg.update(overrides)
    except Exception:
        pass
    return cfg


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def sha256(text: str) -> str:
    """Return hex SHA-256 of *text* (UTF-8 encoded)."""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def estimate_tokens(text: str) -> int:
    """Cheap heuristic token count: max(1, len(text) // 4)."""
    return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# Dedup / memo helpers
# ---------------------------------------------------------------------------

def seen_output(session_id: str, content_hash: str) -> Optional[Dict[str, Any]]:
    """Return the outputs row dict for (session_id, content_hash), or None."""
    try:
        conn = db()
        row = conn.execute(
            "SELECT session_id, content_hash, tool_name, summary, token_count, ts "
            "FROM outputs WHERE session_id=? AND content_hash=?",
            (session_id, content_hash),
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return dict(row)
    except Exception:
        return None


def record_output(
    session_id: str,
    tool_name: str,
    content_hash: str,
    summary: str,
    token_count: int,
) -> None:
    """Insert or replace a row in outputs."""
    try:
        conn = db()
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO outputs"
                "(session_id, content_hash, tool_name, summary, token_count, ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, content_hash, tool_name, summary, token_count, time.time()),
            )
        conn.close()
    except Exception:
        pass


def memo_get(input_hash: str) -> Optional[str]:
    """Return the cached output_text for input_hash, or None."""
    try:
        conn = db()
        row = conn.execute(
            "SELECT output_text FROM memo WHERE input_hash=?",
            (input_hash,),
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return row[0]
    except Exception:
        return None


def memo_put(input_hash: str, output_text: str) -> None:
    """Insert or replace a memo entry."""
    try:
        conn = db()
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO memo(input_hash, output_text, ts) VALUES (?, ?, ?)",
                (input_hash, output_text, time.time()),
            )
        conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Session KV (flags + task)
# ---------------------------------------------------------------------------

def get_flag(session_id: str, key: str) -> Optional[str]:
    """Return the value for (session_id, key) from session_kv, or None."""
    try:
        conn = db()
        row = conn.execute(
            "SELECT value FROM session_kv WHERE session_id=? AND key=?",
            (session_id, key),
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return row[0]
    except Exception:
        return None


def set_flag(session_id: str, key: str, value: str) -> None:
    """Insert or replace a (session_id, key, value) row in session_kv."""
    try:
        conn = db()
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO session_kv(session_id, key, value) VALUES (?, ?, ?)",
                (session_id, key, value),
            )
        conn.close()
    except Exception:
        pass


def get_session_task(session_id: str) -> Optional[str]:
    """Return the stored task summary for this session, or None."""
    return get_flag(session_id, "task")


def set_session_task(session_id: str, summary: str) -> None:
    """Store a task summary for this session."""
    set_flag(session_id, "task", summary)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def add_usage(
    session_id: str,
    model: str,
    input_tok: int,
    output_tok: int,
    cache_read_tok: int = 0,
) -> None:
    """Append a usage row."""
    try:
        conn = db()
        with conn:
            conn.execute(
                "INSERT INTO usage(session_id, model, input_tok, output_tok, cache_read_tok, ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (session_id, model, input_tok, output_tok, cache_read_tok, time.time()),
            )
        conn.close()
    except Exception:
        pass


def usage_rows(session_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return all usage rows (optionally filtered by session_id) as list of dicts."""
    try:
        conn = db()
        if session_id is not None:
            rows = conn.execute(
                "SELECT session_id, model, input_tok, output_tok, cache_read_tok, ts "
                "FROM usage WHERE session_id=? ORDER BY ts",
                (session_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT session_id, model, input_tok, output_tok, cache_read_tok, ts "
                "FROM usage ORDER BY ts"
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def record_saving(session_id: str, source: str, saved_tok: int) -> None:
    """Append a savings row (tokens saved by a context-reduction source)."""
    try:
        conn = db()
        with conn:
            conn.execute(
                "INSERT INTO savings(session_id, source, saved_tok, ts) "
                "VALUES (?, ?, ?, ?)",
                (session_id, source, saved_tok, time.time()),
            )
        conn.close()
    except Exception:
        pass


def session_savings(session_id: str) -> int:
    """Return the total tokens saved for this session."""
    try:
        conn = db()
        row = conn.execute(
            "SELECT COALESCE(SUM(saved_tok),0) FROM savings WHERE session_id=?",
            (session_id,),
        ).fetchone()
        conn.close()
        if row is None:
            return 0
        return int(row[0])
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cmd_init() -> None:
    """Create the database and apply the schema."""
    data_dir = nexum_data_dir()
    db_path = os.path.join(data_dir, "nexum.db")
    conn = db()
    conn.close()
    print(f"[nexum] Database initialised at {db_path}")


def _cmd_config() -> None:
    """Print the effective config as JSON."""
    cfg = get_config()
    print(json.dumps(cfg, sort_keys=True, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="store.py",
        description="Nexum store — foundation module CLI.",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("init", help="Create the nexum database and schema.")
    sub.add_parser("config", help="Print effective config as JSON.")

    args = parser.parse_args()

    if args.command == "init":
        _cmd_init()
    elif args.command == "config":
        _cmd_config()
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
