#!/usr/bin/env python3
"""
guardrail.py — CLI guardrail check for nexum.

Usage:
    python3 guardrail.py --acceptance "<cmd>" --scope-root <dir> [--scope-root <dir2>] \
                         --changed <f1,f2,...>

Runs the acceptance command and checks that the changed files respect scope. A
changed file is a violation if it is outside every allowed --scope-root, OR if it
is under any --deny-path. --deny-path mirrors a plan step's "scope: do NOT touch
X" language directly, so the orchestrator can pass the exclusions verbatim
instead of inverting them into an allow-list. Outputs a single JSON object.

Output shape:
    {
      "pass": bool,
      "acceptance_rc": int,
      "scope_violations": [...],
      "log": "<tail of combined stdout+stderr>"
    }

Edge cases (§4.4):
- No --acceptance given  → pass=true, acceptance_rc=0, note in log.
- Acceptance times out   → pass=false, acceptance_rc=124.
- No --scope-root given  → skip the allow-list check.
- No --deny-path given   → skip the deny check.
- No --changed given     → no files to check (scope_violations=[]).
"""

import argparse
import json
import os
import subprocess
import sys


# Maximum number of characters to keep in the "log" tail.
_LOG_TAIL_CHARS = 4096


def _flatten_csv(raw_values: list[str]) -> list[str]:
    """
    Accept repeatable CLI values that may themselves be comma-separated lists,
    expand them, and return a flat list of stripped, non-empty strings.
    """
    out: list[str] = []
    for entry in raw_values:
        for part in entry.split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out


# Backwards-compatible aliases (kept so existing imports/callers keep working).
_resolve_scope_roots = _flatten_csv
_resolve_changed_files = _flatten_csv


def _is_under_root(file_path: str, root: str) -> bool:
    """
    Return True if *file_path* is located under *root*.

    Comparison is done on normalised paths so that 'src/a.py' is considered
    under 'src' even when the caller omits a trailing slash.
    """
    # Normalise both sides to ABSOLUTE paths before comparing. The orchestrator
    # commonly passes a relative --changed (e.g. "tests/x.py") alongside an
    # absolute --scope-root (or vice versa); os.path.commonpath raises ValueError
    # when mixing absolute and relative paths, which previously surfaced as a
    # spurious scope violation. abspath() resolves both against the cwd so the
    # prefix check is consistent regardless of how the caller spelled the paths.
    norm_file = os.path.abspath(file_path)
    norm_root = os.path.abspath(root)

    # A file is under a root when the root is a path-component prefix of the
    # file path — i.e. the file's path starts with "<root>/".
    # Using os.path.commonpath avoids false matches like 'src2/a.py' ⊂ 'src'.
    try:
        common = os.path.commonpath([norm_file, norm_root])
    except ValueError:
        # Different drives on Windows — never a match.
        return False

    return common == norm_root


def _run_acceptance(cmd: str) -> tuple[int, str]:
    """
    Run *cmd* in a shell with a 120-second timeout.

    Returns (return_code, combined_log_tail).
    On timeout returns (124, <log so far>).
    """
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
        combined = result.stdout + b"\n" + result.stderr
        log_text = combined.decode("utf-8", errors="replace")
        # Keep only the tail.
        if len(log_text) > _LOG_TAIL_CHARS:
            log_text = "...(truncated)...\n" + log_text[-_LOG_TAIL_CHARS:]
        return result.returncode, log_text.strip()
    except subprocess.TimeoutExpired as exc:
        # Collect whatever output was captured before the timeout.
        out = exc.stdout or b""
        err = exc.stderr or b""
        combined = out + b"\n" + err
        log_text = combined.decode("utf-8", errors="replace").strip()
        if len(log_text) > _LOG_TAIL_CHARS:
            log_text = "...(truncated)...\n" + log_text[-_LOG_TAIL_CHARS:]
        return 124, log_text


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Guardrail: run acceptance check + scope diff."
    )
    parser.add_argument(
        "--acceptance",
        metavar="CMD",
        default=None,
        help="Shell command to run as the acceptance test.",
    )
    parser.add_argument(
        "--scope-root",
        dest="scope_roots",
        metavar="DIR",
        action="append",
        default=[],
        help=(
            "Allowed root directory (repeatable). "
            "Also accepts a comma-separated list of roots in a single value."
        ),
    )
    parser.add_argument(
        "--deny-path",
        dest="deny_paths",
        metavar="DIR",
        action="append",
        default=[],
        help=(
            "Path a changed file must NOT be under (repeatable; also accepts a "
            "comma-separated list). Mirrors a plan step's 'do NOT touch X' scope."
        ),
    )
    parser.add_argument(
        "--changed",
        metavar="FILES",
        action="append",
        default=[],
        help=(
            "Comma-separated list of changed file paths to scope-check "
            "(repeatable)."
        ),
    )

    args = parser.parse_args()

    # ------------------------------------------------------------------
    # 1. Acceptance command
    # ------------------------------------------------------------------
    acceptance_rc: int = 0
    log: str = ""
    passed_acceptance: bool = True

    if not args.acceptance:
        # No acceptance command supplied → trivially pass.
        log = "[nexum] No acceptance command provided; skipping acceptance check."
    else:
        acceptance_rc, log = _run_acceptance(args.acceptance)
        passed_acceptance = acceptance_rc == 0

    # ------------------------------------------------------------------
    # 2. Scope check
    # ------------------------------------------------------------------
    scope_roots = _flatten_csv(args.scope_roots)
    deny_paths = _flatten_csv(args.deny_paths)
    changed_files = _flatten_csv(args.changed)

    scope_violations: list[str] = []

    if changed_files:
        for f in changed_files:
            # Violation if outside every allow-list root...
            outside_allow = scope_roots and not any(
                _is_under_root(f, root) for root in scope_roots
            )
            # ...or under any explicitly denied path.
            under_deny = any(_is_under_root(f, deny) for deny in deny_paths)
            if outside_allow or under_deny:
                scope_violations.append(f)

    # ------------------------------------------------------------------
    # 3. Overall pass/fail
    # ------------------------------------------------------------------
    overall_pass: bool = passed_acceptance and len(scope_violations) == 0

    output = {
        "pass": overall_pass,
        "acceptance_rc": acceptance_rc,
        "scope_violations": scope_violations,
        "log": log,
    }

    print(json.dumps(output, sort_keys=True))


if __name__ == "__main__":
    main()
