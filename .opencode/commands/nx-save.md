---
description: "Write a session handoff so you can continue in a fresh session once a context or plan limit is near."
---

You are the nexum handoff writer. Capture everything a NEW session (which shares none of this conversation's context) needs to resume this work cleanly, and write it to a handoff file.

## 1. Resolve the handoff path

Resolve the data directory: `$CLAUDE_PLUGIN_DATA` if set, else `${CLAUDE_PLUGIN_ROOT}/.nexum-data`, else `./.nexum-data`. Session id from `$CLAUDE_SESSION_ID` (or `_nosession`).

Create `<data_dir>/handoff/` if needed. Write the handoff to BOTH:
- `<data_dir>/handoff/<session_id>.md`
- `<data_dir>/handoff/latest.md`

## 2. Gather real state (do not guess)

- `git rev-parse --abbrev-ref HEAD`, `git status --short`, `git diff --stat`
- Session task: `python3 -c "import sys; sys.path.insert(0,'${CLAUDE_PLUGIN_ROOT}/scripts'); import store; print(store.get_session_task('<session_id>') or '')"`
- Actual work done from the conversation

## 3. Write the handoff (self-contained, concise)

```markdown
# Handoff: <short task title>

**Session:** <session_id>   **Branch:** <branch>   **Written:** <ISO datetime>
**Why now:** <context near limit | 5h plan limit near | user-requested>

## Goal
<one or two sentences>

## State now
- <what is DONE and verified>
- Uncommitted changes: <git diff --stat summary>
- <key decisions / findings>

## Next steps (in order)
1. <concrete action, with file path / command>
2. ...

## How to resume
- Read: <the 1-3 files most worth opening first>
- Run: <the command(s) that re-establish state>
- Watch out for: <gotchas>
```

## 4. Report

Print the absolute path of the written handoff and a one-line summary. Also suggest `/compact` if context-triggered.
