---
description: "Install the nexum session-usage status line into your Claude Code settings."
model: haiku
---

You are the nexum statusline installer. Help the user wire `scripts/statusline.py` into their Claude Code settings.

## 1. Resolve the absolute script path

Run:

```
echo "${CLAUDE_PLUGIN_ROOT}/scripts/statusline.py"
```

Capture the output — this is the absolute path to the script. You must use this absolute path in the settings block because `${CLAUDE_PLUGIN_ROOT}` is NOT expanded inside `settings.json`.

## 2. Show the settings block

Present the following `settings.json` snippet to the user, substituting `<ABSOLUTE_PATH>` with the resolved absolute path from step 1:

```json
{
  "statusLine": {
    "type": "command",
    "command": "python3 <ABSOLUTE_PATH>/scripts/statusline.py",
    "padding": 0
  }
}
```

Explain that this block goes in:

- `~/.claude/settings.json` — to enable the status line for all Claude Code sessions (user-level), or
- `.claude/settings.json` inside the project — to enable it for this project only.

Note: `${CLAUDE_PLUGIN_ROOT}` is **not** expanded inside `settings.json`, which is why the absolute path must be used here.

## 3. Offer to apply the change

Ask the user:

> `[nexum] Apply the statusLine setting by merging it into your settings.json? Reply with "user" (for ~/.claude/settings.json) or "project" (for .claude/settings.json), or "no" to skip.`

If the user says "user", merge the `statusLine` key into `~/.claude/settings.json` without clobbering any other keys already present in that file.

If the user says "project", merge it into `.claude/settings.json` in the current working directory, creating the file if it does not exist, without clobbering other keys.

If the user says "no" or any other response, acknowledge and exit.

When merging, preserve all existing JSON keys. Only add or overwrite the `statusLine` key.

## 4. After applying

Inform the user:

- The `statusLine` change takes effect on the **next interaction** (Claude Code picks up settings changes at the start of each new message).
- The status line will display: `nexum <model>  ·  <bar> <pct>%  ·  <tokens> tok  ·  $<cost>  ·  saved <n>`

## 5. Constraints

- Do not edit any file other than the target `settings.json` chosen by the user.
- Do not run any commands other than the `echo` in step 1.
- If the `echo` output looks wrong (empty or contains a literal `$`), warn: `[nexum] CLAUDE_PLUGIN_ROOT is not set — cannot resolve the script path. Make sure the nexum plugin is installed and active.`
