#!/usr/bin/env python3
"""fake_harness.py — a stand-in headless agent CLI for tests.

Invoked exactly like a real harness would be (prompt is the final argv item).
It "does work" by writing a marker file into its cwd (the worktree dispatch.py
created), then prints one stream-json result line so parse_stream sees tokens.

Used via `NEXUM_HARNESS_CMD_CLAUDE="python3 <abs>/tests/fixtures/fake_harness.py"`
so dispatch.py can be exercised end-to-end with no real claude/opencode/cursor
binary installed.
"""

import json
import os
import sys

target = os.environ.get("FAKE_HARNESS_TARGET", "fake_out.txt")
prompt = sys.argv[-1] if len(sys.argv) > 1 else ""

with open(target, "w", encoding="utf-8") as fh:
    fh.write("edited by fake harness\n")

print(json.dumps({"type": "result", "tokens": 10, "cost_usd": 0.0}))
