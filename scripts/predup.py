"""
predup.py — Nexum PreToolUse pre-emptive dedup hook.

Denies (or asks on) a tool call whose normalised input was already executed
earlier this session. Recording a saving BEFORE the repeat runs is ungated —
a PreToolUse deny is actually honored by Claude Code, so the avoided
re-injection is a real saving, not a fictional PostToolUse one.

Hook contract:
  stdin  → single JSON object (Claude Code PreToolUse payload)
  stdout → single JSON object (deny shape or {})
  exit 0 always (fail-open)
"""

import json
import os
import shlex
import sys

# ---------------------------------------------------------------------------
# sys.path: ensure scripts/ dir is importable as "import store"
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import store  # noqa: E402


# ---------------------------------------------------------------------------
# Read-only Bash command allowlist for predup_bash_readonly mode.
# For "git" a second-token check is also required (see main()).
# ---------------------------------------------------------------------------
_BASH_READONLY = {"cat", "head", "tail", "ls", "wc", "grep", "rg", "egrep", "fgrep", "find", "git"}
_GIT_READONLY_SUBCMDS = {"log", "diff", "show", "status", "branch"}


def _bash_is_readonly(command: str) -> bool:
    """Return True if the Bash command is on the read-only allowlist."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    if not tokens:
        return False
    first = tokens[0]
    if first not in _BASH_READONLY:
        return False
    if first == "git":
        second = tokens[1] if len(tokens) > 1 else ""
        return second in _GIT_READONLY_SUBCMDS
    return True


def main() -> None:
    """PreToolUse hook entry point."""
    try:
        # ----------------------------------------------------------------
        # 1. Parse stdin JSON
        # ----------------------------------------------------------------
        try:
            raw = sys.stdin.read()
            data = json.loads(raw)
        except Exception:
            print("{}")
            return

        if not isinstance(data, dict):
            print("{}")
            return

        # ----------------------------------------------------------------
        # 2. Check config gate
        # ----------------------------------------------------------------
        cfg = store.get_config()
        if not cfg.get("predup_enabled", True):
            print("{}")
            return

        # ----------------------------------------------------------------
        # 3. Extract fields
        # ----------------------------------------------------------------
        session_id = data.get("session_id") or "_nosession"
        tool_name = data.get("tool_name") or ""
        tool_input = data.get("tool_input") or {}

        # ----------------------------------------------------------------
        # 4. Eligibility check
        # ----------------------------------------------------------------
        if tool_name in {"Read", "Grep", "Glob"}:
            eligible = True
        elif tool_name == "Bash":
            if cfg.get("predup_bash_readonly", False):
                command = tool_input.get("command", "") or ""
                eligible = _bash_is_readonly(command)
            else:
                eligible = False
        else:
            eligible = False

        if not eligible:
            print("{}")
            return

        # ----------------------------------------------------------------
        # 5. Compute signature and check for a prior call
        # ----------------------------------------------------------------
        sig = store.tool_call_sig(tool_name, tool_input)
        prior = store.seen_tool_call(session_id, sig)

        if prior is None:
            # First occurrence — nothing to dedup
            print("{}")
            return

        # ----------------------------------------------------------------
        # 6. Read-state guard (only for Read tool)
        # ----------------------------------------------------------------
        if tool_name == "Read":
            fp = tool_input.get("file_path")
            if fp and prior.get("mtime") is not None:
                try:
                    cur = os.path.getmtime(fp)
                    if cur != prior["mtime"]:
                        print("{}")
                        return
                except OSError:
                    # File gone or inaccessible → let the call through
                    print("{}")
                    return

        # ----------------------------------------------------------------
        # 7. Compute and record saving (ungated — deny IS honored)
        # ----------------------------------------------------------------
        raw_tok = int(prior.get("token_count") or 0)
        if raw_tok > 0:
            try:
                weight = float(cfg.get("dedup_cache_weight", 0.1))
                effective = max(0, round(raw_tok * weight))
                store.record_saving(session_id, "predup", raw_tok, effective)
            except Exception:
                pass

        # ----------------------------------------------------------------
        # 8. Build and emit the deny/ask decision
        # ----------------------------------------------------------------
        decision = cfg.get("predup_decision", "deny")
        if decision not in {"deny", "ask"}:
            decision = "deny"

        reason = (
            f"[nexum] identical {tool_name} call already ran earlier this session "
            f"(~{raw_tok} tok already in context) — reuse the earlier result "
            "instead of re-running."
        )

        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            }
        }
        print(json.dumps(out, sort_keys=True))

    except Exception:
        # Fail-open: never crash the Claude Code session
        print("{}")


if __name__ == "__main__":
    main()
