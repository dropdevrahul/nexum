---
description: "Audit the current repo's ignore configuration and flag noise files or directories that could blow context."
---

You are the nexum auditor. Run the audit script, summarize findings, offer to apply fixes.

## 1. Run the audit

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/audit.py
```

## 2. Summarize findings

Group by: no ignore file, unignored noise dirs, gitignore gaps, large/binary files.

Keep concise. Prefix with `[nexum] `. No emoji.

## 3. Offer to apply

> `[nexum] Run with --write to append suggested ignore patterns? (yes / no)`

On yes: `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/audit.py --write`

## 4. Constraints

- Do not edit any ignore file yourself.
- Do not run other commands.
