![nexum](docs/assets/banner.svg)

# nexum

[![CI](https://github.com/dropdevrahul/nexum/actions/workflows/ci.yml/badge.svg)](https://github.com/dropdevrahul/nexum/actions/workflows/ci.yml)
[![Docs](https://github.com/dropdevrahul/nexum/actions/workflows/docs.yml/badge.svg)](https://github.com/dropdevrahul/nexum/actions/workflows/docs.yml)
[![Release](https://img.shields.io/github/v/release/dropdevrahul/nexum)](https://github.com/dropdevrahul/nexum/releases)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)

Long Claude Code sessions burn tokens you never meant to spend — the same file read twice, a giant log pasted in full, a planning task quietly running on Opus when Haiku would have done. **nexum** is a plugin that quietly takes those costs back.

It works along three lines:

- **Context-savings hooks** — truncate oversized tool outputs, collapse repeated reads before they re-enter context, and stop unscoped recursive scans from ever reaching the model.
- **A cost-driven planner and executor** — break work into steps with explicit contracts and scope guards, route each step to the cheapest model that can do it (Haiku → Sonnet → Opus), and verify every step against a real acceptance check.
- **Lifecycle and hygiene guards** — keep a session's intent coherent across handoffs, recommend and maintain ignore files, and keep the status line honest about where your tokens and dollars are going.

It's pure Python standard library, fails open (a broken hook never breaks your session), and keeps all of its state in a single local SQLite file.

## Install

nexum is its own Claude Code plugin marketplace. From inside Claude Code:

```
/plugin marketplace add dropdevrahul/nexum
/plugin install nexum@nexum
```

`/plugin install` enables it right away. Pull new releases with `/plugin marketplace update nexum`. To run from a local checkout instead, point the marketplace at the path: `/plugin marketplace add ./path/to/nexum`.

Full details — including the status-line setup, which needs one line in your own settings — are in the [installation guide](https://dropdevrahul.github.io/nexum/install/).

## Commands

| Command | What it does |
| --- | --- |
| `/nx-plan` | Break the current task into ordered steps with contracts and scope, routing each to the cheapest capable model tier. |
| `/nx-build` | Execute a plan: dispatch steps to Haiku/Sonnet/Opus, run acceptance checks, retry and escalate on failure. |
| `/nx-audit` | Scan the repo for context risks — unignored large/binary files, missing ignore rules — and optionally apply fixes. |
| `/nx-status` | Install the nexum session-usage status line into your Claude Code settings. |
| `/nx-save` | Write a session handoff so you can resume cleanly in a fresh session before a context limit bites. |
| `/nx-load` | Resume from the most recent handoff written by `/nx-save` or the auto-handoff hook. |

## Documentation

The full documentation lives at **[dropdevrahul.github.io/nexum](https://dropdevrahul.github.io/nexum/)**:

- [Install](https://dropdevrahul.github.io/nexum/install/) · [Commands](https://dropdevrahul.github.io/nexum/commands/) · [Configuration](https://dropdevrahul.github.io/nexum/configuration/)
- [How it works](https://dropdevrahul.github.io/nexum/how-it-works/) — the context levers, what works today vs. what's gated on upstream Claude Code fixes
- [Status line](https://dropdevrahul.github.io/nexum/status-line/) · [Contributing](https://dropdevrahul.github.io/nexum/contributing/)

The same pages are in the [`docs/`](docs/) directory if you'd rather read them in the repo.

## License

Released under the [MIT License](LICENSE).
