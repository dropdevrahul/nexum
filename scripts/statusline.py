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
import time


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


def _bar(pct: float) -> str:
    """A 10-char progress bar filled to *pct* percent (▓ filled, ░ empty)."""
    filled = max(0, min(10, round(pct / 10)))
    return "▓" * filled + "░" * (10 - filled)


def format_reset(resets_at, now: float) -> str:
    """Format a rate-limit reset time as a compact 'time until reset' string.

    e.g. 2h05m, 45m, 1d03h, or 'now' if already past. Empty string if the
    timestamp is missing/unparseable.
    """
    try:
        delta = int(resets_at) - int(now)
    except (TypeError, ValueError):
        return ""
    if delta <= 0:
        return "now"
    mins = delta // 60
    hours, mins = divmod(mins, 60)
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}d{hours:02d}h"
    if hours:
        return f"{hours}h{mins:02d}m"
    return f"{mins}m"


def render(
    data: dict,
    saved_tokens: int,
    warn_pct: int = 0,
    warn_tokens: int = 0,
    plan_warn_pct: int = 0,
    now: float | None = None,
) -> str:
    """Return ONE compact status line (no trailing newline).

    Leads with the subscription plan's **5-hour session window** — how much of
    the rate limit is left and when it resets (`rate_limits.five_hour`) — since
    that is the limit that actually gates a working session. The 7-day window is
    shown compactly when present. When `rate_limits` is absent (e.g. API-key
    users), falls back to context-window usage.

    Parameters
    ----------
    data:         Parsed session JSON from Claude Code's statusLine hook.
    saved_tokens: Tokens saved by nexum this session (0 → omit the segment).
    warn_pct:     Context-usage percentage threshold above which a context
                  warning is appended. 0 means disabled.
    warn_tokens:  Absolute context token threshold above which a context
                  warning is appended. 0 means disabled.
    now:          Current epoch seconds (for reset countdowns); defaults to
                  time.time(). Injectable for deterministic tests.
    """
    if now is None:
        now = time.time()

    model = (data.get("model") or {}).get("display_name") or "?"
    cost = float((data.get("cost") or {}).get("total_cost_usd") or 0.0)

    cw = data.get("context_window") or {}
    pct = int(cw.get("used_percentage") or 0)
    ctx_tok = (cw.get("total_input_tokens") or 0) + (cw.get("total_output_tokens") or 0)

    parts = [f"nexum {model}"]

    rate_limits = data.get("rate_limits") or {}
    five = rate_limits.get("five_hour") or {}
    seven = rate_limits.get("seven_day") or {}
    five_used = five.get("used_percentage")

    if five_used is not None:
        # Subscription plan mode: usage LEFT for the current 5-hour window.
        # The bar fills with REMAINING budget (full bar = plenty left).
        left = max(0, min(100, int(round(100 - float(five_used)))))
        seg = f"{_bar(left)} {left}% left"
        reset = format_reset(five.get("resets_at"), now)
        if reset:
            seg += f" · ↻{reset}"
        parts.append(seg)

        seven_used = seven.get("used_percentage")
        if seven_used is not None:
            left7 = max(0, min(100, int(round(100 - float(seven_used)))))
            parts.append(f"wk {left7}%")

        # Context-window usage, shown compactly alongside the plan window.
        parts.append(f"ctx {pct}% · {format_tokens(ctx_tok)}")
    else:
        # Fallback: no plan rate-limit data → context-window usage is primary.
        parts.append(f"{_bar(pct)} {pct}%")
        parts.append(f"{format_tokens(ctx_tok)} tok")

    parts.append(f"${cost:.2f}")

    if saved_tokens and saved_tokens > 0:
        parts.append(f"saved {format_tokens(saved_tokens)}")

    # Context near limit → suggest /compact or a handoff to a fresh session.
    should_warn = (warn_pct and warn_pct > 0 and pct >= warn_pct) or (
        warn_tokens and warn_tokens > 0 and ctx_tok >= warn_tokens
    )
    if should_warn:
        parts.append("⚠ /compact · /nx-save")

    # 5-hour plan window nearly exhausted → suggest a handoff so work can resume
    # in a fresh session after the window resets.
    if (
        five_used is not None
        and plan_warn_pct
        and plan_warn_pct > 0
        and float(five_used) >= plan_warn_pct
    ):
        parts.append("⚠ plan low · /nx-save")

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
        _capture_session_cost(store, session_id, data)
    except Exception:
        saved = 0
        warn_pct = 80
        warn_tokens = 80000

    print(render(data, saved, warn_pct, warn_tokens))


def _capture_session_cost(store, session_id: str, data: dict) -> None:
    """Snapshot Claude Code's own metered cost/usage for this session.

    `cost.total_cost_usd` is the authoritative bill (cache-accurate); the
    context_window token counts are cumulative. Cache token fields are read
    opportunistically — captured if Claude Code exposes them, ignored if not.
    Fail-open: a missing field or store error must never affect the status line.
    """
    try:
        cost = data.get("cost") or {}
        cw = data.get("context_window") or {}
        model = (data.get("model") or {}).get("display_name") or "?"
        store.upsert_session_cost(
            session_id=session_id,
            model=model,
            cost_usd=cost.get("total_cost_usd") or 0.0,
            input_tok=cw.get("total_input_tokens") or 0,
            output_tok=cw.get("total_output_tokens") or 0,
            cache_read_tok=cw.get("cache_read_input_tokens")
            or cw.get("total_cache_read_tokens") or 0,
            cache_creation_tok=cw.get("cache_creation_input_tokens")
            or cw.get("total_cache_creation_tokens") or 0,
        )
        # Persist the REAL context size so context_watch can trigger its
        # handoff/compaction thresholds off Claude Code's own measurement
        # rather than its crude per-prompt token estimate (which omits tool
        # output and so massively undercounts). This is the same value the
        # status line shows as `ctx … tok`.
        real_ctx_tok = int(cw.get("total_input_tokens") or 0) + int(
            cw.get("total_output_tokens") or 0
        )
        if real_ctx_tok > 0:
            store.set_flag(session_id, "real_context_tokens", str(real_ctx_tok))
        real_pct = int(cw.get("used_percentage") or 0)
        if real_pct > 0:
            store.set_flag(session_id, "real_context_pct", str(real_pct))
    except Exception:
        pass


if __name__ == "__main__":
    main()
