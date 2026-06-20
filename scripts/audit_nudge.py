"""
audit_nudge.py — Nexum SessionStart ignore-config nudge hook.

When the project's ignore config has actionable findings (missing ignore file,
unignored noise dirs, or large/binary files), surfaces a one-line
"/nx-audit" hint — throttled to once per repo per audit_nudge_throttle_hours.

Hook contract:
  stdin  → single JSON object (Claude Code SessionStart payload)
  stdout → single JSON object (hint or {})
  exit 0 always (fail-open)
"""

import json
import os
import sys
import time

# ---------------------------------------------------------------------------
# sys.path: ensure scripts/ dir is importable as "import store" / "import audit"
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import store  # noqa: E402
import audit  # noqa: E402


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
        if not cfg.get("audit_nudge_enabled", True):
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
        # 4. Throttle: once per repo per audit_nudge_throttle_hours
        # ----------------------------------------------------------------
        cwd = data.get("cwd") or os.getcwd()
        repo_key = store.current_repo(cwd)
        throttle_hours = float(cfg.get("audit_nudge_throttle_hours", 24))

        last_str = store.get_flag("_audit_nudge", repo_key)
        now = time.time()
        if last_str is not None:
            try:
                last_ts = float(last_str)
                if now - last_ts < throttle_hours * 3600:
                    print("{}")
                    return
            except ValueError:
                pass

        # ----------------------------------------------------------------
        # 5. Run audit
        # ----------------------------------------------------------------
        try:
            findings = audit.run_audit(cwd)
        except Exception:
            print("{}")
            return

        # ----------------------------------------------------------------
        # 6. Check for actionable findings
        # ----------------------------------------------------------------
        missing = findings.get("missing_ignore", False)
        noise_dirs = findings.get("unignored_noise_dirs") or []
        large_bin = findings.get("large_or_binary") or []

        if not missing and not noise_dirs and not large_bin:
            print("{}")
            return

        # ----------------------------------------------------------------
        # 7. Build hint
        # ----------------------------------------------------------------
        count = sum([bool(missing), bool(noise_dirs), bool(large_bin)])

        if noise_dirs:
            example = noise_dirs[0] + "/"
        elif missing:
            example = "no ignore file"
        else:
            # large_or_binary entries are tuples of (rel_path, size, is_binary)
            first = large_bin[0]
            example = first[0] if isinstance(first, (tuple, list)) else str(first)

        hint = (
            f"[nexum] Ignore-config has {count} issue(s) "
            f"(e.g. {example}); run /nx-audit."
        )

        out = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": hint,
            },
            "systemMessage": hint,
        }

        # ----------------------------------------------------------------
        # 8. Record throttle timestamp and emit
        # ----------------------------------------------------------------
        store.set_flag("_audit_nudge", repo_key, str(now))
        print(json.dumps(out, sort_keys=True))

    except Exception:
        # Fail-open: never crash the Claude Code session
        print("{}")


if __name__ == "__main__":
    main()
