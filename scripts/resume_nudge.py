"""
resume_nudge.py — Nexum SessionStart resume hint hook.

When a recent handoff for the current branch exists, surfaces a one-line
"resume available — run /nx-load" hint without auto-loading anything.

Hook contract:
  stdin  → single JSON object (Claude Code SessionStart payload)
  stdout → single JSON object (hint or {})
  exit 0 always (fail-open)
"""

import datetime
import json
import os
import re
import subprocess
import sys

# ---------------------------------------------------------------------------
# sys.path: ensure scripts/ dir is importable as "import store"
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import store  # noqa: E402


def main() -> None:
    """SessionStart hook entry point."""
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
        if not cfg.get("resume_nudge_enabled", True):
            print("{}")
            return

        # ----------------------------------------------------------------
        # 3. Skip continued sessions (already resumed or compacted)
        # ----------------------------------------------------------------
        source = data.get("source")
        if source in {"resume", "compact"}:
            print("{}")
            return

        # ----------------------------------------------------------------
        # 4. Locate the latest handoff file
        # ----------------------------------------------------------------
        cwd = data.get("cwd") or os.getcwd()
        data_dir = store.project_data_dir(cwd)
        latest = os.path.join(data_dir, "handoff", "latest.md")
        if not os.path.isfile(latest):
            print("{}")
            return

        # ----------------------------------------------------------------
        # 5. Parse header from the handoff file
        # ----------------------------------------------------------------
        try:
            with open(latest, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except Exception:
            print("{}")
            return

        branch_m = re.search(r'\*\*Branch:\*\*\s*`?([^\s`*]+)', content)
        written_m = re.search(r'\*\*Written:\*\*\s*([0-9T:+\-]+)', content)

        if not branch_m or not written_m:
            print("{}")
            return

        branch = branch_m.group(1).strip()
        written = written_m.group(1).strip()

        # ----------------------------------------------------------------
        # 6. Freshness check
        # ----------------------------------------------------------------
        try:
            written_dt = datetime.datetime.fromisoformat(written)
            now_dt = datetime.datetime.now().astimezone()
            age = now_dt - written_dt
            max_age_hours = float(cfg.get("resume_nudge_max_age_hours", 24))
            if age.total_seconds() > max_age_hours * 3600:
                print("{}")
                return
        except Exception:
            print("{}")
            return

        # ----------------------------------------------------------------
        # 7. Branch match
        # ----------------------------------------------------------------
        try:
            result = subprocess.run(
                ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            current_branch = result.stdout.strip()
        except Exception:
            current_branch = ""

        if current_branch and current_branch != branch:
            print("{}")
            return

        # ----------------------------------------------------------------
        # 8. Emit the hint
        # ----------------------------------------------------------------
        hint = (
            f"[nexum] Resume available: a handoff for branch '{branch}' was written "
            f"{written} — run /nx-load to continue. (Not loaded automatically.)"
        )

        out = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": hint + " Do not load it unless the user asks.",
            },
            "systemMessage": hint,
        }
        print(json.dumps(out, sort_keys=True))

    except Exception:
        # Fail-open: never crash the Claude Code session
        print("{}")


if __name__ == "__main__":
    main()
