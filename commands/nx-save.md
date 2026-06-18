---
description: "Write a session handoff so you can continue in a fresh session once a context or plan limit is near."
---

You are the nexum handoff writer. Capture everything a NEW session (which shares none of this conversation's context) needs to resume this work cleanly, and write it to a handoff file. Use this when the context window is filling up or the 5-hour plan limit is nearly exhausted and continuing in a fresh session (or after the reset) is cheaper/cleaner.

## 1. Resolve the handoff path

Resolve the data directory: `$CLAUDE_PLUGIN_DATA` if set, else `<git toplevel of the current working directory>/.nexum-data` (via `git rev-parse --show-toplevel`), else `./.nexum-data`. Session id comes from `$CLAUDE_SESSION_ID` (or `_nosession`).

Create `<data_dir>/handoff/` if needed. Write the handoff to BOTH:
- `<data_dir>/handoff/<session_id>.md` (the durable per-session copy), and
- `<data_dir>/handoff/latest.md` (a stable path a fresh session can find without knowing the old id).

Note: the `context_watch` hook may have already auto-written a deterministic *skeleton* to these paths once context crossed `handoff_threshold_tokens` (git state only, no narrative). This command produces the **rich** handoff and overwrites that skeleton — which is the intended result. A fresh session picks up whichever `latest.md` is present when the user runs `/nx-load`, so your richer version wins.

## 2. Gather real state (do not guess)

Before writing, collect concrete facts:
- `git rev-parse --abbrev-ref HEAD` (current branch) and `git status --short` and `git diff --stat` (uncommitted work). Note untracked scratch files.
- The session task, if recorded: read it via `python3 -c "import sys; sys.path.insert(0,'${CLAUDE_PLUGIN_ROOT}/scripts'); import store; print(store.get_session_task('<session_id>') or '')"`.
- The actual work done this session from the conversation: decisions made, what was verified (and how), and what is still open.

## 3. Write the handoff (self-contained, concise)

The file MUST let a cold session resume without re-deriving context. Use this structure:

```markdown
# Handoff: <short task title>

**Session:** <session_id>   **Branch:** <branch>   **Written:** <ISO datetime>
**Why now:** <context near limit | 5h plan limit near | user-requested>

## Goal
<one or two sentences: what the overall task is trying to achieve>

## State now
- <what is DONE and verified — name the verification, e.g. "tests pass: python3 -m pytest -q (221 passed)">
- Uncommitted changes: <git diff --stat summary>; untracked: <files>
- <key decisions / findings that are expensive to rediscover>

## Next steps (in order)
1. <the very next concrete action, with the file path / command>
2. ...

## How to resume
- Read: <the 1-3 files most worth opening first, with paths>
- Run: <the command(s) that re-establish where things stand, e.g. `git status`, the test suite>
- Watch out for: <gotchas, e.g. live plugin runs from the worktree, not the cache>
```

Rules:
- Name every file path explicitly; a fresh session cannot see this conversation.
- Convert relative times to absolute. Quote exact commands.
- Be concise — capture signal, not a transcript. Do NOT paste large diffs; summarise and point to `git diff`.
- Do not invent state you did not verify; if something is uncertain, say so.

## 4. Report

Print the absolute path of the written handoff and a one-line summary. Tell the user they can start a fresh session and run `/nx-load` is NOT required — instead instruct: open the new session and say "read <data_dir>/handoff/latest.md and continue", or `cat` that file. If a context limit (not the plan limit) triggered this, also suggest `/compact` as the lighter alternative when a full reset isn't needed.
