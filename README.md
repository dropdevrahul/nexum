# nexum

**nexum** is a Claude Code plugin that cuts context tokens and model cost during Claude Code sessions through three optimization pillars:

1. **Context-savings hooks** — automatically truncate large tool outputs, deduplicate repeated results, and warn on context-blowing scans.
2. **Cost-driven planner & executor** — structure work as steps with contracts and scope guards, route to the right model tier (Haiku/Sonnet/Opus) based on complexity, and verify each step against acceptance criteria.
3. **Lifecycle & hygiene guards** — enforce per-session intent continuity, recommend and maintain ignore files, and prevent unscoped recursive searches.

## Install

This repository is its own Claude Code plugin marketplace. Install it from within Claude Code:

```
/plugin marketplace add dropdevrahul/nexum
/plugin install nexum@nexum
```

`/plugin install` enables the plugin immediately. Use `/plugin marketplace update nexum` to pull new releases.

To try it from a local checkout instead:

```
/plugin marketplace add ./path/to/nexum
/plugin install nexum@nexum
```

## Commands

- **`/nexum-plan`** — Analyze the task and produce a multi-step plan with explicit contracts and scope boundaries.
- **`/nexum-implement`** — Execute the plan, routing each step to Haiku, Sonnet, or Opus based on complexity, running acceptance checks, and reporting per-step results.
- **`/nexum-audit`** — Scan the repo for context risks (unignored large/binary files, missing ignore rules) and optionally apply recommendations.

## Status line

nexum ships `scripts/statusline.py`, a Claude Code `statusLine` command that renders a compact session-usage bar in the Claude Code UI:

```
nexum <model>  ·  <bar> <pct>%  ·  <tokens> tok  ·  $<cost>  ·  saved <n>
```

Run `/nexum-statusline` to have nexum resolve the absolute path and offer to merge the setting into your `settings.json` automatically. Or add it manually:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 /absolute/path/to/nexum/scripts/statusline.py",
    "padding": 0
  }
}
```

Put this in `~/.claude/settings.json` (user-level) or `.claude/settings.json` (project-level). Note that `${CLAUDE_PLUGIN_ROOT}` is not expanded inside `settings.json`, so you must use an absolute path.

The status line reads the session JSON piped in by Claude Code on stdin and takes effect on the next interaction after the setting is saved.

When context usage reaches the `statusline_compaction_warn_pct` threshold (default 80%), the status line appends a `⚠ /compact` warning to prompt you to run `/compact` before the window fills. The threshold is configurable via `config.json` in the nexum data directory; set it to `0` to disable the warning entirely.

## Technical Notes

- **Stdlib only** — all Python dependencies are from the standard library (3.9+). No pip installs.
- **Fail-open** — hooks never crash the Claude Code session; errors emit `{}` and exit 0.
- **State** — persistent session state (dedup memo, usage metrics, task history) lives in SQLite at `${CLAUDE_PLUGIN_ROOT}/.nexum-data/nexum.db`.
