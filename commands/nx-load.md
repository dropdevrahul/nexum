---
description: "Resume from the most recent session handoff written by /nx-save or the auto-handoff hook."
---

You are the nexum handoff loader. The user is (usually) in a fresh session and wants to pick up where a previous session left off. Load the most recent handoff and continue the work — explicitly, because the user asked, not automatically.

## 1. Locate the handoff

Resolve the data directory the same way `store.py` does: `$CLAUDE_PLUGIN_DATA`, else `${CLAUDE_PLUGIN_ROOT}/.nexum-data`, else `./.nexum-data`.

Read `<data_dir>/handoff/latest.md`.
- If it does not exist, check `<data_dir>/handoff/` for any `*.md` and offer the most recently modified one.
- If there is no handoff at all, tell the user: `[nexum] No handoff found. Nothing to resume.` and stop.

## 2. Sanity-check freshness and origin

- Print the handoff's **Written** timestamp and **Branch**. If the handoff is old (e.g. more than a day) or names a branch that isn't the current one (`git rev-parse --abbrev-ref HEAD`), say so plainly — the user may not want to resume it. Ask before proceeding if there's a mismatch.
- A handoff may be a **rich** one (from `/nx-save`) or an **auto-skeleton** (git state only, written by the hook past the context threshold). If it's the skeleton, say so: it has no decisions/next-steps narrative, so you will reconstruct intent from the git diff.

## 3. Re-establish state (don't trust the handoff blindly)

Run, from the repo root:
- `git rev-parse --abbrev-ref HEAD`, `git status --short`, `git diff --stat` — confirm the working tree matches what the handoff describes.
- If the handoff references a nexum plan or step ledger, the work may be resumable via `/nx-build` (which itself resumes completed steps). Mention that if relevant.

If the actual git state contradicts the handoff (e.g. it says files are uncommitted but the tree is clean), surface the discrepancy rather than acting on stale notes.

## 4. Summarize and continue

Print a short summary: the goal, what was done/verified, and the next concrete step from the handoff (or, for a skeleton, the next step you infer from the diff). Then proceed with that next step — or, if the goal is ambiguous from a skeleton, ask the user to confirm the goal before doing work.

Do not delete or move `latest.md`; leave it in place so the user can re-run this if needed.
