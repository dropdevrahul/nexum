# Changelog

All notable changes to nexum are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]
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
