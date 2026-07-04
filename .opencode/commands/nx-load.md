---
description: "Resume from the most recent session handoff written by /nx-save or the auto-handoff hook."
---

You are the nexum handoff loader. The user wants to pick up where a previous session left off.

## 1. Locate the handoff

Data directory: `$CLAUDE_PLUGIN_DATA` if set, else `<git toplevel>/.nexum-data` (via `git rev-parse --show-toplevel`), else `./.nexum-data`.

Read `<data_dir>/handoff/latest.md`. If missing, check `<data_dir>/handoff/` for any `*.md` and offer the newest. No handoff → `[nexum] No handoff found. Nothing to resume.`

## 2. Sanity-check

Print the handoff's **Written** timestamp and **Branch**. Warn if old (>1 day) or branch mismatch. Note if it's a rich handoff (from `/nx-save`) or auto-skeleton (git state only).

## 3. Re-establish state

Run `git rev-parse --abbrev-ref HEAD`, `git status --short`, `git diff --stat` to confirm the working tree matches. Surface discrepancies.

## 4. Summarize and continue

Print goal, what was done/verified, and the next concrete step. Then proceed with that step.
