#!/usr/bin/env python3
"""
precompact.py — Nexum PreCompact hook.

Fires immediately before Claude Code compacts the conversation. Two jobs, both
done at the exact compaction boundary (no token estimate, no polling):

1. Invalidate predup state — compaction evicts tool output from the live
   context, but the `tool_calls` rows predup keys off persist. Clearing them
   here prevents predup from later denying a legitimate re-read of content the
   compaction removed.
2. Write a deterministic handoff skeleton — guaranteed to capture git + task
   state at the boundary even if the post-compaction session is interrupted.

This hook NEVER blocks compaction: it performs its side effects and emits `{}`
(a PreCompact `decision: "block"` would cancel the user's compaction).

Hook contract:
  stdin  → single JSON object (Claude Code PreCompact payload: session_id,
           transcript_path, cwd, trigger)
  stdout → "{}" always (never block)
  exit 0 always (fail-open)
"""

import json
import os
import sys

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import store  # noqa: E402


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

        session_id = data.get("session_id") or "_nosession"
        cwd = data.get("cwd") or None
        cfg = store.get_config()

        # 1. Invalidate predup's tool_calls for this session.
        if cfg.get("precompact_invalidate_predup", True):
            try:
                store.clear_tool_calls(session_id)
            except Exception:
                pass

        # 2. Write a handoff skeleton at the boundary (best-effort).
        if cfg.get("precompact_handoff_enabled", True):
            try:
                import handoff  # noqa: E402 (path already set)
                token_total = 0
                tp = data.get("transcript_path") or ""
                if tp:
                    try:
                        token_total = store.context_tokens_from_transcript(tp) or 0
                    except Exception:
                        token_total = 0
                handoff.write_skeleton(
                    session_id=session_id, cwd=cwd, token_total=token_total
                )
            except Exception:
                pass

    except Exception:
        pass
    # Always allow the compaction to proceed.
    print("{}")


if __name__ == "__main__":
    main()
