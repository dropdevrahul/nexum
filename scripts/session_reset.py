#!/usr/bin/env python3
"""
session_reset.py — Nexum SessionStart housekeeping hook.

Two jobs on session start:

1. Invalidate predup state on a context reset. When `source` is `clear` (the
   user ran /clear) or `compact` (the session resumed after a compaction), the
   tool output predup keys off is gone — so clear this session's `tool_calls`
   rows to prevent predup denying a legitimate re-read. No-op for `startup`/
   `resume`, where prior context is still present.
2. Throttled retention prune (`store.maybe_prune`, at most once/day) so the
   SQLite file and predup lookups stay bounded across many sessions.

Filtering is done in-script on the `source` field rather than via a hook
matcher, so behaviour is identical regardless of SessionStart matcher support.

Hook contract:
  stdin  → single JSON object (session_id, source, ...)
  stdout → "{}" always
  exit 0 always (fail-open)
"""

import json
import os
import sys

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import store  # noqa: E402

_RESET_SOURCES = {"clear", "compact"}


def main() -> None:
    try:
        try:
            data = json.loads(sys.stdin.read())
        except Exception:
            print("{}")
            return
        if not isinstance(data, dict):
            print("{}")
            return

        source = (data.get("source") or "").strip().lower()
        session_id = data.get("session_id") or "_nosession"

        if source in _RESET_SOURCES:
            cfg = store.get_config()
            if cfg.get("precompact_invalidate_predup", True):
                try:
                    store.clear_tool_calls(session_id)
                except Exception:
                    pass

        # Bounded, throttled retention prune (internally rate-limited to 1/day).
        try:
            store.maybe_prune()
        except Exception:
            pass

    except Exception:
        pass
    print("{}")


if __name__ == "__main__":
    main()
