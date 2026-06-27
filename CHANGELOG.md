# Changelog

All notable changes to nexum are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]
### Added
- **Caveman prompts — telegraphic plans + dispatch prompts (`caveman_prompts_enabled`, default true).** `/nx-plan` now writes the plan's prose fields (task summary, step `title`/`objective`/`contract`/`scope`) in clipped, telegraphic English — articles, copulas, and filler dropped — and `/nx-build` builds its executor dispatch prompts the same way. The plan is re-read by every executor and the shared dispatch prefix ships on every step, so trimming function words from them is a recurring token saving. Strict carve-outs stay verbatim and unambiguous: file paths, identifiers, signatures, config keys, code, and the runnable `acceptance` command — terseness never costs precision. Set `false` for normal prose.
- **Grep narrowing — a *working* PreToolUse context-savings lever (`scripts/scan_guard.py`).** An unscoped/broad search now has its **output capped** instead of being hard-denied: the `Grep` tool gets a `head_limit` injected and an unscoped recursive Bash `grep`/`rg` gets `| head -n N` appended (via PreToolUse `updatedInput`, which current Claude Code honors — unlike PostToolUse output shrink). The model still gets a bounded answer with no retry round-trip. Searches into a `scan_deny_paths` directory, and Glob (no `head_limit`), still deny; a Bash grep that already pipes falls back to deny; an explicit caller `head_limit` is never overridden. Config: `grep_narrow_enabled` (default true), `grep_head_limit` (default 80).
- **Confidence-aware, bidirectional routing calibration (`store.calibration_advice`, `store.py calib-advice` CLI).** Replaces the raw first-try pass ratio with a **Wilson score lower bound**, so a short lucky/unlucky streak no longer flips a route. Advice is now **bidirectional**: nudge a route *up* a tier when low-confidence (lower bound < `route_calib_min_success_ratio`) and *down* a tier when a cheaper tier reliably suffices (lower bound ≥ `route_calib_downgrade_ratio`, default 0.9). Falls back to a cross-repo `_global` prior when a repo lacks `route_calib_min_samples` of its own history. `/nx-plan` consumes the new `calib-advice` JSON (action/reason/samples/lower/source). Config: `route_calib_downgrade_ratio` (default 0.9; 1.0 disables downgrades).
- **Honest savings split in `/nx-report` (`scripts/report.py`, `store.savings_by_source`).** The report now separates savings into three buckets so the headline never overclaims the inert PostToolUse lever: **Realized** (PreToolUse, measured — `predup`'s exact denied-repeat tokens), **Bounded interventions** (`read_guard`/`grep_narrow` — output capped, exact saving unknowable, counted only), and **Theoretical** (`dedup`/`truncate` — PostToolUse shrink that is inert because Claude Code ignores `updatedToolOutput` for built-in tools, tracking #65403). read-guard and grep-narrow now record a 0-token intervention row so the bounded count is visible.
- **`PreCompact` hook (`scripts/precompact.py`).** Fires at the exact compaction boundary to (a) invalidate this session's `tool_calls` rows — so predup can never deny a re-read of output the compaction just evicted — and (b) write a deterministic handoff skeleton. Never blocks compaction. Closes the predup-after-compaction correctness gap that the `predup_max_age_seconds` time guard could only approximate.
- **`SessionStart` reset/prune hook (`scripts/session_reset.py`).** On `source` `clear`/`compact`, clears `tool_calls` for the same reason as PreCompact (filtered in-script, not via matcher). Also runs the throttled retention prune.
- **`SubagentStop` hook (`scripts/subagent_usage.py`) → real per-tier usage.** Maps a nexum executor agent (`nexum-impl-{haiku,sonnet,opus}`) to its tier and records a `usage` row with token totals parsed best-effort from the subagent transcript (`store.transcript_usage_totals`), so the cost-report breakdown reflects measured spend rather than the plan-preview estimate. (The SubagentStop payload carries no usage fields, so attribution is transcript-based and best-effort — documented as such.)
- **Retention/pruning (`store.prune`, `store.maybe_prune`, `store.py prune` CLI).** Rows older than `retention_days` (default 14) are pruned from the ephemeral tables (`tool_calls`, `savings`, `outputs`, `usage`, `memo`, `file_activity`), throttled to at most once/day on session start, keeping the SQLite file and predup lookups bounded. `0` disables. (`route_calibration`/`step_ledger` are persistent and intentionally not age-pruned.)
- **Fable pricing.** `store.PRICING` gains `fable` ($10/$50 per MTok) and `cost_report._model_key` recognises it (and orders matching most-specific-first), so Fable-class usage is no longer mispriced as Sonnet. Bedrock/Vertex `anthropic.`-prefixed IDs still map correctly.
- **Wasted-context analytics + `/nx-report` (inspired by the open-source `claude-context-optimizer`).** A PostToolUse tracker (extends `dedup.py`, now also matching `Edit|Write|MultiEdit`) records per-file read/edit counts and injected-token estimates in a new `file_activity` table. The new `/nx-report` command (`scripts/report.py`) prints a deterministic, no-LLM session digest: the cost summary plus a wasted-context analysis — total tokens read, tokens spent on files read but never edited, a waste ratio, an S–F efficiency grade, a per-file useful/WASTED table, and concrete "drop X → save ~N tokens" suggestions. New `store` helpers: `record_file_read`, `record_file_edit`, `file_activity_rows`, `wasted_files`. Config `file_activity_enabled` (default true).
- **Tiered budget alerts (C).** `context_watch` now compares the session's real metered cost (the status-line `session_cost` snapshot) to `budget_usd`, and cumulative input+output tokens to `budget_tokens`, emitting an escalating, once-per-tier non-blocking `systemMessage` at `budget_alert_tiers` (default 50/70/80/90%). At ≥70% it names the biggest never-edited files to drop (from the wasted-context tracker); at ≥90% it urges `/compact`. Both budgets default to `0` (disabled).

### Changed
- **predup recall — canonicalised signatures.** `store.tool_call_sig` now realpath-canonicalises `file_path`/`path` before hashing, so `./foo.py`, `foo.py`, and the absolute path collapse to one signature and predup catches the repeat. Distinct read ranges (`offset`/`limit`) and patterns stay distinct. New `store.clear_tool_calls`.

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
