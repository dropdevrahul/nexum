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
DEFAULT_PRICING: Dict[str, tuple] = {
    "fable":  (10.0, 50.0),
    "opus":   (5.0,  25.0),
    "sonnet": (3.0,  15.0),
    "haiku":  (1.0,   5.0),
}


def get_pricing(cfg: Optional[dict] = None) -> Dict[str, tuple]:
    """Return pricing dict, merging user overrides from config.

    User config.json can contain {"pricing": {"my-model": (2.0, 10.0)}}
    entry to add or override model rates. Returns defaults if no config.
    """
    if cfg is None:
        cfg = get_config()
    user_pricing = cfg.get("pricing", {})
    base = dict(DEFAULT_PRICING)
    for model, rates in user_pricing.items():
        if isinstance(rates, (list, tuple)) and len(rates) == 2:
            base[model] = (float(rates[0]), float(rates[1]))
    return base

# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------
_CONFIG_DEFAULTS: Dict[str, Any] = {
    "pricing": {},
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
    # Divergent-task worktrees: when the intent-guard detects a task switch AND
    # the working tree has uncommitted changes, nexum creates an isolated git
    # worktree under .nexum-data/worktrees/<slug> (branch nexum/<slug>) so the
    # new task doesn't tangle with the unfinished work, and blocks the prompt
    # with a pointer to it ('continue' still works to stay put). A CLEAN tree
    # needs no worktree — the divergent task is simply allowed in place. Set
    # False to disable worktree creation (the guard then always allows on a
    # clean tree and, on a dirty tree, just blocks with a heads-up).
    "worktree_enabled": True,
    # Untracked files (globs, relative to repo root) to COPY into a freshly
    # created worktree — git only checks out tracked files, so env/local config
    # the new work needs must be copied explicitly, e.g. [".env", "*.local.*"].
    "worktree_copy": [],
    # Globs (relative to repo root) to EXCLUDE from the copy above even when a
    # worktree_copy pattern would have matched them.
    "worktree_ignore": [],
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
    # Default lowered from 6 to 4 after a 6-step grouped Sonnet dispatch stalled
    # the stream watchdog mid-batch (600s no-progress) and lost the whole batch;
    # smaller batches bound the blast radius and the per-dispatch context.
    "max_steps_per_dispatch": 4,
    # Size-aware dispatch cap (used by plan_preview.py --indices). The step-count
    # cap above is blunt: 4 trivial steps and 4 huge-file steps are very
    # different context loads. This bounds a single grouped dispatch by ESTIMATED
    # context tokens (per-step base + the byte size of each declared file ÷ 4),
    # so a few large-file steps get split off even under the count cap. A single
    # step over budget still dispatches alone (a step is never split). 0 disables
    # the size bound (revert to the count-only cap).
    "max_dispatch_context_tokens": 50000,
    # Per-step base overhead (tokens) added to each step's file-size estimate
    # when computing the size-aware partition — covers the shared spec/prompt and
    # the step's own fields, independent of how big its files are.
    "dispatch_step_base_tokens": 1500,
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
    # Max age (seconds) of a recorded tool_calls row before predup STOPS treating
    # it as "still in context." A row only proves the output was injected once —
    # not that it survived. Subagents share the parent's tool_calls DB, and
    # compaction/resume silently evicts output while the row persists, so an old
    # row can deny a legitimate read whose content is no longer (or never was) in
    # the live context. Beyond this window predup lets the call through. 0
    # disables the age check (revert to the original "ever-recorded" behaviour).
    "predup_max_age_seconds": 900,
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
    # Caveman prompts: /nx-plan writes the plan's PROSE fields (task summary,
    # step title/objective/contract/scope) in clipped, telegraphic English —
    # articles, copulas, and filler dropped — and /nx-build builds its executor
    # dispatch prompts the same way. The plan is re-read by every executor and
    # the dispatch prefix is sent on every step, so trimming function words from
    # them is a recurring token saving. STRICT carve-outs (never caveman-ified):
    # file paths, identifiers, signatures, config keys, code, and the runnable
    # `acceptance` command stay verbatim, and a `contract` must stay
    # unambiguous — terseness never costs precision. Set False for normal prose.
    "caveman_prompts_enabled": True,
    # Route calibration: track per-route dispatch outcomes (pass/escalate) and
    # feed the data back into routing decisions over time.
    "route_calib_enabled": True,
    # Minimum ratio of passed_first_try / dispatched to consider a route reliable.
    "route_calib_min_success_ratio": 0.6,
    # Minimum number of dispatches before calibration data is trusted.
    "route_calib_min_samples": 5,
    # Downgrade threshold (bidirectional calibration): when a route's Wilson
    # lower-bound first-try pass rate is at or above this, calibration may nudge
    # comparable steps DOWN one tier (cheaper) — the counterpart to the up-nudge.
    # Conservative by default so a downgrade needs strong evidence. 1.0 disables
    # downgrades (revert to up-only calibration).
    "route_calib_downgrade_ratio": 0.9,
    # Grep narrowing (PreToolUse): instead of hard-denying a broad/unscoped
    # search, cap its output — inject `head_limit` on the Grep tool and append
    # `| head -n grep_head_limit` to an unscoped recursive Bash grep/rg. This is
    # a *working* context-savings lever on current Claude Code (PreToolUse
    # updatedInput is honored, unlike PostToolUse output shrink). Searches that
    # target a deny-listed directory are still denied outright. False reverts to
    # the deny-everything behaviour.
    "grep_narrow_enabled": True,
    "grep_head_limit": 80,
    # SessionStart audit nudge: surface /nx-audit hint when ignore config has
    # findings (throttled to once per repo per audit_nudge_throttle_hours).
    "audit_nudge_enabled": True,
    # Hours between successive audit nudges for the same repo.
    "audit_nudge_throttle_hours": 24,
    # PreCompact hook: clear this session's tool_calls rows before a compaction
    # (which evicts the cached output predup keys off) and write a handoff
    # skeleton at the exact compaction boundary (deterministic, not estimated).
    "precompact_invalidate_predup": True,
    "precompact_handoff_enabled": True,
    # SubagentStop hook: record a real per-tier usage row (parsed best-effort
    # from the subagent transcript) for nexum executor agents, replacing the
    # pure estimate in the cost report. Token attribution is best-effort — the
    # SubagentStop payload carries no usage fields, so this reads the transcript.
    "subagent_usage_enabled": True,
    # Retention: rows in the ephemeral tables (tool_calls, savings, outputs,
    # usage, file_activity, memo) older than this many days are pruned on
    # session start so the SQLite file and predup lookups stay bounded. 0
    # disables pruning.
    "retention_days": 14,
    # Wasted-context tracking: record per-file read/edit counts so /nx-report
    # can flag files read into context but never edited.
    "file_activity_enabled": True,
    # Budget alerts (C): tiered warnings as session spend approaches a budget.
    # budget_usd is checked against the real metered cost nexum captures from
    # the status line; budget_tokens against cumulative session tokens. 0
    # disables that axis. Alerts fire once per tier, escalating, via a
    # non-blocking systemMessage on UserPromptSubmit.
    "budget_usd": 0.0,
    "budget_tokens": 0,
    "budget_alert_tiers": [50, 70, 80, 90],
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
    # Route calibration — durable per-(repo, route) dispatch outcome counters.
    # Accumulated incrementally by /nx-build so routing decisions can be tuned
    # over time. updated_ts is epoch seconds of the last upsert.
    """
    CREATE TABLE IF NOT EXISTS route_calibration(
        repo              TEXT,
        route             TEXT,
        dispatched        INTEGER,
        passed_first_try  INTEGER,
        escalated         INTEGER,
        updated_ts        REAL,
        PRIMARY KEY(repo, route)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS file_activity(
        session_id    TEXT,
        file_path     TEXT,
        reads         INTEGER,
        partial_reads INTEGER,
        edits         INTEGER,
        tokens_read   INTEGER,
        ts            REAL,
        PRIMARY KEY(session_id, file_path)
    )
    """,
    # Agents registry — durable state for headless-CLI agents dispatched by
    # /nx-build (claude / opencode / cursor). Keyed by agent_id (caller-chosen,
    # e.g. a uuid or "<session>-<step_index>"). Intentionally NOT in
    # _PRUNE_TABLES: an active agent row must survive the retention sweep, and
    # a finished one is small and useful for post-hoc cost/status queries.
    """
    CREATE TABLE IF NOT EXISTS agents(
        agent_id    TEXT PRIMARY KEY,
        harness     TEXT,
        model       TEXT,
        repo_root   TEXT,
        worktree    TEXT,
        branch      TEXT,
        pid         INTEGER,
        log_path    TEXT,
        task        TEXT,
        plan_hash   TEXT,
        step_index  INTEGER,
        status      TEXT,
        cost_usd    REAL,
        session_id  TEXT,
        tmux        TEXT,
        started_ts  REAL,
        updated_ts  REAL
    )
    """,
]

# Column migrations for databases created by an earlier schema version.
# Each entry: (table, column, column_def). ALTER is wrapped so a column that
# already exists (or any other error) never breaks db().
_MIGRATIONS = [
    ("savings", "effective_tok", "INTEGER"),
    ("agents", "session_id", "TEXT"),
    ("agents", "tmux", "TEXT"),
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


def transcript_usage_totals(transcript_path: str) -> Dict[str, int]:
    """Sum token usage across all assistant turns in a transcript JSONL.

    Returns ``{"input_tok", "output_tok", "cache_read_tok"}``. Used by the
    SubagentStop hook to attribute real per-tier usage: the hook payload carries
    no usage fields, but the subagent's transcript records per-message ``usage``
    blocks. Best-effort — returns zeros on any error or an empty/absent file.
    Note: if the path is the parent transcript rather than the subagent's, this
    over-counts; callers treat the result as a best-effort signal.
    """
    totals = {"input_tok": 0, "output_tok": 0, "cache_read_tok": 0}
    if not transcript_path:
        return totals
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                msg = d.get("message") or {}
                usage = msg.get("usage")
                if not isinstance(usage, dict):
                    continue
                totals["input_tok"] += int(usage.get("input_tokens") or 0)
                totals["output_tok"] += int(usage.get("output_tokens") or 0)
                totals["cache_read_tok"] += int(usage.get("cache_read_input_tokens") or 0)
    except Exception:
        return {"input_tok": 0, "output_tok": 0, "cache_read_tok": 0}
    return totals


# ---------------------------------------------------------------------------
# Input-keyed tool-call helpers (used by PreToolUse predup)
# ---------------------------------------------------------------------------

def _canonical_tool_input(tool_input: dict) -> dict:
    """Return a copy of tool_input with path-like keys canonicalised.

    Resolves ``file_path``/``path`` to an absolute, symlink-free path so that
    ``./foo.py``, ``foo.py``, and ``/abs/foo.py`` all produce the same
    signature. Other keys (e.g. a Read ``offset``/``limit``, a Grep ``pattern``)
    are preserved verbatim — a different range or pattern is genuinely different
    content and must not collapse. Best-effort: on any error the original value
    is kept.
    """
    if not isinstance(tool_input, dict):
        return tool_input
    canon = dict(tool_input)
    for key in ("file_path", "path"):
        val = canon.get(key)
        if isinstance(val, str) and val:
            try:
                canon[key] = os.path.realpath(val)
            except Exception:
                pass
    return canon


def tool_call_sig(tool_name: str, tool_input: dict) -> str:
    """Return the SHA-256 of tool_name + NUL + canonicalised, sorted tool_input.

    Deterministic for equivalent inputs regardless of dict insertion order or
    how a path was spelled (``./foo`` vs ``foo`` vs an absolute path). Uses the
    existing sha256() helper.
    """
    canon = _canonical_tool_input(tool_input)
    serialised = tool_name + "\x00" + json.dumps(canon, sort_keys=True, default=str)
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


def clear_tool_calls(session_id: str) -> int:
    """Delete all tool_calls rows for a session. Returns the row count removed.

    Called when context that predup keys off is evicted — on PreCompact (before
    a compaction) and on a SessionStart whose source is ``clear``/``compact`` —
    so predup can never deny a re-read of content no longer in the live context.
    Fail-open: returns 0 on any error.
    """
    try:
        conn = db()
        with conn:
            cur = conn.execute(
                "DELETE FROM tool_calls WHERE session_id=?", (session_id,)
            )
            n = cur.rowcount
        conn.close()
        return int(n or 0)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# File-activity accounting (wasted-context analytics)
#
# Per-session, per-file counters used by /nx-report to flag files that were
# read into context but never edited ("wasted"), and by the budget alert to
# name the biggest such files. file_path is realpath-canonicalised so the same
# file under different spellings aggregates into one row.
# ---------------------------------------------------------------------------

def _realpath(file_path: str) -> str:
    try:
        return os.path.realpath(file_path)
    except Exception:
        return file_path


def record_file_read(session_id: str, file_path: str, tokens: int, partial: bool = False) -> None:
    """Increment a file's read counters and accumulate the tokens it injected."""
    if not file_path:
        return
    fp = _realpath(file_path)
    try:
        conn = db()
        with conn:
            row = conn.execute(
                "SELECT reads, partial_reads, edits, tokens_read FROM file_activity "
                "WHERE session_id=? AND file_path=?",
                (session_id, fp),
            ).fetchone()
            reads = (row["reads"] if row else 0) + 1
            preads = (row["partial_reads"] if row else 0) + (1 if partial else 0)
            edits = row["edits"] if row else 0
            toks = (row["tokens_read"] if row else 0) + int(tokens or 0)
            conn.execute(
                "INSERT OR REPLACE INTO file_activity"
                "(session_id, file_path, reads, partial_reads, edits, tokens_read, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, fp, reads, preads, edits, toks, time.time()),
            )
        conn.close()
    except Exception:
        pass


def record_file_edit(session_id: str, file_path: str) -> None:
    """Increment a file's edit counter (marks the file as useful, not wasted)."""
    if not file_path:
        return
    fp = _realpath(file_path)
    try:
        conn = db()
        with conn:
            row = conn.execute(
                "SELECT reads, partial_reads, edits, tokens_read FROM file_activity "
                "WHERE session_id=? AND file_path=?",
                (session_id, fp),
            ).fetchone()
            reads = row["reads"] if row else 0
            preads = row["partial_reads"] if row else 0
            edits = (row["edits"] if row else 0) + 1
            toks = row["tokens_read"] if row else 0
            conn.execute(
                "INSERT OR REPLACE INTO file_activity"
                "(session_id, file_path, reads, partial_reads, edits, tokens_read, ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, fp, reads, preads, edits, toks, time.time()),
            )
        conn.close()
    except Exception:
        pass


def file_activity_rows(session_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return file_activity rows (optionally filtered by session_id)."""
    cols = "session_id, file_path, reads, partial_reads, edits, tokens_read, ts"
    try:
        conn = db()
        if session_id is not None:
            rows = conn.execute(
                f"SELECT {cols} FROM file_activity WHERE session_id=? ORDER BY tokens_read DESC",
                (session_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {cols} FROM file_activity ORDER BY tokens_read DESC"
            ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def wasted_files(session_id: str, limit: int = 5) -> List[Dict[str, Any]]:
    """Return files read into context but never edited, biggest first.

    These are the prime "drop this to save tokens" candidates. Returns dicts
    with file_path / tokens_read / reads.
    """
    try:
        conn = db()
        rows = conn.execute(
            "SELECT file_path, tokens_read, reads FROM file_activity "
            "WHERE session_id=? AND edits=0 AND reads>=1 AND tokens_read>0 "
            "ORDER BY tokens_read DESC LIMIT ?",
            (session_id, int(limit)),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


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


# ---------------------------------------------------------------------------
# current_repo helper
# ---------------------------------------------------------------------------

def current_repo(cwd: Optional[str] = None) -> str:
    """Return the basename of the git toplevel for *cwd* (or os.getcwd()).

    Falls back to the string ``"default"`` when not inside a git repo or on
    any error (fail-open: never raises).
    """
    try:
        import subprocess as _subprocess
        effective_cwd = cwd or os.getcwd()
        result = _subprocess.run(
            ["git", "-C", effective_cwd, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return os.path.basename(result.stdout.strip())
    except Exception:
        pass
    return "default"


# ---------------------------------------------------------------------------
# Route calibration helpers
# ---------------------------------------------------------------------------

def record_calibration(
    repo: str,
    route: str,
    *,
    dispatched: int = 0,
    passed_first_try: int = 0,
    escalated: int = 0,
) -> None:
    """Upsert per-(repo, route) calibration counters, accumulating increments.

    Reads any existing row and writes back the summed counters via INSERT OR
    REPLACE so concurrent writes naturally accumulate.  Fail-open.
    """
    try:
        conn = db()
        existing = conn.execute(
            "SELECT dispatched, passed_first_try, escalated "
            "FROM route_calibration WHERE repo=? AND route=?",
            (repo, route),
        ).fetchone()
        if existing:
            new_dispatched      = existing["dispatched"]      + dispatched
            new_passed          = existing["passed_first_try"] + passed_first_try
            new_escalated       = existing["escalated"]       + escalated
        else:
            new_dispatched      = dispatched
            new_passed          = passed_first_try
            new_escalated       = escalated
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO route_calibration"
                "(repo, route, dispatched, passed_first_try, escalated, updated_ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (repo, route, new_dispatched, new_passed, new_escalated, time.time()),
            )
        conn.close()
    except Exception:
        pass


def calibration_rows(repo: str) -> List[Dict[str, Any]]:
    """Return all route_calibration rows for *repo*, ordered by route.

    Fail-open: returns ``[]`` on any error.
    """
    try:
        conn = db()
        rows = conn.execute(
            "SELECT repo, route, dispatched, passed_first_try, escalated, updated_ts "
            "FROM route_calibration WHERE repo=? ORDER BY route",
            (repo,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


# Tier order from cheapest to strongest; an "up" nudge moves one step right,
# a "down" nudge one step left.
_ROUTE_ORDER = ["mechanical", "standard", "needs-strong"]


def _wilson_bounds(passed: int, n: int, z: float = 1.96) -> tuple:
    """Return the (lower, upper) Wilson score interval for a pass proportion.

    Deterministic (no third-party stats lib; uses ``** 0.5`` for the root). The
    lower bound discounts small samples, so a 3/3 streak does not read as a
    confident 100%. Returns (0.0, 0.0) for n <= 0.
    """
    if n <= 0:
        return (0.0, 0.0)
    phat = passed / n
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = phat + z2 / (2.0 * n)
    margin = z * (((phat * (1.0 - phat)) + z2 / (4.0 * n)) / n) ** 0.5
    return ((centre - margin) / denom, (centre + margin) / denom)


def calibration_advice(repo: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
    """Return deterministic per-route routing advice from calibration history.

    For each route with enough evidence, returns
    ``{"action": "up"|"down"|"keep", "reason", "samples", "lower", "source"}``.
    Uses the repo's own rows when it has >= ``route_calib_min_samples``
    dispatches; otherwise falls back to the cross-repo ``"_global"`` aggregate.
    A Wilson score lower bound replaces a raw pass ratio so small samples don't
    over-trigger. Advice is **bidirectional**: nudge up one tier when confidence
    the tier handles this repo is low, down one tier when a tier clears first-try
    very reliably (cheaper tier may suffice). Conservative and advisory only.
    Fail-open: returns ``{}`` on any error or when calibration is disabled.
    """
    try:
        if cfg is None:
            cfg = get_config()
        if not cfg.get("route_calib_enabled", True):
            return {}
        min_samples = int(cfg.get("route_calib_min_samples", 5))
        up_ratio = float(cfg.get("route_calib_min_success_ratio", 0.6))
        down_ratio = float(cfg.get("route_calib_downgrade_ratio", 0.9))

        own = {r["route"]: r for r in calibration_rows(repo)}
        glob = {r["route"]: r for r in calibration_rows("_global")}

        advice: Dict[str, Dict[str, Any]] = {}
        for idx, route in enumerate(_ROUTE_ORDER):
            row = own.get(route)
            source = "repo"
            if not row or int(row.get("dispatched") or 0) < min_samples:
                grow = glob.get(route)
                if grow and int(grow.get("dispatched") or 0) >= min_samples:
                    row, source = grow, "global"
                else:
                    continue  # not enough evidence in this repo or globally
            n = int(row.get("dispatched") or 0)
            passed = int(row.get("passed_first_try") or 0)
            lower, _upper = _wilson_bounds(passed, n)
            tag = "" if source == "repo" else " (cross-repo prior)"
            if lower < up_ratio and idx < len(_ROUTE_ORDER) - 1:
                advice[route] = {
                    "action": "up",
                    "reason": (
                        f"{route} steps clear first-try with only {lower:.0%} "
                        f"lower-bound confidence over {n} dispatches{tag} "
                        f"(< {up_ratio:.0%}); route up one tier"
                    ),
                    "samples": n, "lower": round(lower, 3), "source": source,
                }
            elif down_ratio < 1.0 and lower >= down_ratio and idx > 0:
                advice[route] = {
                    "action": "down",
                    "reason": (
                        f"{route} steps clear first-try reliably ({lower:.0%} "
                        f"lower-bound over {n}{tag} >= {down_ratio:.0%}); a "
                        f"cheaper tier may suffice — route down one tier"
                    ),
                    "samples": n, "lower": round(lower, 3), "source": source,
                }
            else:
                advice[route] = {
                    "action": "keep",
                    "reason": (
                        f"{route} within calibrated band ({lower:.0%} lower-bound "
                        f"over {n}{tag})"
                    ),
                    "samples": n, "lower": round(lower, 3), "source": source,
                }
        return advice
    except Exception:
        return {}


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


def savings_by_source(session_id: Optional[str] = None) -> Dict[str, Dict[str, int]]:
    """Return savings aggregated by source.

    ``{source: {"count": n, "saved_tok": sum, "effective_tok": sum}}``. Lets the
    report separate *realized* PreToolUse savings (``predup`` — the denied repeat's
    exact token count is known) from *bounded* interventions (``read_guard``,
    ``grep_narrow`` — output was capped but the unbounded size, hence the exact
    saving, is unknowable, so these record a count with 0 tokens) and *theoretical*
    PostToolUse shrink (``dedup``/``truncate`` — inert on Claude Code that ignores
    ``updatedToolOutput`` for built-in tools). When *session_id* is None, sums
    across all sessions. Fail-open: returns ``{}`` on any error.
    """
    try:
        conn = db()
        sql = (
            "SELECT source, COUNT(*) AS cnt, "
            "COALESCE(SUM(saved_tok),0) AS saved_tok, "
            "COALESCE(SUM(COALESCE(effective_tok, saved_tok)),0) AS effective_tok "
            "FROM savings "
        )
        if session_id is None:
            rows = conn.execute(sql + "GROUP BY source").fetchall()
        else:
            rows = conn.execute(
                sql + "WHERE session_id=? GROUP BY source", (session_id,)
            ).fetchall()
        conn.close()
        return {
            r["source"]: {
                "count": int(r["cnt"]),
                "saved_tok": int(r["saved_tok"]),
                "effective_tok": int(r["effective_tok"]),
            }
            for r in rows
        }
    except Exception:
        return {}


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
# Retention — keep the ephemeral tables (and predup lookups) bounded
# ---------------------------------------------------------------------------

# Tables pruned by age, and the column holding their epoch-seconds timestamp.
# session_kv (flags/task) and step_ledger (resume state) are intentionally NOT
# pruned by age — they are small and semantically session-scoped.
_PRUNE_TABLES = (
    ("tool_calls", "ts"),
    ("savings", "ts"),
    ("outputs", "ts"),
    ("usage", "ts"),
    ("memo", "ts"),
    ("file_activity", "ts"),
)


def prune(retention_days: float) -> int:
    """Delete rows older than retention_days from the ephemeral tables.

    Returns the total rows removed. retention_days <= 0 is a no-op (disabled).
    Fail-open: a table that doesn't exist or any error is skipped, never raised.
    """
    if not retention_days or retention_days <= 0:
        return 0
    cutoff = time.time() - float(retention_days) * 86400.0
    removed = 0
    try:
        conn = db()
        with conn:
            for table, tscol in _PRUNE_TABLES:
                try:
                    cur = conn.execute(
                        f"DELETE FROM {table} WHERE {tscol} < ?", (cutoff,)
                    )
                    removed += int(cur.rowcount or 0)
                except sqlite3.OperationalError:
                    continue  # table absent in this db — skip
        conn.close()
    except Exception:
        return removed
    return removed


def maybe_prune() -> int:
    """Run prune() at most once per day, tracked via a global kv timestamp.

    Cheap to call on every session start: returns 0 (and does nothing) unless a
    day has elapsed since the last prune. Uses session_kv under a fixed sentinel
    session id so the throttle is global, not per real session.
    """
    try:
        cfg = get_config()
        days = float(cfg.get("retention_days", 14) or 0)
        if days <= 0:
            return 0
        last_raw = get_flag("_nexum_global", "last_prune_ts")
        now = time.time()
        if last_raw:
            try:
                if now - float(last_raw) < 86400.0:
                    return 0
            except (TypeError, ValueError):
                pass
        set_flag("_nexum_global", "last_prune_ts", str(now))
        return prune(days)
    except Exception:
        return 0


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
# Agents registry — durable state for headless-CLI agents (/nx-build)
# ---------------------------------------------------------------------------

_AGENT_COLUMNS = (
    "harness", "model", "repo_root", "worktree", "branch", "pid",
    "log_path", "task", "plan_hash", "step_index", "status", "cost_usd",
    "session_id", "tmux",
)


def record_agent(agent_id: str, **fields: Any) -> None:
    """Upsert one row in the agents registry.

    Only the fields passed as non-None overwrite the existing row's value;
    omitted/None fields preserve whatever the prior row held (mirrors
    ``record_step``'s upsert style). ``updated_ts`` is always set to now().
    ``started_ts`` is set to now() on first insert and preserved thereafter,
    unless explicitly passed (e.g. via the ``agent-set --started-ts`` CLI
    flag). Fail-open: any error is swallowed.
    """
    try:
        conn = db()
        with conn:
            prior = conn.execute(
                "SELECT * FROM agents WHERE agent_id=?", (agent_id,),
            ).fetchone()
            prior = dict(prior) if prior is not None else None
            now = time.time()

            row: Dict[str, Any] = dict(prior) if prior else {c: None for c in _AGENT_COLUMNS}
            for col in _AGENT_COLUMNS:
                value = fields.get(col)
                if value is not None:
                    row[col] = value

            started_ts = fields.get("started_ts")
            if started_ts is None:
                started_ts = (prior or {}).get("started_ts")
            if started_ts is None:
                started_ts = now

            conn.execute(
                "INSERT OR REPLACE INTO agents("
                "agent_id, harness, model, repo_root, worktree, branch, pid, "
                "log_path, task, plan_hash, step_index, status, cost_usd, "
                "session_id, tmux, started_ts, updated_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    agent_id, row["harness"], row["model"], row["repo_root"],
                    row["worktree"], row["branch"], row["pid"], row["log_path"],
                    row["task"], row["plan_hash"], row["step_index"], row["status"],
                    row["cost_usd"], row["session_id"], row["tmux"], started_ts, now,
                ),
            )
        conn.close()
    except Exception:
        pass


def delete_agent(agent_id: str) -> None:
    """Remove one agents-registry row. Fail-open."""
    try:
        conn = db()
        with conn:
            conn.execute("DELETE FROM agents WHERE agent_id=?", (agent_id,))
        conn.close()
    except Exception:
        pass


def get_agent(agent_id: str) -> Optional[Dict[str, Any]]:
    """Return one agents-registry row as a dict, or None."""
    try:
        conn = db()
        row = conn.execute(
            "SELECT * FROM agents WHERE agent_id=?", (agent_id,),
        ).fetchone()
        conn.close()
        return dict(row) if row is not None else None
    except Exception:
        return None


def _pid_alive(pid: Any) -> bool:
    """True if *pid* names a live process. Fail-open to False."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # process exists, just not ours to signal
    except OSError:
        return False
    return True


def agent_rows(repo_root: Optional[str] = None, active: bool = False) -> List[Dict[str, Any]]:
    """Return agents-registry rows, most recently updated first.

    Filtered to *repo_root* when given. When ``active`` is True, rows are
    further filtered to those whose ``pid`` names a live process. Fail-open:
    any error yields [].
    """
    try:
        conn = db()
        if repo_root:
            repo_root = os.path.realpath(repo_root)
            rows = conn.execute(
                "SELECT * FROM agents WHERE repo_root=? ORDER BY updated_ts DESC",
                (repo_root,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM agents ORDER BY updated_ts DESC",
            ).fetchall()
        conn.close()
    except Exception:
        return []

    result = [dict(r) for r in rows]
    if active:
        result = [r for r in result if r.get("pid") is not None and _pid_alive(r["pid"])]
    return result


def session_rows(repo_root: str) -> List[Dict[str, Any]]:
    """Return session summaries for sessions tagged with *repo_root*.

    Joins ``session_kv`` (key='repo_root', written by session_reset.py at
    SessionStart) with ``session_cost`` (cost/token snapshot) and the stored
    task signature, rendered to readable text via ``handoff._humanize_task``.
    Each row: {"session_id", "repo_root", "task", "cost_usd", "input_tok",
    "output_tok", "context_pct", "updated_ts"}. Fail-open: any error yields [].
    """
    try:
        conn = db()
        repo_root = os.path.realpath(repo_root)
        tagged = conn.execute(
            "SELECT session_id, value FROM session_kv WHERE key='repo_root' AND value=?",
            (repo_root,),
        ).fetchall()

        try:
            import handoff as _handoff
        except Exception:
            _handoff = None

        threshold = float(get_config().get("compaction_threshold_tokens", 120000) or 120000)

        result: List[Dict[str, Any]] = []
        for tagged_row in tagged:
            session_id = tagged_row["session_id"]
            cost_row = conn.execute(
                "SELECT cost_usd, input_tok, output_tok, updated_ts "
                "FROM session_cost WHERE session_id=?",
                (session_id,),
            ).fetchone()
            task_row = conn.execute(
                "SELECT value FROM session_kv WHERE session_id=? AND key='task'",
                (session_id,),
            ).fetchone()

            task_text = ""
            if _handoff is not None:
                try:
                    task_text = _handoff._humanize_task(task_row["value"] if task_row else None)
                except Exception:
                    task_text = ""

            input_tok = cost_row["input_tok"] if cost_row else 0
            output_tok = cost_row["output_tok"] if cost_row else 0
            context_pct = 0.0
            if threshold > 0:
                try:
                    context_pct = round(100.0 * (input_tok or 0) / threshold, 1)
                except Exception:
                    context_pct = 0.0

            result.append({
                "session_id": session_id,
                "repo_root": tagged_row["value"],
                "task": task_text,
                "cost_usd": cost_row["cost_usd"] if cost_row else 0.0,
                "input_tok": input_tok,
                "output_tok": output_tok,
                "context_pct": context_pct,
                "updated_ts": cost_row["updated_ts"] if cost_row else 0.0,
            })
        conn.close()
        return result
    except Exception:
        return []


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


def partition_steps_by_size(
    items: List[Any],
    sizes: List[int],
    max_size: int,
    max_per: int = 0,
) -> List[List[Any]]:
    """Pack ordered step indices into ordered sub-batches bounded by *both* a
    per-batch size budget (*max_size*, e.g. estimated context tokens) and an
    optional item-count cap (*max_per*), preserving order.

    Greedy and order-preserving (like ``partition_steps``): a dependent never
    lands in a batch that runs before its prerequisite's. A batch is closed
    before adding an item when, with the batch already non-empty, adding that
    item would exceed *max_size*, or the batch already holds *max_per* items.
    A single item larger than *max_size* gets its own batch (a step is never
    split). ``max_size <= 0`` disables the size bound (falls back to the
    count-only cap). If *sizes* doesn't line up with *items*, falls back to
    ``partition_steps`` (count-only) so the caller never crashes.
    """
    items = list(items)
    sizes = list(sizes)
    if len(sizes) != len(items):
        return partition_steps(items, max_per)
    if not items:
        return []

    batches: List[List[Any]] = []
    cur: List[Any] = []
    cur_size = 0
    for it, sz in zip(items, sizes):
        sz = max(0, int(sz))
        over_size = max_size and max_size > 0 and cur and (cur_size + sz) > max_size
        over_count = max_per and max_per > 0 and cur and len(cur) >= max_per
        if over_size or over_count:
            batches.append(cur)
            cur = []
            cur_size = 0
        cur.append(it)
        cur_size += sz
    if cur:
        batches.append(cur)
    return batches


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


def _cmd_prune(args) -> None:
    """Prune aged rows. With --days, use that; otherwise the configured value."""
    if args.days is not None:
        removed = prune(args.days)
    else:
        removed = prune(float(get_config().get("retention_days", 14) or 0))
    print(json.dumps({"removed": removed}, sort_keys=True))


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


def _cmd_record_usage(args) -> None:
    """Append a usage row (estimated per-tier attribution recorded by /nx-build)."""
    add_usage(
        session_id=args.session,
        model=args.model,
        input_tok=args.input_tok,
        output_tok=args.output_tok,
        cache_read_tok=args.cache_read_tok,
    )
    print(json.dumps({"ok": True}, sort_keys=True))


def _cmd_calib_record(args) -> None:
    """Incrementally upsert per-repo, per-route calibration counters."""
    record_calibration(
        args.repo,
        args.route,
        dispatched=args.dispatched,
        passed_first_try=args.passed_first_try,
        escalated=args.escalated,
    )
    print(json.dumps({"ok": True}, sort_keys=True))


def _cmd_calib_list(args) -> None:
    """Print all calibration rows for a repo as a JSON array."""
    print(json.dumps(calibration_rows(args.repo), sort_keys=True))


def _cmd_calib_advice(args) -> None:
    """Print per-route routing advice (up/down/keep) for a repo as JSON."""
    print(json.dumps(calibration_advice(args.repo), sort_keys=True))


def _cmd_agent_set(args) -> None:
    """Upsert one agent registry row."""
    record_agent(
        args.id,
        harness=args.harness,
        model=args.model,
        repo_root=args.repo,
        worktree=args.worktree,
        branch=args.branch,
        pid=args.pid,
        log_path=args.log_path,
        task=args.task,
        plan_hash=args.plan_hash,
        step_index=args.step_index,
        status=args.status,
        cost_usd=args.cost_usd,
        session_id=args.session_id,
        tmux=args.tmux,
        started_ts=args.started_ts,
    )
    print(json.dumps({"ok": True, "agent_id": args.id}))


def _cmd_agent_get(args) -> None:
    """Print one agent row as JSON (null if absent)."""
    print(json.dumps(get_agent(args.id)))


def _cmd_agent_del(args) -> None:
    """Delete one agent registry row."""
    delete_agent(args.id)
    print(json.dumps({"ok": True, "agent_id": args.id}))


def _cmd_agent_list(args) -> None:
    """Print agent rows as a JSON array, optionally filtered to repo/active."""
    print(json.dumps(agent_rows(repo_root=args.repo, active=args.active)))


def _cmd_session_list(args) -> None:
    """Print repo-scoped session summaries as a JSON array."""
    print(json.dumps(session_rows(args.repo)))


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

    p_usage = sub.add_parser("record-usage", help="Append an estimated usage row.")
    p_usage.add_argument("--session", required=True)
    p_usage.add_argument("--model", required=True)
    p_usage.add_argument("--input-tok", type=int, required=True)
    p_usage.add_argument("--output-tok", type=int, required=True)
    p_usage.add_argument("--cache-read-tok", type=int, default=0)

    p_calib = sub.add_parser("calib-record", help="Increment per-repo route calibration counters.")
    p_calib.add_argument("--repo", required=True)
    p_calib.add_argument("--route", required=True)
    p_calib.add_argument("--dispatched", type=int, default=0)
    p_calib.add_argument("--passed-first-try", type=int, default=0)
    p_calib.add_argument("--escalated", type=int, default=0)

    p_prune = sub.add_parser("prune", help="Delete aged rows from the ephemeral tables.")
    p_prune.add_argument("--days", type=float, default=None,
                         help="Override retention_days; omit to use config.")

    p_clist = sub.add_parser("calib-list", help="Print calibration rows for a repo as JSON.")
    p_clist.add_argument("--repo", required=True)

    p_cadv = sub.add_parser(
        "calib-advice",
        help="Print per-route routing advice (up/down/keep, JSON) for a repo.",
    )
    p_cadv.add_argument("--repo", required=True)

    p_aset = sub.add_parser("agent-set", help="Upsert one agent registry row.")
    p_aset.add_argument("--id", required=True, dest="id")
    p_aset.add_argument("--harness")
    p_aset.add_argument("--model")
    p_aset.add_argument("--repo")
    p_aset.add_argument("--worktree")
    p_aset.add_argument("--branch")
    p_aset.add_argument("--pid", type=int)
    p_aset.add_argument("--log-path")
    p_aset.add_argument("--task")
    p_aset.add_argument("--plan-hash")
    p_aset.add_argument("--step-index", type=int)
    p_aset.add_argument("--status")
    p_aset.add_argument("--cost-usd", type=float)
    p_aset.add_argument("--session-id")
    p_aset.add_argument("--tmux")
    p_aset.add_argument("--started-ts", type=float)

    p_aget = sub.add_parser("agent-get", help="Print one agent row as JSON.")
    p_aget.add_argument("--id", required=True, dest="id")

    p_adel = sub.add_parser("agent-del", help="Delete one agent registry row.")
    p_adel.add_argument("--id", required=True, dest="id")

    p_alist = sub.add_parser("agent-list", help="Print agent rows as a JSON array.")
    p_alist.add_argument("--repo", default=None)
    p_alist.add_argument("--active", action="store_true")
    p_alist.add_argument("--json", action="store_true", help="Accepted for symmetry; output is always JSON.")

    p_slist = sub.add_parser("session-list", help="Print repo-scoped session summaries as JSON.")
    p_slist.add_argument("--repo", required=True)
    p_slist.add_argument("--json", action="store_true", help="Accepted for symmetry; output is always JSON.")

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
    elif args.command == "record-usage":
        _cmd_record_usage(args)
    elif args.command == "calib-record":
        _cmd_calib_record(args)
    elif args.command == "calib-list":
        _cmd_calib_list(args)
    elif args.command == "calib-advice":
        _cmd_calib_advice(args)
    elif args.command == "prune":
        _cmd_prune(args)
    elif args.command == "agent-set":
        _cmd_agent_set(args)
    elif args.command == "agent-get":
        _cmd_agent_get(args)
    elif args.command == "agent-del":
        _cmd_agent_del(args)
    elif args.command == "agent-list":
        _cmd_agent_list(args)
    elif args.command == "session-list":
        _cmd_session_list(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
