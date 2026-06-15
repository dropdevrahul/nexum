---
description: "Install the nexum session-usage status line into your Claude Code settings."
model: haiku
---

You are the nexum statusline installer. Wire the nexum status line into the user's Claude Code settings using a version-independent command so it keeps working after plugin updates.

## Why a settings.json command (not plugin auto-registration)

A Claude Code plugin cannot register the main `statusLine` itself — a plugin's bundled `settings.json` only honors the `agent` and `subagentStatusLine` keys. So the status line must be set in the user's or project's `settings.json`. The installed plugin lives in a **versioned** cache directory (`~/.claude/plugins/cache/nexum/nexum/<version>/…`) that changes on every `/plugin update`, so the configured command must resolve the script dynamically rather than hardcode a version path.

## 1. The settings block

Present this `settings.json` snippet to the user verbatim:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 \"$(ls -dt ~/.claude/plugins/cache/nexum/nexum/*/scripts/statusline.py | head -1)\"",
    "padding": 0
  }
}
```

The `$(ls -dt … | head -1)` picks the newest installed nexum version automatically, so the status line survives plugin updates without re-editing settings.

It goes in either:
- `~/.claude/settings.json` — enable for all sessions (user-level), or
- `.claude/settings.json` in the project — enable for this project only.

## 2. Offer to apply

Ask:

> `[nexum] Apply this statusLine to your settings? Reply "user", "project", or "no".`

There is only one `statusLine` slot. If the target settings file already has a different `statusLine`, applying this **replaces** it — first show the user their existing value so they can restore it, then confirm.

- On "user": merge the `statusLine` key into `~/.claude/settings.json`.
- On "project": merge it into `.claude/settings.json` in the current directory (create the file if missing).
- On anything else: acknowledge and exit.

Preserve all existing keys; only set `statusLine`.

## 3. After applying

- The change takes effect on the **next interaction**.
- The status line displays: `nexum <model>  ·  <bar> <pct>%  ·  <tokens> tok  ·  $<cost>  ·  saved <n>`, with a trailing `⚠ /compact` when context usage reaches the warn threshold (`statusline_compaction_warn_pct`, default 80%).

## Constraints

- Edit only the chosen `settings.json`.
- To confirm the plugin is installed before applying, run: `ls -d ~/.claude/plugins/cache/nexum/nexum/*/` — if it lists nothing, tell the user to install nexum first (`/plugin install nexum@nexum`).
