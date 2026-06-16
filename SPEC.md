# Nexum — Build Specification

A Claude Code **plugin** that cuts (a) context tokens and (b) model cost during
Claude Code sessions. This spec is written to be implemented by Sonnet/Haiku
subagents: every file has an exact contract, edge cases, and acceptance criteria.
**If anything here is ambiguous, STOP and ask — do not invent behavior.**

---

## 0. Global decisions (RESOLVED — do not revisit)

- **Language:** Python 3.9+ (target stdlib available on 3.9–3.14). **Stdlib only** —
  allowed imports: `json, sqlite3, hashlib, os, sys, re, subprocess, pathlib, time,
  fnmatch, argparse, dataclasses, typing`. **No pip installs. No third-party libs.**
- **All hook scripts** are invoked as `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/<x>.py`,
  read a single JSON object from **stdin**, and write a single JSON object to
  **stdout** (the Claude Code hook contract). They must **never** crash the session:
  wrap everything in try/except; on any internal error, print `{}` to stdout and
  exit 0 (fail-open — never block the user because nexum broke).
- **Determinism:** any JSON we emit uses `json.dumps(obj, sort_keys=True)`. Never put
  timestamps/UUIDs into content that feeds a model prefix.
- **State location:** all persistent state lives under the directory in env var
  `CLAUDE_PLUGIN_DATA` if set, else `${CLAUDE_PLUGIN_ROOT}/.nexum-data`, else
  `./.nexum-data`. Single SQLite file: `<data_dir>/nexum.db`. Resolve this in
  `store.py` via `nexum_data_dir()`; every script imports it from there.
- **Session id:** read from hook input field `session_id`; if absent use the string
  `"_nosession"`. All per-session rows are keyed by it.
- **Config:** optional JSON file at `<data_dir>/config.json`; `store.py` exposes
  `get_config()` returning defaults merged with that file. Defaults are listed in §2.
- **Pricing table (USD per 1M tokens)** lives in `scripts/store.py` as a constant
  `PRICING` and is the single source of truth:
  `{"opus": (5.0, 25.0), "sonnet": (3.0, 15.0), "haiku": (1.0, 5.0)}`
  (tuple = (input, output)). Cache read ≈ 0.1× input; cache write ≈ 1.25× input.
- **Tone of user-facing messages:** one short factual sentence, no emoji, prefixed
  `[nexum] `.

---

## 1. Plugin packaging

```
nexum/
  .claude-plugin/plugin.json
  hooks/hooks.json
  commands/  nexum-plan.md  nexum-implement.md  nexum-audit.md
  agents/    nexum-impl-haiku.md  nexum-impl-sonnet.md  nexum-reviewer.md
  scripts/   store.py truncate.py dedup.py scan_guard.py context_watch.py
             guardrail.py cost_report.py audit.py
  tests/     test_*.py
  SPEC.md  README.md
```

### `.claude-plugin/plugin.json`
```json
{
  "name": "nexum",
  "version": "0.1.0",
  "description": "Context-token and model-cost optimization for Claude Code.",
  "author": "rahul.1992.tyagi@gmail.com"
}
```

### `hooks/hooks.json` — wiring (exact shape)
```json
{
  "hooks": {
    "PostToolUse": [
      { "matcher": "Read|Bash|Grep|Glob",
        "hooks": [
          { "type": "command", "command": "python3 ${CLAUDE_PLUGIN_ROOT}/scripts/truncate.py", "timeout": 10 },
          { "type": "command", "command": "python3 ${CLAUDE_PLUGIN_ROOT}/scripts/dedup.py", "timeout": 10 }
        ] }
    ],
    "PreToolUse": [
      { "matcher": "Bash|Grep|Glob|Read",
        "hooks": [ { "type": "command", "command": "python3 ${CLAUDE_PLUGIN_ROOT}/scripts/scan_guard.py", "timeout": 10 } ] }
    ],
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command", "command": "python3 ${CLAUDE_PLUGIN_ROOT}/scripts/context_watch.py", "timeout": 10 } ] }
    ]
  }
}
```
> EDGE CASE: truncate runs before dedup so dedup hashes the already-truncated text.
> If `updatedToolOutput` from truncate must be visible to dedup, they cannot see
> each other's output (separate processes). RESOLUTION: each reads the **original**
> tool output from its own stdin; truncate's job is shrink, dedup's job is the
> pointer-collapse, and **dedup must re-apply truncation logic by importing
> `truncate.shrink()`** so the final text it emits is both truncated and deduped.
> Net rule: dedup is the authority on the final `updatedToolOutput`; truncate is a
> fallback for tools dedup doesn't handle. Keep both wired; dedup wins when it acts.

---

## 2. `scripts/store.py` — foundation (build FIRST; everything imports it)  · TIER: standard (Sonnet)

Pure module + tiny CLI. No hook contract. Owns the SQLite schema and all shared
helpers.

**Public API (exact signatures):**
```python
def nexum_data_dir() -> str            # resolves + creates the data dir
def db() -> sqlite3.Connection         # opens nexum.db, runs migrations, returns conn (WAL mode)
def get_config() -> dict               # defaults merged with config.json
def sha256(text: str) -> str
def estimate_tokens(text: str) -> int  # cheap heuristic: max(1, len(text)//4)

# dedup / memo
def seen_output(session_id, content_hash) -> dict | None   # row or None
def record_output(session_id, tool_name, content_hash, summary, token_count) -> None
def memo_get(input_hash) -> str | None
def memo_put(input_hash, output_text) -> None

# session task + flags (for context_watch)
def get_session_task(session_id) -> str | None
def set_session_task(session_id, summary) -> None
def get_flag(session_id, key) -> str | None
def set_flag(session_id, key, value) -> None

# metrics
def add_usage(session_id, model, input_tok, output_tok, cache_read_tok=0) -> None
def usage_rows(session_id=None) -> list[dict]
```

**Schema (create if not exists):**
```sql
CREATE TABLE outputs(   session_id TEXT, content_hash TEXT, tool_name TEXT,
                        summary TEXT, token_count INTEGER, ts REAL,
                        PRIMARY KEY(session_id, content_hash));
CREATE TABLE memo(      input_hash TEXT PRIMARY KEY, output_text TEXT, ts REAL);
CREATE TABLE session_kv(session_id TEXT, key TEXT, value TEXT,
                        PRIMARY KEY(session_id, key));
CREATE TABLE usage(     session_id TEXT, model TEXT, input_tok INTEGER,
                        output_tok INTEGER, cache_read_tok INTEGER, ts REAL);
```
`get_session_task`/`set_session_task` use `session_kv` with `key='task'`.

**Config defaults (`get_config()`):**
```json
{
  "truncate_max_lines": 200,
  "truncate_head_lines": 120,
  "truncate_tail_lines": 60,
  "truncate_min_lines_to_act": 240,
  "keep_error_regex": "(?i)(error|exception|traceback|failed|fatal|warning)",
  "compaction_threshold_tokens": 120000,
  "scan_guard_enabled": true,
  "scan_deny_paths": ["node_modules", ".git", "dist", "build", "target", "vendor",
                      ".next", "coverage", ".venv", "__pycache__"],
  "intent_guard_enabled": true,
  "intent_similarity_threshold": 0.25,
  "statusline_compaction_warn_pct": 80,
  "statusline_compaction_warn_tokens": 80000,
  "dedup_cache_weight": 0.1,
  "dispatch_granularity": "group",
  "max_same_tier_retries": 1
}
```

**EDGE CASES:** concurrent hook processes hitting SQLite → open with
`timeout=5`, `PRAGMA journal_mode=WAL`, retry once on `OperationalError`.
Corrupt/locked db → `db()` must fall back to an in-memory connection so callers
never raise. CLI: `python3 store.py init` creates the db; `python3 store.py
config` prints effective config.

**ACCEPTANCE:** `import store; store.db()` creates `nexum.db`; round-trip every
helper; two processes writing concurrently don't error; missing `CLAUDE_PLUGIN_DATA`
falls back correctly.

---

## 3. Pillar 1 — context-savings hooks

### 3.1 `scripts/truncate.py` · TIER: mechanical (Haiku)
- Exposes `shrink(text: str, cfg: dict) -> tuple[str, bool]` (returns new text +
  whether it acted) so `dedup.py` can reuse it. Pure function, fully unit-testable.
- Hook `main()`: read stdin JSON; the tool output is at
  `tool_response` (string) OR `tool_response.stdout`/`.content` depending on tool —
  RESOLUTION: implement `extract_output(data) -> str|None` that checks, in order,
  `data["tool_response"]` (if str), `data["tool_response"]["stdout"]`,
  `["content"]`, `["output"]`; if none/str-empty → emit `{}` exit 0.
- `shrink` logic: split into lines. If `len(lines) <= truncate_min_lines_to_act`,
  return `(text, False)`. Else keep first `head` + last `tail` lines, PLUS any line
  matching `keep_error_regex` from the middle (cap extra at 40 lines), joined with a
  marker line: `... [nexum] omitted N lines ...`. Preserve original order.
- Emit:
  ```json
  {"hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": "<shrunk>"}}
  ```
  Only when it acted; otherwise `{}`.
- **EDGE CASES:** binary/no-newline blobs (treat as 1 line → if huge, hard-cut to
  first+last N chars); text already small → no-op; non-UTF8 → `errors="replace"`.
- **ACCEPTANCE:** unit tests for: no-op under threshold; head+tail+error-line
  retention; omitted-count correct; emits valid JSON; never raises on weird input.

### 3.2 `scripts/dedup.py` · TIER: standard (Sonnet)
- `main()`: extract output (reuse `truncate.extract_output`); compute
  `h = store.sha256(output)`. If `store.seen_output(session_id, h)` exists → emit a
  pointer instead of the body:
  `updatedToolOutput = "[nexum] identical to earlier <tool_name> output (hash <h[:8]>) — omitted to save context."`
  Else: `shrunk,_ = truncate.shrink(output, cfg)`; `store.record_output(...)` with a
  short summary (first line + token estimate); emit `updatedToolOutput=shrunk`.
- **Only dedup outputs ≥ 30 lines or ≥ 2000 chars** (don't pointer-collapse tiny
  results — config-gate via reusing truncate threshold).
- **EDGE CASES:** the SAME large file legitimately re-read after edits → hash
  differs (content changed) so it is NOT collapsed (correct). Empty output → `{}`.
- **ACCEPTANCE:** first occurrence stored + shrunk; exact repeat → pointer, no body;
  changed content → not collapsed; tiny output → untouched.

---

## 4. Pillar 2 — cost-driven planner → executor workflow

### 4.1 `commands/nexum-plan.md` · TIER: standard
Frontmatter `model: opus` (or `inherit`). Instructs the model to produce a plan
file at `<data_dir>/plan/<session>.md` whose steps follow the **Step Schema**
below. The command body is the prompt; no script needed beyond writing the file via
the model's normal tools.

**Step Schema (every step MUST have all fields):**
```
### Step N: <title>
- route: mechanical | standard | needs-strong
- files: <explicit paths to read/create/edit>
- objective: <this step only>
- contract: <signatures / interfaces / output shape the rest depends on>
- scope: do NOT touch <explicit out-of-bounds>
- acceptance: <a runnable check — a test/command/assertion>
```
Routing rubric (state it in the command body): mechanical→Haiku (boilerplate,
mechanical refactor, test scaffold, well-specified single-file CRUD, *with a test*);
standard→Sonnet (default); needs-strong→Opus (architecture, ambiguity, cross-cutting,
debugging).

### 4.2 `agents/nexum-impl-haiku.md` (`model: haiku`), `agents/nexum-impl-sonnet.md` (`model: sonnet`), `agents/nexum-impl-opus.md` (`model: opus`), `agents/nexum-reviewer.md` (`model: sonnet`) · TIER: mechanical
Subagent definitions. The three executors (one per tier) take **one step or a
batch of steps** and implement them in a single warm context. Each executor runs
`guardrail.py` itself as its final action per step and returns the **verbatim
guardrail JSON** per step (orchestrator parses it — no separate guardrail
round-trip). Reviewer takes a step + the produced diff and verifies it against
`contract`/`scope`/`acceptance`, returning PASS/FAIL — invoked **selectively**
(escalation, `needs-strong`, or many-file steps), not after every step. Each
file: frontmatter (`name`, `description`, `model`) + a body stating the
implement-only-listed-steps / stay-in-scope / run-guardrail-and-return-JSON
contract.

### 4.3 `commands/nexum-implement.md` · TIER: standard
Body orchestrates (cost levers in order of impact):
- **Group by route** (`mechanical`→`standard`→`needs-strong`) to keep each
  model's cache warm.
- **Batch by tier** (`dispatch_granularity: group`, default): send a whole route
  group to ONE executor dispatch — shared spec/files read once, one cached
  prefix — instead of one cold-start dispatch per step. `step` granularity is
  the opt-in per-step mode.
- **Skip the spawn when the step's tier == the current session model** —
  implement inline rather than spawning, since a subagent only earns its keep by
  running a *different* model without trashing the main cache.
- **Cheap orchestrator:** orchestration does not require Opus; only `needs-strong`
  *content* is delegated to `nexum-impl-opus`.
- Build each delegation **shared context first, steps last** (stable-prefix-first).
- **Executors self-run the guardrail**; the orchestrator reads the returned JSON
  and only spot-checks implausible passes.
- On FAIL: retry same tier up to `max_same_tier_retries` (default 1) **handing the
  failed diff back to patch** (not reimplement), then escalate
  haiku→sonnet→opus once; never demote a `needs-strong` step.
Prompt-driven (uses the Task/subagent mechanism), referencing the agents in 4.2.

### 4.4 `scripts/guardrail.py` · TIER: standard
- CLI: `python3 guardrail.py --acceptance "<cmd>" --scope-root <dir> --changed <f1,f2,...>`
- Runs the acceptance command via `subprocess` (capture rc/stdout/stderr, timeout
  120s). Computes **scope-diff**: any changed file NOT under an allowed scope path →
  violation. Output JSON `{"pass": bool, "acceptance_rc": int, "scope_violations": [...], "log": "<tail>"}`.
- **EDGE CASES:** no acceptance cmd given → pass=true with note; acceptance times
  out → pass=false rc=124; scope empty → no scope check.
- **ACCEPTANCE:** passing cmd → pass true; failing cmd → false; out-of-scope file →
  violation listed.

### 4.5 `scripts/cost_report.py` · TIER: standard
- CLI: `python3 cost_report.py [--session <id>]`. Reads `usage` rows; computes
  actual $ (PRICING) and an **all-opus baseline** $ (same tokens priced at opus),
  reports `$ saved`, per-model breakdown, and **token yield** if shipped-token data
  is recorded (else note "yield needs shipped-token tagging"). Pure read + print.
- Usage data sources: (1) per-call `usage` rows written via `store.add_usage`
  (tiering breakdown); (2) the **`session_cost` snapshot** written by the
  statusLine via `store.upsert_session_cost` — Claude Code's own metered,
  cache-accurate total (`cost.total_cost_usd` + cumulative tokens). On API-key
  billing the snapshot is the number that matches the invoice; it captures the
  prompt-cache writes/reads a token-count reconstruction cannot see. A full OTel
  collector remains out of scope — the statusLine snapshot is the reliable
  stdlib-only spend signal.
- **Cache-aware savings:** dedup pointer-collapses are weighted by
  `dedup_cache_weight` (default 0.1) because a repeated read would bill at the
  cache-read rate, not full price; truncation of fresh output stays at full
  weight. `record_saving` stores raw + effective tokens; `session_savings` sums
  effective. This is a prompt-cache invariant — hooks must rewrite tool output
  **deterministically** (see `tests/test_determinism.py`) or they invalidate the
  cached prefix and cost more than they save.
- **ACCEPTANCE:** with seeded usage rows, prints correct actual vs baseline and
  savings.

---

## 5. Pillar 3 — lifecycle & hygiene guards

### 5.1 `scripts/scan_guard.py` (PreToolUse) · TIER: standard
- Read stdin; `tool_name = data["tool_name"]`, input at `data["tool_input"]`.
- Detect context-blowing scans:
  - **Bash:** command string matches an unscoped recursive search:
    `grep -r`/`grep -R`/`rg` with no path arg or path `.`/`/`; `find /` or `find .`
    with no `-maxdepth`/`-path` filter and no prune; `cat`/`ls -R` over a deny path.
  - **Grep/Glob:** `path` missing or repo-root AND `glob`/`pattern` very broad
    (`**/*` or `*`); OR path under a `scan_deny_paths` entry.
  - **Read:** `file_path` under a `scan_deny_paths` entry.
- Action: emit
  `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"[nexum] <why> — scope the search to a directory or add -maxdepth/path."}}`.
  For Bash where a safe narrowing exists, prefer `updatedInput` to inject a path/
  limit instead of denying (only when unambiguous; else deny).
- Respect `scan_guard_enabled`; fail-open on any error.
- **EDGE CASES:** legitimately scoped commands must pass untouched; a command that
  already has `-maxdepth` or an explicit non-root path → allow; never deny a plain
  `Read` of a normal source file. False-positive risk is high — be conservative;
  when unsure, ALLOW.
- **ACCEPTANCE:** `grep -r foo` → deny; `grep -r foo src/` → allow; `Read node_modules/x`
  → deny; `Read src/app.py` → allow; disabled flag → always allow.

### 5.2 `scripts/context_watch.py` (UserPromptSubmit) · TIER: standard
Two responsibilities in one hook:
- **(a) Compaction prompt:** maintain a running token estimate per session in
  `session_kv` (add this turn's prompt estimate + accumulated tool tokens recorded
  by dedup). When it crosses `compaction_threshold_tokens` AND not already prompted
  this window (flag), emit a `systemMessage`:
  `"[nexum] Context is large (~Xk tokens). Consider /compact to reduce cost."`
  (systemMessage only — do NOT block.)
- **(b) Intent-change guard:** `prompt = data["prompt"]`. Derive a keyword/topic
  signature (lowercased word set minus stopwords; plus task-type keywords:
  fix/bug/error→"fix", add/implement/feature/new→"feature", refactor, test, docs).
  Compare to stored session task signature via Jaccard similarity. If a stored task
  exists AND task-type changed (e.g. fix→feature) AND similarity <
  `intent_similarity_threshold` AND no `bypass_intent` flag set:
  emit
  `{"decision":"block","reason":"[nexum] This looks like a new task (<old>→<new>). A fresh session gives cleaner, cheaper context. Reply 'continue' to proceed here."}`
  and set nothing yet. If the prompt is exactly `continue` (case-insensitive,
  trimmed) → set `bypass_intent` flag and allow (`{}`). On first prompt of a session
  (no stored task) → store signature, allow.
- Respect `intent_guard_enabled`. Always update the stored task signature AFTER a
  non-blocked prompt.
- **EDGE CASES:** empty prompt → allow; `continue` bypass must clear so the new task
  becomes the session task; never block twice in a row for the same divergence.
- **ACCEPTANCE:** first prompt allowed + stored; same-topic follow-up allowed;
  fix→feature divergence blocked; `continue` bypasses then adopts new task;
  threshold crossing emits systemMessage exactly once per window.

### 5.3 `scripts/audit.py` + `commands/nexum-audit.md` · TIER: standard
- CLI `python3 audit.py [--root <dir>] [--write]`. Scans `--root` (default cwd):
  - **Ignore mechanism:** detect which Claude Code ignore the repo uses. v1 targets,
    in priority: a `.claudeignore` file if present; else `.gitignore`; report which.
    ⚠️ At build time, VERIFY the current Claude Code ignore filename/behavior; if it
    differs, target the real one. Keep the detection in one function `ignore_files()`.
  - Findings: (1) no ignore file at all; (2) noise dirs that EXIST on disk but are
    not matched by any ignore pattern (use `scan_deny_paths` + common set);
    (3) entries in `.gitignore` not covered by the Claude ignore (reconcile);
    (4) files > 5 MB or binary likely to blow context if read.
  - Output: human report to stdout. With `--write`: append a `# nexum` block of
    suggested patterns to the chosen ignore file (idempotent — skip patterns already
    present; never duplicate; never delete existing lines).
- `commands/nexum-audit.md`: frontmatter `model: haiku` (cheap); body runs
  `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/audit.py` and summarizes, offering `--write`.
- **EDGE CASES:** empty repo → "clean" report; existing `# nexum` block → update in
  place, no dupes; symlinks → don't follow into deny dirs; permission errors → skip
  file, continue.
- **ACCEPTANCE:** repo with unignored `node_modules` → flagged; `--write` adds it
  once and is a no-op on second run; missing ignore file → recommends creating one.

---

## 6. Build order & dispatch plan (who builds what)

1. **store.py** — Sonnet. Foundation; blocks everything. Build + test first.
2. Parallel after store: **truncate.py** (Haiku), **dedup.py** (Sonnet),
   **scan_guard.py** (Sonnet), **context_watch.py** (Sonnet), **guardrail.py**
   (Sonnet), **cost_report.py** (Sonnet), **audit.py** (Sonnet).
3. Parallel, no deps: **plugin.json** (Haiku), **hooks.json** (Haiku),
   **commands/*.md** (Sonnet), **agents/*.md** (Haiku), **README.md** (Haiku).
4. **tests/** — Sonnet, after the modules they cover exist.
5. **Reviewer pass** (Sonnet) against each file's ACCEPTANCE list; fix or escalate.

Each dispatched task gets: this SPEC.md path, the one file to build, its §, and the
instruction "implement ONLY this file; stdlib only; fail-open; match the acceptance
criteria; do not edit other files."

## 7. Definition of done
- `python3 -m pytest tests/` (or stdlib `unittest`) green. Tests use only stdlib.
- Every hook script: piping a representative JSON on stdin returns valid JSON and
  exit 0, including on malformed input (fail-open).
- Plugin loads in Claude Code (`hooks.json`/`plugin.json` valid); manual smoke per
  §verification in the design notes.
- No third-party imports anywhere (`grep -rn "import " scripts/` review).
