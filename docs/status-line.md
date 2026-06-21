# Status line

nexum ships `scripts/statusline.py`, a Claude Code `statusLine` command that renders a compact session-usage bar in the Claude Code UI:

```
nexum <model>  ·  <bar> <pct>%  ·  <tokens> tok  ·  $<cost>  ·  saved <n>
```

When context usage crosses a warn threshold, a `⚠ /compact` suffix is appended as a reminder to run `/compact` before the window fills.

## Setup

A plugin cannot register the main `statusLine` itself — a plugin's `settings.json` only supports the `agent` and `subagentStatusLine` keys. The status line must be added to your own settings file.

### Automatic setup with /nx-status

Run `/nx-status` inside Claude Code. It presents the settings block and asks whether to apply it to your user-level settings (`~/.claude/settings.json`) or project-level settings (`.claude/settings.json`). There is only one `statusLine` slot, so this replaces any existing one — the command shows your current value first so you can restore it if needed.

### Manual setup

Add this block to `~/.claude/settings.json` (all sessions) or `.claude/settings.json` (this project only):

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 \"$(ls -dt ~/.claude/plugins/cache/nexum/nexum/*/scripts/statusline.py | head -1)\"",
    "padding": 0
  }
}
```

The `$(ls -dt … | head -1)` expression picks the newest installed nexum version automatically, so the status line keeps working after `/plugin marketplace update nexum` without requiring you to edit the path.

The change takes effect on the next interaction after saving.

## Compaction warnings

The status line appends `⚠ /compact` when EITHER of two configurable thresholds is crossed — whichever comes first:

- `statusline_compaction_warn_pct` (default `80`) — fires when context usage reaches this percentage of the context window.
- `statusline_compaction_warn_tokens` (default `80000`) — fires when the absolute token count reaches this value, regardless of window size. This is useful for large windows (such as Opus's 1M-token window) where 80% would be 800,000 tokens — far later than most users want to be reminded.

Both thresholds are configurable via `config.json`. Set either to `0` to disable that trigger entirely.

```json
{
  "statusline_compaction_warn_pct": 80,
  "statusline_compaction_warn_tokens": 80000
}
```

## What the status line shows

The status line reads the session JSON piped in by Claude Code on stdin and displays:

- **model** — the model currently running (haiku / sonnet / opus).
- **bar and pct** — a visual bar and percentage of the context window used.
- **tokens** — the current context token count.
- **cost** — the metered session cost in USD, captured from Claude Code's own cost tracking.
- **saved** — tokens saved by nexum hooks this session (non-zero once the self-test confirms PostToolUse is honored, or immediately for PreToolUse-based savings like pre-emptive dedup).
