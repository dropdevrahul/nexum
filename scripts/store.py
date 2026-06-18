"""
store.py — Nexum plugin foundation.

Single source of truth for:
- Data-directory resolution (nexum_data_dir)
- SQLite connection + schema (db)
- Shared config (get_config)
- Utility helpers: sha256, estimate_tokens
- Dedup / memo helpers: seen_output, record_output, memo_get, memo_put
- Session KV (flags + task): get_flag, set_flag, get_session_task, set_session_task
- Metrics: add_usage, usage_rows, record_saving, session_savings,
  upsert_session_cost, session_cost_rows

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
    # When the running session token estimate crosses this, context_watch
    # nudges the user (once per window) to run /nx-save and capture a
    # resume point before the window fills. Fires below the compaction
    # threshold so a handoff can be written while context is still clean.
    # 0 disables the handoff nudge.
    "handoff_threshold_tokens": 100000,
    # Auto-handoff: when context crosses handoff_threshold_tokens, context_watch
    # writes a deterministic handoff skeleton (git state + task + tokens) to
    # handoff/<session>.md and handoff/latest.md — no model involvement, so it
    # is guaranteed even if the session then dies. Resume is NOT automatic: a
    # fresh session picks it up only when the user runs /nx-load (auto-injecting
    # prior context into every new session was judged too risky). 0 also via the
    # handoff_threshold disables the write.
    "handoff_auto_write_enabled": True,
    "scan_guard_enabled": True,
    "scan_deny_paths": [
        "node_modules", ".git", "dist", "build", "target", "vendor",
        ".next", "coverage", ".venv", "__pycache__",
    ],
    "intent_guard_enabled": True,
    "intent_similarity_threshold": 0.25,
    # Read-guard: cap Read of very large text files via PreToolUse updatedInput
    # (inject a line `limit`). PostToolUse output shrink cannot help here —
    # `updatedToolOutput` is ignored for built-in tools on current Claude Code —
    # but PreToolUse `updatedInput` IS honored for Read, so this is the working
    # lever for Read context savings. Only acts on files above the byte
    # threshold that don't already carry an explicit limit; the model can always
    # re-read further with an offset.
    "read_guard_enabled": True,
    "read_guard_min_bytes": 262144,   # 256 KB — only intervene on big files
    "read_guard_inject_lines": 2000,  # injected line limit (Read's own default cap)
    "statusline_compaction_warn_pct": 80,
    "statusline_compaction_warn_tokens": 80000,
    # When the 5-hour subscription plan window is this % used or more, the status
    # line suggests writing a handoff (/nx-save) so work can resume in a
    # fresh session after the window resets. 0 disables the plan warning.
    "statusline_plan_warn_pct": 90,
    # Dollar-weight applied to dedup (pointer-collapse) savings. A repeated tool
    # output would, under Claude Code's automatic prompt caching, bill at the
    # cache-read rate (~0.1x input) rather than full price — so collapsing it
    # saves ~0.1x of its tokens in dollar terms, not 1x. Truncation of fresh
    # (never-cached) output is weighted 1.0. Tunable for non-cached setups.
    "dedup_cache_weight": 0.1,
    # /nx-build dispatch granularity: "group" sends a whole route-tier
    # of steps to ONE executor dispatch (warm context, one cached prefix);
    # "step" sends one dispatch per step (more isolation, more cold starts).
    "dispatch_granularity": "group",
    # Same-tier retries before escalating a failing step one tier up.
    "max_same_tier_retries": 1,
    # Upper bound on steps sent in a single executor dispatch under "group"
    # granularity. A whole route-tier is still grouped for cache warmth, but a
    # tier with more than this many steps is split into sub-batches so one
    # dispatch can't overflow the executor's context (which would force a
    # mid-batch compaction and re-derivation) or widen the blast radius of a
    # single failure. 0 disables the cap (send the entire tier at once).
    "max_steps_per_dispatch": 6,
    # Resume: /nx-build persists each step's verdict to the step_ledger
    # table and, on a re-run for the same plan, skips already-`done` steps and
    # patch-retries `failed` ones from their saved diff — so a session that died
    # mid-plan resumes instead of redoing completed work. Set False to always
    # execute every step from scratch.
    "orchestrator_resume_enabled": True,
    # PreToolUse pre-emptive dedup: deny (or ask on) a tool call whose
    # normalised input was already executed earlier this session, avoiding a
    # redundant re-injection and recording a real saving.
    "predup_enabled": True,
    # When predup fires, either "deny" the repeat outright or "ask" the user.
    "predup_decision": "deny",
    # When True, also predup a conservative allowlist of read-only Bash commands
    # (cat, head, tail, ls, wc, grep family, find, git log/diff/show/status/branch).
    "predup_bash_readonly": False,
    # SessionStart resume nudge: when a recent handoff for the current branch
    # exists, surface a one-line hint without auto-loading anything.
    "resume_nudge_enabled": True,
    # Maximum age (hours) of a handoff before the nudge is suppressed.
    "resume_nudge_max_age_hours": 24,
    # Plan cost preview: parse a nexum plan file and print projected cost per
    # tier vs an all-opus baseline so /nx-build can show savings up front.
    "plan_preview_enabled": True,
    # Per-step token heuristic for the plan cost preview (input side).
    "plan_preview_input_tok_per_step": 8000,
    # Per-step token heuristic for the plan cost preview (output side).
    "plan_preview_output_tok_per_step": 2000,
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
        session_id    TEXT,
        source        TEXT,
        saved_tok     INTEGER,
        effective_tok INTEGER,
        ts            REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS session_cost(
        session_id          TEXT PRIMARY KEY,
        model               TEXT,
        cost_usd            REAL,
        input_tok           INTEGER,
        output_tok          INTEGER,
        cache_read_tok      INTEGER,
        cache_creation_tok  INTEGER,
        updated_ts          REAL
    )
    """,
    # Input-keyed tool-call store so PreToolUse predup can recognise repeat calls.
    # Keyed by (session_id, input_sig): input_sig is the SHA-256 of tool_name +
    # NUL + json.dumps(tool_input, sort_keys=True) so two identical invocations
    # produce the same key regardless of dict-insertion order.
    """
    CREATE TABLE IF NOT EXISTS tool_calls(
        session_id  TEXT,
        input_sig   TEXT,
        tool_name   TEXT,
        token_count INTEGER,
        file_path   TEXT,
        mtime       REAL,
        ts          REAL,
        PRIMARY KEY(session_id, input_sig)
    )
    """,
    # Step ledger — durable per-step execution state for /nx-build, so a
    # session that dies mid-plan resumes instead of redoing completed steps.
    # Keyed by (session_id, plan_hash, step_index): plan_hash ties a row to a
    # specific plan content, so editing the plan naturally invalidates stale
    # state (a new hash → no matching rows → clean start). status is one of
    # pending | done | failed. last_diff/verdict persist the failed attempt so a
    # retry (even across a restart) can patch rather than reimplement.
    """
    CREATE TABLE IF NOT EXISTS step_ledger(
        session_id  TEXT,
        plan_hash   TEXT,
        step_index  INTEGER,
        title       TEXT,
        route       TEXT,
        status      TEXT,
        tier_used   TEXT,
        last_diff   TEXT,
        verdict     TEXT,
        attempts    INTEGER,
        updated_ts  REAL,
        PRIMARY KEY(session_id, plan_hash, step_index)
    )
    """,
]

# Column migrations for databases created by an earlier schema version.
# Each entry: (table, column, column_def). ALTER is wrapped so a column that
# already exists (or any other error) never breaks db().
_MIGRATIONS = [
    ("savings", "effective_tok", "INTEGER"),
]


# ---------------------------------------------------------------------------
# nexum_data_dir
# ---------------------------------------------------------------------------

def project_data_dir(cwd: Optional[str] = None) -> str:
    """Resolve and create a project-local nexum data directory.

    Priority (kept in lockstep with the /nx-save and /nx-load commands so the
    handoff writer and reader always agree):
    1. $CLAUDE_PLUGIN_DATA (explicit override) — returned as-is.
    2. ``<git toplevel of cwd>/.nexum-data`` (project-scoped).
    3. ``<cwd or os.getcwd()>/.nexum-data`` (fallback when not in a git repo).

    Fail-open: if the git call raises or times out, falls back to cwd/os.getcwd()
    without raising.
    """
    env_data = os.environ.get("CLAUDE_PLUGIN_DATA", "").strip()
    if env_data:
        os.makedirs(env_data, exist_ok=True)
        return env_data

    base: Optional[str] = None
    effective_cwd = cwd or os.getcwd()
    try:
        import subprocess as _subprocess
        result = _subprocess.run(
            ["git", "-C", effective_cwd, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            base = result.stdout.strip()
    except Exception:
        pass
    if not base:
        base = effective_cwd
    path = Path(base) / ".nexum-data"
    os.makedirs(str(path), exist_ok=True)
    return str(path)


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
    """Create tables if they don't exist, then apply additive column migrations."""
    with conn:
        for ddl in _DDL:
            conn.execute(ddl)
        for table, column, coldef in _MIGRATIONS:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coldef}")
            except sqlite3.OperationalError:
                # Column already present — expected on an already-migrated db.
                pass


def _open_db(db_path: str) -> sqlite3.Connection:
    """Open the SQLite file, enable WAL, apply schema."""
    conn = sqlite3.connect(db_path, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _apply_schema(conn)
    return conn


# A process-wide shared-cache in-memory database used as the corruption
# fallback. A module-level "keeper" connection stays open for the life of the
# process so the shared cache survives even though every caller closes its own
# handle — making the fallback a single SHARED store rather than a fresh
# (stateless) DB per db() call.
_MEMORY_KEEPER: Optional[sqlite3.Connection] = None
_MEMORY_URI = "file:nexum-fallback?mode=memory&cache=shared"


def _memory_db() -> sqlite3.Connection:
    """Return a fresh handle to the process-wide shared in-memory database."""
    global _MEMORY_KEEPER
    if _MEMORY_KEEPER is None:
        keeper = sqlite3.connect(_MEMORY_URI, uri=True, check_same_thread=False)
        keeper.row_factory = sqlite3.Row
        _apply_schema(keeper)
        _MEMORY_KEEPER = keeper
    conn = sqlite3.connect(_MEMORY_URI, uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
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

    # Fall back to a SHARED in-memory DB so callers never raise AND still see
    # each other's writes within the process (a private :memory: per call would
    # silently turn dedup / session-KV into no-ops).
    try:
        return _memory_db()
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


def context_tokens_from_transcript(transcript_path: str) -> Optional[int]:
    """Return the real current context size from a Claude Code session transcript JSONL.

    Reads the file line by line and tracks the LAST line whose parsed object has a
    ``message.usage`` dict. Returns the sum of input_tokens + cache_creation_input_tokens
    + cache_read_input_tokens from that last usage block.

    Returns None if *transcript_path* is falsy, the file is missing/unreadable, no line
    has ``message.usage``, or any exception occurs (fail-open).
    """
    if not transcript_path:
        return None
    try:
        last_usage: Optional[dict] = None
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                msg = d.get("message") or {}
                usage = msg.get("usage")
                if isinstance(usage, dict):
                    last_usage = usage
        if last_usage is None:
            return None
        return (
            int(last_usage.get("input_tokens") or 0)
            + int(last_usage.get("cache_creation_input_tokens") or 0)
            + int(last_usage.get("cache_read_input_tokens") or 0)
        )
    except Exception:
        return None


def transcript_tool_result_len(transcript_path: str, tool_use_id: str) -> Optional[int]:
    """Return the character length of the tool_result the MODEL actually received
    for *tool_use_id*, by reading the session transcript JSONL.

    This is the oracle for the PostToolUse ``updatedToolOutput`` self-test: the
    transcript records the post-hook tool result the model saw, so comparing its
    length against what a hook emitted reveals whether the replacement took
    effect (it is silently ignored for built-in tools on current Claude Code —
    see anthropics/claude-code #65403/#32105). Returns None if not found or on
    any error (caller treats None as "undetermined, try later").
    """
    if not transcript_path or not tool_use_id:
        return None
    try:
        found: Optional[int] = None
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"tool_result"' not in line or tool_use_id not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                msg = d.get("message") or {}
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_result"
                        and block.get("tool_use_id") == tool_use_id
                    ):
                        c = block.get("content")
                        if isinstance(c, list):
                            text = "".join(
                                x.get("text", "") for x in c if isinstance(x, dict)
                            )
                        elif isinstance(c, str):
                            text = c
                        else:
                            text = str(c)
                        found = len(text)  # keep the last (most recent) match
        return found
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Input-keyed tool-call helpers (used by PreToolUse predup)
# ---------------------------------------------------------------------------

def tool_call_sig(tool_name: str, tool_input: dict) -> str:
    """Return the SHA-256 of tool_name + NUL + json.dumps(tool_input, sort_keys=True).

    Deterministic for identical inputs regardless of dict insertion order.
    Uses the existing sha256() helper.
    """
    serialised = tool_name + "\x00" + json.dumps(tool_input, sort_keys=True, default=str)
    return sha256(serialised)


def record_tool_call(
    session_id: str,
    input_sig: str,
    tool_name: str,
    token_count: int,
    file_path=None,
    mtime=None,
) -> None:
    """INSERT OR REPLACE a row into tool_calls with ts=time.time(). Fail-open."""
    try:
        conn = db()
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO tool_calls"
                "(session_id, input_sig, tool_name, token_count, file_path, mtime, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, input_sig, tool_name, token_count, file_path, mtime, time.time()),
            )
        conn.close()
    except Exception:
        pass


def seen_tool_call(session_id: str, input_sig: str) -> Optional[Dict[str, Any]]:
    """Return the tool_calls row dict for (session_id, input_sig), or None."""
    try:
        conn = db()
        row = conn.execute(
            "SELECT session_id, input_sig, tool_name, token_count, file_path, mtime, ts "
            "FROM tool_calls WHERE session_id=? AND input_sig=?",
            (session_id, input_sig),
        ).fetchone()
        conn.close()
        if row is None:
            return None
        return dict(row)
    except Exception:
        return None


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


def record_saving(
    session_id: str,
    source: str,
    saved_tok: int,
    effective_tok: Optional[int] = None,
) -> None:
    """Append a savings row.

    ``saved_tok`` is the raw token count removed from the tool output.
    ``effective_tok`` is the dollar-equivalent saving after accounting for
    prompt-cache economics (a deduped re-read would have billed at the
    cache-read rate, not full price). When omitted it defaults to ``saved_tok``
    (full weight) so direct callers and full-price truncations are unchanged.
    """
    if effective_tok is None:
        effective_tok = saved_tok
    try:
        conn = db()
        with conn:
            conn.execute(
                "INSERT INTO savings(session_id, source, saved_tok, effective_tok, ts) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_id, source, saved_tok, effective_tok, time.time()),
            )
        conn.close()
    except Exception:
        pass


def session_savings(session_id: str) -> int:
    """Return the cache-adjusted (dollar-equivalent) tokens saved this session.

    Sums ``effective_tok``, falling back to ``saved_tok`` for legacy rows
    written before the cache-weight column existed.
    """
    try:
        conn = db()
        row = conn.execute(
            "SELECT COALESCE(SUM(COALESCE(effective_tok, saved_tok)),0) "
            "FROM savings WHERE session_id=?",
            (session_id,),
        ).fetchone()
        conn.close()
        if row is None:
            return 0
        return int(row[0])
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Session cost snapshot — Claude Code's own metered, cache-accurate totals.
#
# Claude Code's statusLine hook receives a per-session JSON that already carries
# `cost.total_cost_usd` (the authoritative metered bill, which internally
# reflects cache writes/reads) plus cumulative token counts. Those values are
# CUMULATIVE, so we UPSERT a single row per session rather than appending —
# this is the one reliable place a stdlib hook can observe real API spend
# without standing up an OTel collector.
# ---------------------------------------------------------------------------

def upsert_session_cost(
    session_id: str,
    model: str,
    cost_usd: float,
    input_tok: int = 0,
    output_tok: int = 0,
    cache_read_tok: int = 0,
    cache_creation_tok: int = 0,
) -> None:
    """Insert or replace the latest cumulative cost snapshot for a session."""
    try:
        conn = db()
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO session_cost("
                "session_id, model, cost_usd, input_tok, output_tok, "
                "cache_read_tok, cache_creation_tok, updated_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id, model, float(cost_usd or 0.0),
                    int(input_tok or 0), int(output_tok or 0),
                    int(cache_read_tok or 0), int(cache_creation_tok or 0),
                    time.time(),
                ),
            )
        conn.close()
    except Exception:
        pass


def session_cost_rows(session_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return session_cost snapshot rows (optionally filtered by session_id)."""
    cols = (
        "session_id, model, cost_usd, input_tok, output_tok, "
        "cache_read_tok, cache_creation_tok, updated_ts"
    )
    try:
        conn = db()
        if session_id is not None:
            rows = conn.execute(
                f"SELECT {cols} FROM session_cost WHERE session_id=? ORDER BY updated_ts",
                (session_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {cols} FROM session_cost ORDER BY updated_ts"
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Step ledger — durable execution state for /nx-build resume
# ---------------------------------------------------------------------------

_STEP_COLS = (
    "session_id, plan_hash, step_index, title, route, status, "
    "tier_used, last_diff, verdict, attempts, updated_ts"
)


def record_step(
    session_id: str,
    plan_hash: str,
    step_index: int,
    status: str,
    title: Optional[str] = None,
    route: Optional[str] = None,
    tier_used: Optional[str] = None,
    last_diff: Optional[str] = None,
    verdict: Optional[str] = None,
    attempts: Optional[int] = None,
) -> None:
    """Upsert a step's execution state.

    Only the fields passed (non-None) overwrite existing values; omitted fields
    preserve whatever the prior row held. ``status`` is required and always
    written. ``attempts`` defaults to preserving the prior count; pass an
    explicit value to set it. This lets the orchestrator mark a step ``done``
    with a one-liner while still being able to persist a failed attempt's diff
    and guardrail verdict for a later (possibly post-restart) patch-retry.
    """
    try:
        conn = db()
        with conn:
            prior = conn.execute(
                f"SELECT {_STEP_COLS} FROM step_ledger "
                "WHERE session_id=? AND plan_hash=? AND step_index=?",
                (session_id, plan_hash, step_index),
            ).fetchone()
            prior = dict(prior) if prior is not None else {}

            def pick(name, value):
                return value if value is not None else prior.get(name)

            row = {
                "title": pick("title", title),
                "route": pick("route", route),
                "tier_used": pick("tier_used", tier_used),
                "last_diff": pick("last_diff", last_diff),
                "verdict": pick("verdict", verdict),
                "attempts": attempts if attempts is not None else (prior.get("attempts") or 0),
            }
            conn.execute(
                "INSERT OR REPLACE INTO step_ledger("
                "session_id, plan_hash, step_index, title, route, status, "
                "tier_used, last_diff, verdict, attempts, updated_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id, plan_hash, int(step_index),
                    row["title"], row["route"], status,
                    row["tier_used"], row["last_diff"], row["verdict"],
                    int(row["attempts"]), time.time(),
                ),
            )
        conn.close()
    except Exception:
        pass


def get_step(session_id: str, plan_hash: str, step_index: int) -> Optional[Dict[str, Any]]:
    """Return the step_ledger row dict for one step, or None."""
    try:
        conn = db()
        row = conn.execute(
            f"SELECT {_STEP_COLS} FROM step_ledger "
            "WHERE session_id=? AND plan_hash=? AND step_index=?",
            (session_id, plan_hash, int(step_index)),
        ).fetchone()
        conn.close()
        return dict(row) if row is not None else None
    except Exception:
        return None


def step_ledger_rows(session_id: str, plan_hash: str) -> List[Dict[str, Any]]:
    """Return all step rows for a (session, plan), ordered by step_index."""
    try:
        conn = db()
        rows = conn.execute(
            f"SELECT {_STEP_COLS} FROM step_ledger "
            "WHERE session_id=? AND plan_hash=? ORDER BY step_index",
            (session_id, plan_hash),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def clear_step_ledger(session_id: str, plan_hash: Optional[str] = None) -> None:
    """Delete step rows for a session (optionally scoped to one plan_hash)."""
    try:
        conn = db()
        with conn:
            if plan_hash is None:
                conn.execute("DELETE FROM step_ledger WHERE session_id=?", (session_id,))
            else:
                conn.execute(
                    "DELETE FROM step_ledger WHERE session_id=? AND plan_hash=?",
                    (session_id, plan_hash),
                )
        conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Dispatch batching — deterministic sub-batch partition for /nx-build
# ---------------------------------------------------------------------------

def partition_steps(items: List[Any], max_per: int) -> List[List[Any]]:
    """Split an ordered list of step indices into ordered sub-batches of at
    most *max_per* items each, preserving order.

    This is the code-enforced version of the orchestrator's batch cap: rather
    than the prompt judging "more than N" by eye, it calls this so splitting is
    deterministic. Order preservation is what keeps it dependency-safe — the
    planner already orders steps so a prerequisite precedes its dependent, and
    chunking in order means a dependent never lands in a sub-batch that runs
    *before* its prerequisite's. ``max_per <= 0`` means no cap (one batch).
    """
    items = list(items)
    if max_per is None or max_per <= 0 or len(items) <= max_per:
        return [items] if items else []
    return [items[i:i + max_per] for i in range(0, len(items), max_per)]


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


def _read_optional_file(path: Optional[str]) -> Optional[str]:
    """Read a file's text, or None. '-' means stdin. Missing → None (fail-open)."""
    if not path:
        return None
    try:
        if path == "-":
            return sys.stdin.read()
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except Exception:
        return None


def _cmd_plan_hash(args) -> None:
    """Print the content hash of a plan file — the orchestrator's ledger key."""
    text = _read_optional_file(args.file) or ""
    print(sha256(text))


def _cmd_step_set(args) -> None:
    """Upsert one step's ledger state. Big fields load from files (or stdin '-')."""
    record_step(
        session_id=args.session,
        plan_hash=args.plan_hash,
        step_index=args.index,
        status=args.status,
        title=args.title,
        route=args.route,
        tier_used=args.tier,
        last_diff=_read_optional_file(args.diff_file),
        verdict=_read_optional_file(args.verdict_file),
        attempts=args.attempts,
    )
    print(json.dumps({"ok": True, "step_index": args.index, "status": args.status}))


def _cmd_step_get(args) -> None:
    """Print one step row as JSON (null if absent)."""
    print(json.dumps(get_step(args.session, args.plan_hash, args.index)))


def _cmd_step_list(args) -> None:
    """Print all step rows for a (session, plan) as a JSON array."""
    print(json.dumps(step_ledger_rows(args.session, args.plan_hash)))


def _cmd_step_clear(args) -> None:
    """Delete step rows for a session (optionally one plan_hash)."""
    clear_step_ledger(args.session, args.plan_hash)
    print(json.dumps({"ok": True}))


def _cmd_plan_batches(args) -> None:
    """Print the ordered sub-batches for a comma-separated list of step indices.

    --max defaults to config max_steps_per_dispatch when omitted.
    """
    raw = [tok.strip() for tok in (args.indices or "").split(",") if tok.strip()]
    indices: List[Any] = []
    for tok in raw:
        try:
            indices.append(int(tok))
        except ValueError:
            indices.append(tok)
    max_per = args.max if args.max is not None else int(
        get_config().get("max_steps_per_dispatch", 6)
    )
    print(json.dumps(partition_steps(indices, max_per)))


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="store.py",
        description="Nexum store — foundation module CLI.",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("init", help="Create the nexum database and schema.")
    sub.add_parser("config", help="Print effective config as JSON.")

    p_hash = sub.add_parser("plan-hash", help="Print the content hash of a plan file.")
    p_hash.add_argument("--file", required=True, help="Plan file path ('-' for stdin).")

    p_set = sub.add_parser("step-set", help="Upsert a step's ledger state.")
    p_set.add_argument("--session", required=True)
    p_set.add_argument("--plan-hash", required=True)
    p_set.add_argument("--index", type=int, required=True)
    p_set.add_argument("--status", required=True, choices=["pending", "done", "failed"])
    p_set.add_argument("--title")
    p_set.add_argument("--route")
    p_set.add_argument("--tier")
    p_set.add_argument("--diff-file", help="Path to the attempt's diff ('-' for stdin).")
    p_set.add_argument("--verdict-file", help="Path to the guardrail verdict JSON ('-' for stdin).")
    p_set.add_argument("--attempts", type=int)

    p_get = sub.add_parser("step-get", help="Print one step row as JSON.")
    p_get.add_argument("--session", required=True)
    p_get.add_argument("--plan-hash", required=True)
    p_get.add_argument("--index", type=int, required=True)

    p_list = sub.add_parser("step-list", help="Print all step rows for a (session, plan).")
    p_list.add_argument("--session", required=True)
    p_list.add_argument("--plan-hash", required=True)

    p_clear = sub.add_parser("step-clear", help="Delete step rows for a session.")
    p_clear.add_argument("--session", required=True)
    p_clear.add_argument("--plan-hash", default=None)

    p_batches = sub.add_parser(
        "plan-batches",
        help="Partition step indices into capped, order-preserving sub-batches.",
    )
    p_batches.add_argument("--indices", required=True,
                           help="Comma-separated step indices in execution order.")
    p_batches.add_argument("--max", type=int, default=None,
                           help="Cap per batch; defaults to max_steps_per_dispatch.")

    args = parser.parse_args()

    if args.command == "init":
        _cmd_init()
    elif args.command == "config":
        _cmd_config()
    elif args.command == "plan-hash":
        _cmd_plan_hash(args)
    elif args.command == "step-set":
        _cmd_step_set(args)
    elif args.command == "step-get":
        _cmd_step_get(args)
    elif args.command == "step-list":
        _cmd_step_list(args)
    elif args.command == "step-clear":
        _cmd_step_clear(args)
    elif args.command == "plan-batches":
        _cmd_plan_batches(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
