#!/usr/bin/env python3
"""
subagent_usage.py — Nexum SubagentStop hook.

Records a real per-tier usage row when a nexum executor subagent finishes, so
`cost_report.py`'s tiering breakdown reflects measured spend rather than the
plan-preview estimate.

Limitation (documented, not hidden): the SubagentStop payload carries NO token
usage fields — only the agent name and a transcript path. We therefore map the
agent name to its model tier and parse token totals best-effort from the
transcript (`store.transcript_usage_totals`). If the transcript is unavailable
or is the parent's rather than the subagent's, token counts may be 0 or
approximate; the row still records that the tier ran, which is strictly better
than a pure estimate. When Claude Code adds usage to the SubagentStop payload,
prefer that.

Only nexum's own executor agents are recorded (agent names starting with
`nexum-impl-`); other subagents are ignored.

Hook contract:
  stdin  → single JSON object (session_id, transcript_path, cwd, agent_type)
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

# Executor agent name → pricing tier key (matches store.PRICING).
_AGENT_TIER = {
    "nexum-impl-haiku": "haiku",
    "nexum-impl-sonnet": "sonnet",
    "nexum-impl-opus": "opus",
}


def _tier_for_agent(agent: str) -> str:
    """Return the pricing tier for a nexum executor agent name, or '' if N/A."""
    if not agent:
        return ""
    a = agent.strip()
    if a in _AGENT_TIER:
        return _AGENT_TIER[a]
    # Tolerate suffixes/namespacing (e.g. "plugin:nexum-impl-haiku").
    for name, tier in _AGENT_TIER.items():
        if a.endswith(name) or name in a:
            return tier
    return ""


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

        cfg = store.get_config()
        if not cfg.get("subagent_usage_enabled", True):
            print("{}")
            return

        agent = (
            data.get("agent_type")
            or data.get("agent_name")
            or data.get("subagent_type")
            or ""
        )
        tier = _tier_for_agent(agent)
        if not tier:
            print("{}")  # not a nexum executor — ignore
            return

        session_id = data.get("session_id") or "_nosession"
        transcript_path = data.get("transcript_path") or ""
        totals = store.transcript_usage_totals(transcript_path)

        store.add_usage(
            session_id=session_id,
            model=tier,
            input_tok=totals.get("input_tok", 0),
            output_tok=totals.get("output_tok", 0),
            cache_read_tok=totals.get("cache_read_tok", 0),
        )
    except Exception:
        pass
    print("{}")


if __name__ == "__main__":
    main()
