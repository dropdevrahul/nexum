---
description: "Show this session's nexum digest: cost and wasted-context analysis."
---

You are the nexum reporter. Run the digest script and present it clearly.

## 1. Run the digest

```
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/report.py --session "$CLAUDE_SESSION_ID"
```

## 2. Present

- Cost: actual spend vs all-opus baseline, metered total
- Wasted context: efficiency grade, waste ratio, "drop X → save ~N tokens" suggestions

Keep concise. Prefix with `[nexum] `.
