# Changelog

All notable changes to nexum are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
