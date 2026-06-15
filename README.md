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

A plugin cannot register the main `statusLine` itself (a plugin's `settings.json` only supports `agent` and `subagentStatusLine`), so you add it to your own settings. Run `/nexum-statusline` to merge it automatically, or add it manually:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 \"$(ls -dt ~/.claude/plugins/cache/nexum/nexum/*/scripts/statusline.py | head -1)\"",
    "padding": 0
  }
}
```

Put this in `~/.claude/settings.json` (user-level) or `.claude/settings.json` (project-level). The `$(ls -dt … | head -1)` resolves the newest installed nexum version, so the status line keeps working after `/plugin update` instead of breaking on a hardcoded version path. (There is only one `statusLine` slot, so this replaces any existing one.)

The status line reads the session JSON piped in by Claude Code on stdin and takes effect on the next interaction after the setting is saved.

The status line appends a `⚠ /compact` warning to prompt you to run `/compact` before the window fills. The warning fires when EITHER of two configurable thresholds is crossed — whichever comes first:

- `statusline_compaction_warn_pct` (default 80%) — fires when context usage reaches this percentage of the window.
- `statusline_compaction_warn_tokens` (default 80,000) — fires when the absolute context token count reaches this value, regardless of window size (useful for large windows such as Opus's 1M-token window where 80% would be 800k tokens).

Both thresholds are configurable via `config.json` in the nexum data directory. Set either to `0` to disable that trigger.

## Technical Notes

- **Stdlib only** — all Python dependencies are from the standard library (3.9+). No pip installs.
- **Fail-open** — hooks never crash the Claude Code session; errors emit `{}` and exit 0.
- **State** — persistent session state (dedup memo, usage metrics, task history) lives in SQLite at `${CLAUDE_PLUGIN_ROOT}/.nexum-data/nexum.db`.
