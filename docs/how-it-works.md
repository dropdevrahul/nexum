# How it works

## Architecture

nexum is a Claude Code plugin written in Python 3.9+ using only the standard library. No pip installs. All runtime scripts live under `scripts/` and are invoked as hooks by the Claude Code harness. Hooks must never crash the session: every script wraps its logic in try/except and emits `{}` on any internal error, exiting with code 0 (fail-open).

Persistent state (dedup memo, usage metrics, task history, session flags) lives in a single SQLite file at `<data_dir>/nexum.db`, opened in WAL mode so concurrent hook processes don't block each other. All JSON emission uses `json.dumps(obj, sort_keys=True)` for determinism — non-deterministic output would invalidate the prompt cache and cost more than it saves.

## Context levers: what works today

Claude Code's hook contract distinguishes PreToolUse from PostToolUse. Not all hook outputs are honored equally.

### Working (PreToolUse is honored)

**Read-guard** (`scan_guard.py`, `_read_limit_input`) — when a file exceeds `read_guard_min_bytes` (default 262,144 bytes) and the Read call has no explicit `limit` already set, nexum injects a line limit via `updatedInput`. This is the reliable context-saving path for large file reads.

Configure via `config.json`:

```json
{
  "read_guard_enabled": true,
  "read_guard_min_bytes": 262144,
  "read_guard_inject_lines": 2000
}
```

**Scan-guard** (`scan_guard.py`) — reads into deny paths, recursive searches over deny paths, and unscoped `find`/`ls -R` are blocked via `permissionDecision: deny` before the tool executes. The blocked call never reaches the model.

**Grep narrowing** (`scan_guard.py`, when `grep_narrow_enabled`) — rather than denying a broad/unscoped *search* (a result-volume problem, not a noisy-directory one), nexum caps its output: it injects `head_limit` into the `Grep` tool and appends `| head -n grep_head_limit` to an unscoped recursive Bash `grep`/`rg`, via `updatedInput`. The model still gets a bounded answer without a retry round-trip. This is a *working* PreToolUse lever (unlike PostToolUse shrink). A search that already pipes, targets a deny path, or already sets `head_limit` is left to the deny path instead.

**Pre-emptive dedup** (`scripts/predup.py`) — denies an identical repeated `Read`, `Grep`, or `Glob` call (and optionally read-only `Bash`) that was already executed in the same session. For `Read` calls an mtime guard is applied first: if the file has changed since the first call, the repeat is allowed through. Because a PreToolUse `deny` is actually honored, the avoided re-injection is a real saving that moves the `saved` figure in the status line.

Configure via `config.json`:

```json
{
  "predup_enabled": true,
  "predup_decision": "deny",
  "predup_bash_readonly": false
}
```

Set `predup_decision` to `"ask"` to prompt instead of silently denying. Set `predup_bash_readonly` to `true` to also cover read-only Bash commands.

### Pending / self-test-gated (PostToolUse is currently ignored)

PostToolUse `updatedToolOutput` is silently ignored for built-in tools on current Claude Code (see anthropics/claude-code [#65403](https://github.com/anthropics/claude-code/issues/65403) and [#32105](https://github.com/anthropics/claude-code/issues/32105)). As a result, the output truncation (`truncate.py`) and dedup pointer-collapse (`dedup.py`) hooks emit replacements that the harness does not apply.

nexum performs a per-session self-test to detect whether the harness honors `updatedToolOutput`. Savings from truncation and dedup are only counted in the status line and cost report after the self-test confirms the field is being applied. If the upstream issue is fixed, the self-test passes and savings are counted automatically — no config change required.

## Session resume nudge

`scripts/resume_nudge.py` runs as a `SessionStart` hook. When a recent handoff exists in the nexum data directory for the current branch, it surfaces a one-line hint in the session context:

```
[nexum] Resume available: a handoff for branch 'my-branch' was written 2026-06-18T10:00:00+00:00 — run /nx-load to continue. (Not loaded automatically.)
```

The nudge is skipped for continued (resumed) or compacted sessions, and checks that the handoff was written within `resume_nudge_max_age_hours` (default 24). Nothing is loaded automatically — the user must run `/nx-load` explicitly.

Configure via `config.json`:

```json
{
  "resume_nudge_enabled": true,
  "resume_nudge_max_age_hours": 24
}
```

## SQLite state

All persistent state is stored in `<data_dir>/nexum.db`. The schema includes:

- `outputs` — dedup memo: hashes of tool outputs seen this session, keyed by `(session_id, content_hash)`.
- `memo` — short-lived memoization of expensive computations.
- `session_kv` — per-session key/value flags (task signature, bypass flags, context-size estimates).
- `usage` — per-call token counts and model tier, used to compute the cost report.

Inspect the effective configuration at any time with:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/store.py config
```
