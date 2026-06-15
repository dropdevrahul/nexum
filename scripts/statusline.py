#!/usr/bin/env python3
"""
statusline.py — Nexum Claude Code statusLine command.

Reads the session JSON on stdin and prints a single compact line summarizing
usage plus nexum's saved tokens. Fail-open: always exits 0, always prints
a non-empty line.
"""

from __future__ import annotations

import json
import os
import sys


# ---------------------------------------------------------------------------
# Bootstrap: make sure scripts/ dir is on sys.path so `import store` works
# when invoked as python3 scripts/statusline.py from any cwd.
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


def format_tokens(n: int) -> str:
    """Format a token count compactly.

    n < 1000  → str(n)
    otherwise → f"{n/1000:.1f}k"  (e.g. 16700 → "16.7k", 1000 → "1.0k")
    """
    if n < 1000:
        return str(n)
    return f"{n / 1000:.1f}k"


def render(data: dict, saved_tokens: int, warn_pct: int = 0, warn_tokens: int = 0) -> str:
    """Return ONE compact status line (no trailing newline).

    Parameters
    ----------
    data:         Parsed session JSON from Claude Code's statusLine hook.
    saved_tokens: Tokens saved by nexum this session (0 → omit the segment).
    warn_pct:     Context-usage percentage threshold above which a compaction
                  warning is appended. 0 means disabled (no warning).
    warn_tokens:  Absolute context token threshold above which a compaction
                  warning is appended. 0 means disabled (no warning).
    """
    model = (data.get("model") or {}).get("display_name") or "?"

    cw = data.get("context_window") or {}
    pct = int(cw.get("used_percentage") or 0)
    ctx_tok = (cw.get("total_input_tokens") or 0) + (cw.get("total_output_tokens") or 0)

    cost = float((data.get("cost") or {}).get("total_cost_usd") or 0.0)

    # Progress bar: 10 chars wide
    filled = max(0, min(10, round(pct / 10)))
    bar = "▓" * filled + "░" * (10 - filled)

    parts = [
        f"nexum {model}",
        f"{bar} {pct}%",
        f"{format_tokens(ctx_tok)} tok",
        f"${cost:.2f}",
    ]

    if saved_tokens and saved_tokens > 0:
        parts.append(f"saved {format_tokens(saved_tokens)}")

    should_warn = (warn_pct and warn_pct > 0 and pct >= warn_pct) or (warn_tokens and warn_tokens > 0 and ctx_tok >= warn_tokens)
    if should_warn:
        parts.append("⚠ /compact")

    return "  ·  ".join(parts)


def main() -> None:
    """StatusLine entry point. Reads stdin JSON, prints one line, exits 0."""
    try:
        raw = sys.stdin.read()
        data = json.loads(raw)
    except Exception:
        print("nexum")
        return

    if not isinstance(data, dict):
        print("nexum")
        return

    session_id = data.get("session_id") or "_nosession"

    saved = 0
    warn_pct = 80
    warn_tokens = 80000
    try:
        import store  # noqa: E402 (path already set above)
        saved = store.session_savings(session_id)
        warn_pct = int(store.get_config().get("statusline_compaction_warn_pct", 80))
        warn_tokens = int(store.get_config().get("statusline_compaction_warn_tokens", 80000))
    except Exception:
        saved = 0
        warn_pct = 80
        warn_tokens = 80000

    print(render(data, saved, warn_pct, warn_tokens))


if __name__ == "__main__":
    main()
