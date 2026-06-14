#!/usr/bin/env python3
"""
dedup.py — Nexum deduplication hook (PostToolUse).

Runs AFTER truncate.py in the hook chain but reads the ORIGINAL tool output
from its own stdin (the two are separate processes and each receive the
unmodified hook input). Per §1 EDGE CASE: dedup is the AUTHORITY on the
final updatedToolOutput — it re-applies truncate.shrink() so the emitted
text is both deduplicated and truncated in one pass.

Contract:
- Only acts on outputs >= 30 lines OR >= 2000 chars.  Tiny outputs → emit {}.
- Computes h = store.sha256(output).
  * If store.seen_output(session_id, h) exists → emit pointer, omit body.
  * Else: shrunk,_ = truncate.shrink(output, cfg); record; emit shrunk.
- Fail-open: any unhandled error → print {} exit 0.
- Deterministic JSON: json.dumps(..., sort_keys=True).
"""

import json
import os
import sys


# ---------------------------------------------------------------------------
# Bootstrap: make sure scripts/ dir is on sys.path so `import store` and
# `import truncate` work when invoked as python3 scripts/dedup.py from any cwd.
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import store      # noqa: E402  (must come after path setup)
import truncate   # noqa: E402


# ---------------------------------------------------------------------------
# Thresholds for "worth deduplicating"
# ---------------------------------------------------------------------------
_MIN_LINES = 30
_MIN_CHARS = 2000


def _is_large(text: str) -> bool:
    """Return True if the output is large enough to be worth deduplicating."""
    if len(text) >= _MIN_CHARS:
        return True
    if text.count("\n") + 1 >= _MIN_LINES:
        return True
    return False


def _make_summary(output: str, token_count: int) -> str:
    """Build a short summary: first non-empty line + token estimate."""
    first_line = ""
    for line in output.splitlines():
        stripped = line.strip()
        if stripped:
            first_line = stripped[:120]  # cap at 120 chars
            break
    return f"{first_line} (~{token_count} tokens)"


def main() -> None:
    """PostToolUse hook entry point."""
    try:
        # ----------------------------------------------------------------
        # 1. Parse stdin JSON
        # ----------------------------------------------------------------
        try:
            data = json.load(sys.stdin)
        except (json.JSONDecodeError, ValueError):
            print("{}")
            return

        if not isinstance(data, dict):
            print("{}")
            return

        # ----------------------------------------------------------------
        # 2. Extract tool output
        # ----------------------------------------------------------------
        output = truncate.extract_output(data)
        if not output:
            # Empty or missing output — nothing to do.
            print("{}")
            return

        # ----------------------------------------------------------------
        # 3. Size gate: only dedup large outputs
        # ----------------------------------------------------------------
        if not _is_large(output):
            print("{}")
            return

        # ----------------------------------------------------------------
        # 4. Session / tool metadata
        # ----------------------------------------------------------------
        session_id = data.get("session_id") or "_nosession"
        tool_name = data.get("tool_name") or "unknown"

        # ----------------------------------------------------------------
        # 5. Load config (needed for shrink; fail-open if unavailable)
        # ----------------------------------------------------------------
        try:
            cfg = store.get_config()
        except Exception:
            cfg = {}

        # ----------------------------------------------------------------
        # 6. Compute hash of the ORIGINAL (pre-shrink) output.
        #    The hash identifies the content; dedup collapses identical content.
        # ----------------------------------------------------------------
        h = store.sha256(output)

        # ----------------------------------------------------------------
        # 7. Dedup check
        # ----------------------------------------------------------------
        existing = store.seen_output(session_id, h)

        if existing is not None:
            # Pointer collapse — identical content seen before.
            pointer = (
                f"[nexum] identical to earlier {tool_name} output "
                f"(hash {h[:8]}) — omitted to save context."
            )
            response = {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "updatedToolOutput": pointer,
                }
            }
            print(json.dumps(response, sort_keys=True))
            return

        # ----------------------------------------------------------------
        # 8. New content: shrink (dedup is the authority on final output),
        #    record, and emit.
        # ----------------------------------------------------------------
        shrunk, _acted = truncate.shrink(output, cfg)

        token_count = store.estimate_tokens(shrunk)
        summary = _make_summary(output, token_count)

        store.record_output(session_id, tool_name, h, summary, token_count)

        response = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedToolOutput": shrunk,
            }
        }
        print(json.dumps(response, sort_keys=True))

    except Exception:
        # Fail-open: never crash the Claude Code session.
        print("{}")


if __name__ == "__main__":
    main()
