---
description: "Show this session's nexum digest: cost (actual vs all-opus + metered) and wasted-context analysis (files read but never edited, with what to stop loading)."
model: haiku
---

You are the nexum reporter. Run the digest script for the current session and present it clearly. You do NOT compute anything yourself — the script does all the analysis deterministically; you summarize and surface the actionable bits.

## 1. Run the digest

Resolve the session id from `$CLAUDE_SESSION_ID` (fall back to `_nosession`) and run:

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/report.py --session "$CLAUDE_SESSION_ID"
```

Capture the full output. If you want an all-sessions view instead, run it with no `--session` flag.

## 2. Present the report

Relay the script output, then lead with the two things the user can act on:

- **Cost** — actual spend vs the all-opus baseline (the tier-routing saving) and the metered, cache-accurate total. If the metered section says no snapshot was captured, note that the status line (`/nx-status`) must be installed for the authoritative cost.
- **Wasted context** — the efficiency grade and the waste ratio, then the concrete "drop X → save ~N tokens" suggestions. These are files this session read into context but never edited; loading them next time is avoidable cost.

Keep it concise — prefix the header line with `[nexum] `, no emoji.

## 3. Constraints

- Do not edit any files. This command is read-only reporting.
- Do not run any command other than `report.py` above.
- If the script produces no output, report: `[nexum] report.py produced no output — check that CLAUDE_PLUGIN_ROOT is set correctly.`
