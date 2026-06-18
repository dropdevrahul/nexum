# Changelog

All notable changes to nexum are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-06-18
### Added
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
