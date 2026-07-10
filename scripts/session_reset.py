#!/usr/bin/env python3
"""
session_reset.py — Nexum SessionStart housekeeping hook.

Three jobs on session start:

1. Invalidate predup state on a context reset. When `source` is `clear` (the
   user ran /clear) or `compact` (the session resumed after a compaction), the
   tool output predup keys off is gone — so clear this session's `tool_calls`
   rows to prevent predup denying a legitimate re-read. No-op for `startup`/
   `resume`, where prior context is still present.
2. Throttled retention prune (`store.maybe_prune`, at most once/day) so the
   SQLite file and predup lookups stay bounded across many sessions.
3. Capture the git toplevel of the session's cwd into session_kv under the
   "repo_root" flag, so store.session_rows()/agent_rows() can filter sessions
   and agents to one repo. Falls back to the raw cwd when not a git repo.

Filtering is done in-script on the `source` field rather than via a hook
matcher, so behaviour is identical regardless of SessionStart matcher support.

Hook contract:
  stdin  → single JSON object (session_id, source, ...)
  stdout → "{}" always
  exit 0 always (fail-open)
"""

import json
import os
import subprocess
import sys

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import store  # noqa: E402

_RESET_SOURCES = {"clear", "compact"}


def _git_toplevel(cwd: str) -> str:
    """Resolve the git toplevel of *cwd* (mirrors store.project_data_dir's
    subprocess pattern). Falls back to *cwd* itself on any error/non-repo."""
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return cwd


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

        # Tag this session with its repo toplevel so agent/session queries can
        # filter by repo. Fail-open: never blocks session start.
        try:
            cwd = data.get("cwd") or os.getcwd()
            repo_root = _git_toplevel(cwd)
            store.set_flag(session_id, "repo_root", repo_root)
        except Exception:
            pass

    except Exception:
        pass
    print("{}")


if __name__ == "__main__":
    main()
