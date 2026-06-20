# Changelog

All notable changes to nexum are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] - 2026-06-18
### Added
- **Estimated per-tier usage recording in `/nx-build`** so the cost report breakdown is populated with token estimates per execution tier, giving visibility into where the session's cost is incurred.
- **Per-repo routing calibration** that biases `/nx-plan` from historical step outcomes, routing subsequent steps based on learned success rates and execution patterns rather than static tier assignments.
- **Throttled SessionStart audit nudge** (`scripts/audit_nudge.py`, SessionStart hook) that checks ignore-config rot on session start without spamming the user, surfacing a one-line reminder when the ignore config has actionable findings (missing ignore file, unignored noise dirs, or large/binary files). Throttled to once per repo per `audit_nudge_throttle_hours`. Config: `audit_nudge_enabled` (default true), `audit_nudge_throttle_hours` (default 24).

### Changed
- **`max_steps_per_dispatch` default lowered 6 → 4.** A 6-step grouped Sonnet dispatch stalled the stream watchdog mid-batch (600s no-progress) and lost the whole batch; smaller batches bound both the per-dispatch context and a single failure's blast radius.
- **Size-aware dispatch batching.** `/nx-build` now partitions a route tier with `plan_preview.py --plan … --indices …`, which estimates each step's context load (per-step base + declared-file bytes ÷ 4) and bounds each sub-batch by **both** `max_dispatch_context_tokens` (default 50000) and the `max_steps_per_dispatch` count cap — so a few large-file steps split off while a single over-budget step still dispatches alone (steps are never split). New `store.partition_steps_by_size`; config `max_dispatch_context_tokens`, `dispatch_step_base_tokens`. The count-only `store.py plan-batches` helper remains for when no plan file is available.

### Fixed
- **predup freshness guard.** A recorded `tool_calls` row only proves an output was injected once, not that it is still in the live context — subagents share the parent's DB, and compaction/resume evicts output while the row persists, so predup could deny a legitimate read whose content was no longer (or never) in context. predup now lets a call through when its prior row is older than `predup_max_age_seconds` (default 900; 0 restores the prior ever-recorded behaviour).

## [0.3.0] - 2026-06-18
### Added
- **Pre-emptive dedup hook** (`scripts/predup.py`, PreToolUse). Denies an identical repeated `Read`, `Grep`, or `Glob` call that already ran in the same session, with an mtime guard for `Read` to allow re-reads of changed files. Unlike the PostToolUse dedup (currently inert), a PreToolUse `deny` is actually honored by Claude Code, so the avoided re-injection is a real saving — the `saved` figure in the status line now moves on deduped calls. Config: `predup_enabled` (default true), `predup_decision` (`deny` | `ask`, default `deny`), `predup_bash_readonly` (extend coverage to read-only Bash; default false).
- **`/nx-build` plan cost preview** (`scripts/plan_preview.py`). Before dispatching any steps, `/nx-build` prints a projected cost table — steps per tier, estimated input/output tokens, actual cost, and all-opus baseline — so the user sees the projected savings before execution begins. Numbers are a per-step token heuristic; authoritative post-run totals still come from the §10 cost report. Config: `plan_preview_enabled` (default true).
- **Session resume nudge** (`scripts/resume_nudge.py`, SessionStart hook). On each new session start, if a recent handoff for the current branch exists, a one-line hint is surfaced: "Resume available — run /nx-load to continue." Nothing is loaded automatically. Skipped for resumed or compacted sessions and for handoffs older than `resume_nudge_max_age_hours`. Config: `resume_nudge_enabled` (default true), `resume_nudge_max_age_hours` (default 24).
- **Orchestrator step-ledger resume.** `/nx-build` persists each step's verdict to a new `step_ledger` table (keyed by session + plan hash) and, on re-run for the same plan, skips `done` steps and patch-retries `failed` ones from their saved diff — so a session that dies mid-plan resumes instead of redoing completed work. A resumed failed step continues the escalation ladder from its persisted `attempts`/`tier_used`. New `store.py` CLI: `plan-hash`, `step-set/get/list/clear`. Config `orchestrator_resume_enabled` (default true).
- **Code-enforced dispatch batch cap.** `store.partition_steps` + CLI `plan-batches` split a route tier into deterministic, order-preserving sub-batches of at most `max_steps_per_dispatch` (default 6), so one dispatch can't overflow an executor's context. Executors return verdict-only on PASS (+ the diff on FAIL) to keep the orchestrator's shared context small.
- **Auto-handoff write + `/nx-load` resume.** Past `handoff_threshold_tokens` (default 100k), `context_watch` auto-writes a deterministic handoff skeleton (git state + task + tokens, via `handoff.py`) to `handoff/latest.md` every prompt — guaranteed even if the session dies. The trigger uses Claude Code's REAL context size (the statusline persists `real_context_tokens` from `context_window`), not a prompt-text estimate. A fresh session resumes on demand with `/nx-load` (resume is explicit, not automatic). Config `handoff_threshold_tokens`, `handoff_auto_write_enabled`.

- **Metered cost capture for API-key Claude Code.** The status line now snapshots Claude Code's own `cost.total_cost_usd` and cumulative token counts into a new `session_cost` table (`store.upsert_session_cost`). `cost_report.py` prints this authoritative, cache-accurate total alongside the per-tier breakdown — on API-key billing it matches the invoice, capturing prompt-cache writes/reads that a token-count reconstruction cannot see.
- **Cache-aware savings.** Dedup pointer-collapses are now weighted by `dedup_cache_weight` (default 0.1), because a repeated tool read would bill at the cache-read rate (~0.1×), not full price. `record_saving` records raw + effective tokens; `session_savings` (and the status-line "saved" figure) report the dollar-equivalent effective number. Truncation of fresh output stays at full weight.
- **`tests/test_determinism.py`** — asserts `truncate.shrink` and the dedup hook emit byte-identical output across repeated calls, protecting the auto-cached conversation prefix from invalidation.
- **`agents/nexum-impl-opus.md`** — Opus-tier executor so `needs-strong` step content can be delegated instead of forcing the whole orchestrator onto Opus.
- New config keys: `dispatch_granularity` (`group` | `step`, default `group`), `max_same_tier_retries` (default 1), `dedup_cache_weight` (default 0.1).

### Changed
- **Compact command names:** `/nexum-plan`→`/nx-plan`, `/nexum-implement`→`/nx-build`, `/nexum-handoff`→`/nx-save`, `/nexum-audit`→`/nx-audit`, `/nexum-statusline`→`/nx-status`; new `/nx-load`.
- **Staged context nudges:** `/nx-save` suggested at 100k, `/compact` at 120k (was a single 120k nudge).
- **`/nx-build` request-cost overhaul.** Steps are batched by tier into one warm executor dispatch (`dispatch_granularity: group`) instead of one cold-start dispatch per step; executors run `guardrail.py` themselves and return its verdict (no separate orchestrator round-trip); the reviewer is gated to escalation/`needs-strong`/many-file steps; the retry ladder is 1 same-tier retry (patching the failed diff, not reimplementing) then escalate; and a step whose tier matches the current session model is implemented inline rather than spawned. Orchestration no longer assumes Opus.

## [0.2.1] - 2026-06-15
### Added
- Absolute-token compaction trigger: `statusline_compaction_warn_tokens` (default 80,000). The `⚠ /compact` status line warning now fires when EITHER the percentage threshold (`statusline_compaction_warn_pct`) OR the absolute token count threshold is reached. This ensures large context windows (e.g. Opus's 1M-token window) still warn at a meaningful, cost-relevant size rather than waiting until 800k tokens are consumed.

### Fixed
- `/nexum-statusline` and the README now install a version-independent `statusLine` command (`python3 "$(ls -dt ~/.claude/plugins/cache/nexum/nexum/*/scripts/statusline.py | head -1)"`) instead of a hardcoded `${CLAUDE_PLUGIN_ROOT}` path, which pointed at the versioned cache dir and broke on every `/plugin update`. Also documented that a plugin cannot self-register the main `statusLine` (plugin `settings.json` only supports `agent` and `subagentStatusLine`).

## [0.2.0] - 2026-06-15
### Added
- Session-usage status line (`scripts/statusline.py`) that displays model, context percentage, token count, cumulative cost, and dedup-hook savings in the Claude Code UI. Wire it with the new `/nexum-statusline` command, which resolves the absolute path and offers to merge the `statusLine` key into your `settings.json`.

### Fixed
- The compaction warning is now driven by Claude Code's real `context_window.used_percentage` via the status line (configurable `statusline_compaction_warn_pct`, default 80), replacing the prompt-token estimate that undercounted and rarely fired.

## [0.1.1] - 2026-06-15
### Fixed
- Hook scripts (`truncate.py`, `context_watch.py`, and `dedup.py` via its import
  of `truncate`) crashed on Python 3.9 because of PEP 604 `X | None` type
  annotations evaluated at runtime. Added `from __future__ import annotations`
  so the documented Python 3.9+ support actually holds. Caught by CI.

## [0.1.0] - 2026-06-14
### Added
- Initial release: a Claude Code plugin to cut context tokens and model cost.
- **Context-savings hooks** — PostToolUse truncation and dedup of large/repeated
  tool outputs (`truncate.py`, `dedup.py`).
- **Cost-driven planner/executor workflow** — `/nexum-plan` and `/nexum-implement`
  with Haiku/Sonnet/Opus routing, scope and acceptance guardrails
  (`guardrail.py`), and cost reporting (`cost_report.py`).
- **Lifecycle & hygiene guards** — unscoped-scan suppression (`scan_guard.py`),
  compaction prompt and intent-change new-session nudge (`context_watch.py`),
  and the `/nexum-audit` workflow-hygiene command (`audit.py`).
- SQLite client-side store for dedup, response memoization, and metrics
  (`store.py`). Stdlib-only Python; fail-open hooks.
