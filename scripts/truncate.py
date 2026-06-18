#!/usr/bin/env python3
"""
truncate.py — Nexum context-savings hook (PostToolUse).

Exposes:
- shrink(text, cfg) -> (shrunk_text, acted: bool)
  Pure function; reusable by dedup.py.
- extract_output(data) -> str | None
  Extracts tool output from hook input JSON.
- main()
  PostToolUse hook: read stdin JSON, shrink if needed, emit hook response JSON.

Fail-open: wrap everything; on error print {} and exit 0.
"""

from __future__ import annotations

import json
import os
import re
import sys


def extract_output(data: dict) -> str | None:
    """Extract tool output from hook input, handling multiple possible fields.

    Checks, in order:
    - data["tool_response"] (if str)
    - data["tool_response"]["stdout"]
    - data["tool_response"]["content"]
    - data["tool_response"]["output"]

    Returns str if found and non-empty, else None.
    """
    try:
        tool_response = data.get("tool_response")
        if tool_response is None:
            return None

        # Case 1: tool_response is a string
        if isinstance(tool_response, str):
            return tool_response if tool_response else None

        # Case 2: tool_response is a dict
        if isinstance(tool_response, dict):
            for key in ["stdout", "content", "output"]:
                val = tool_response.get(key)
                if val and isinstance(val, str):
                    return val

            # Case 3: Read tool shape — {"type":"text","file":{"filePath","content",...}}
            file_obj = tool_response.get("file")
            if isinstance(file_obj, dict):
                val = file_obj.get("content")
                if val and isinstance(val, str):
                    return val

        return None
    except Exception:
        return None


def shrink(text: str, cfg: dict) -> tuple[str, bool]:
    """Shrink text by keeping head + tail lines + error lines.

    Args:
        text: The text to potentially shrink.
        cfg: Config dict with keys:
            - truncate_min_lines_to_act: only shrink if >= this many lines
            - truncate_head_lines: number of head lines to keep
            - truncate_tail_lines: number of tail lines to keep
            - truncate_max_lines: max extra lines to keep from middle (for errors)
            - keep_error_regex: regex to identify error lines

    Returns:
        (shrunk_text, acted: bool)
        - acted=False if text was below threshold or didn't need shrinking.
        - acted=True if shrinking occurred.

    Edge cases:
    - Binary/no-newline blobs: treat as 1 line, hard-cut to first+last N chars.
    - Already-small text: no-op.
    - Non-UTF8: use errors="replace".
    """
    try:
        # Get config values
        min_lines_to_act = cfg.get("truncate_min_lines_to_act", 240)
        head_lines = cfg.get("truncate_head_lines", 120)
        tail_lines = cfg.get("truncate_tail_lines", 60)
        max_extra_lines = cfg.get("truncate_max_lines", 200)
        keep_error_regex = cfg.get("keep_error_regex", "(?i)(error|exception|traceback|failed|fatal|warning)")

        # Handle decoding errors
        if isinstance(text, bytes):
            text = text.decode("utf-8", errors="replace")

        # Split into lines
        lines = text.split("\n")

        # If no newlines (binary/single line) and huge, hard-cut by chars
        if len(lines) <= 1 and len(text) > 10000:
            # Hard-cut: first 5000 + last 5000 chars
            first = text[:5000]
            last = text[-5000:]
            return f"{first}\n... [nexum] omitted middle ...\n{last}", True

        # Check threshold
        if len(lines) < min_lines_to_act:
            return (text, False)

        # Shrinking logic: keep head + tail + error lines from middle
        try:
            error_pattern = re.compile(keep_error_regex)
        except Exception:
            error_pattern = None

        # Collect head lines
        kept_indices = set(range(min(head_lines, len(lines))))

        # Collect tail lines
        kept_indices.update(range(max(head_lines, len(lines) - tail_lines), len(lines)))

        # Collect error lines from the middle (between head and tail)
        error_lines_found = []
        for i in range(head_lines, len(lines) - tail_lines):
            if error_pattern and error_pattern.search(lines[i]):
                error_lines_found.append(i)

        # Keep up to max_extra_lines error lines
        error_lines_to_keep = error_lines_found[:max_extra_lines]
        kept_indices.update(error_lines_to_keep)

        # If we're keeping almost everything, don't act
        if len(kept_indices) >= len(lines) - 10:
            return (text, False)

        # Build output: keep in original order
        kept_lines = [lines[i] for i in sorted(kept_indices)]
        omitted_count = len(lines) - len(kept_indices)

        # Insert marker line
        marker = f"... [nexum] omitted {omitted_count} lines ..."

        # Insert marker after head lines (position = head_lines)
        kept_lines.insert(head_lines, marker)
        result = "\n".join(kept_lines)

        return (result, True)

    except Exception:
        # Fail-open: return original text, didn't act
        return (text, False)


def main() -> None:
    """PostToolUse hook: read stdin JSON, shrink if needed, emit hook response."""
    try:
        # Read JSON from stdin
        try:
            data = json.load(sys.stdin)
        except (json.JSONDecodeError, ValueError):
            # Not valid JSON; fail-open
            print("{}")
            return

        if not isinstance(data, dict):
            print("{}")
            return

        # Extract output
        output = extract_output(data)
        if output is None:
            print("{}")
            return

        # Get config
        try:
            # Try to import store from current script directory
            import os
            script_dir = os.path.dirname(os.path.abspath(__file__))
            if script_dir not in sys.path:
                sys.path.insert(0, script_dir)
            import store
            cfg = store.get_config()
        except Exception:
            # Fail-open: if we can't get config, don't act
            print("{}")
            return

        # Shrink
        shrunk, acted = shrink(output, cfg)

        if not acted:
            print("{}")
            return

        # Emit hook response
        response = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedToolOutput": shrunk
            }
        }
        print(json.dumps(response, sort_keys=True))

    except Exception:
        # Fail-open: any error → print {} and exit 0
        print("{}")


if __name__ == "__main__":
    main()
