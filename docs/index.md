# nexum

![nexum](assets/banner.svg)

nexum is a Claude Code plugin that cuts context tokens and model cost during your sessions. It works through three optimization pillars: context-savings hooks that automatically truncate large tool outputs, deduplicate repeated results, and block context-blowing scans; a cost-driven planner and executor that structures work as steps with contracts and scope guards, routing each step to the right model tier (Haiku, Sonnet, or Opus) based on complexity; and lifecycle and hygiene guards that enforce per-session intent continuity, recommend and maintain ignore files, and prevent unscoped recursive searches.

## Get started

- [Install nexum](install.md) — add the marketplace and enable the plugin in two commands.
- [Commands reference](commands.md) — what `/nx-plan`, `/nx-build`, `/nx-audit`, `/nx-status`, `/nx-save`, and `/nx-load` do and when to use them.
- [Agent TUI (crew)](tui.md) — a standalone terminal dashboard to run, chat with, and manage coding agents across harnesses (with a [screenshots showcase](crew/)).
