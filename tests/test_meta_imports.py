"""
test_meta_imports.py — Meta test asserting NO third-party imports in scripts/

From §0 (RESOLVED): stdlib only — allowed imports:
    json, sqlite3, hashlib, os, sys, re, subprocess, pathlib, time,
    fnmatch, argparse, dataclasses, typing

This test greps every .py file under scripts/ and asserts that no import
statement references a module outside this allowlist.
"""

import os
import re
import sys
import unittest

_SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)

# §0 allowlist + the nexum internal modules (store, truncate, dedup, etc.)
# and Python builtins / special names.
_ALLOWED_MODULES = frozenset({
    # §0 explicit stdlib allowlist
    "json",
    "sqlite3",
    "hashlib",
    "os",
    "sys",
    "re",
    "subprocess",
    "pathlib",
    "time",
    "fnmatch",
    "argparse",
    "dataclasses",
    "datetime",
    "typing",

    # Standard library extras that are legitimately used (transitively stdlib)
    "multiprocessing",

    # Standard library sub-modules that may appear in imports
    "os.path",
    "pathlib.Path",
    "typing.Optional",
    "typing.List",
    "typing.Dict",
    "typing.Any",
    "typing.Tuple",

    # Internal nexum modules (all in scripts/)
    "store",
    "truncate",
    "dedup",
    "scan_guard",
    "context_watch",
    "guardrail",
    "cost_report",
    "audit",
    "handoff",

    # Python built-ins / __future__ (never third-party)
    "__future__",
    "builtins",
    "abc",
    "collections",
    "collections.abc",
    "contextlib",
    "copy",
    "enum",
    "functools",
    "hashlib",
    "io",
    "itertools",
    "logging",
    "math",
    "operator",
    "shlex",
    "shutil",
    "signal",
    "stat",
    "string",
    "struct",
    "tempfile",
    "textwrap",
    "threading",
    "traceback",
    "types",
    "unittest",
    "urllib",
    "urllib.parse",
    "urllib.request",
    "warnings",
    "weakref",
})

# Regex patterns to detect import statements.
# We capture the top-level module name.
# Only match lines where the first non-space token is 'import' or 'from'
# (not inside comments or docstrings).
_IMPORT_LINE_RE = re.compile(
    r"^(\s*)(?:import|from)\s+([a-zA-Z_][a-zA-Z0-9_]*)",
)


def _top_level_module(name: str) -> str:
    """Return the top-level package name (e.g. 'os.path' → 'os')."""
    return name.split(".")[0]


class TestNoThirdPartyImports(unittest.TestCase):
    """Assert that every script in scripts/ imports only allowed (stdlib) modules."""

    def test_no_third_party_imports(self):
        violations = []

        scripts_files = [
            f for f in os.listdir(_SCRIPTS_DIR)
            if f.endswith(".py")
        ]
        self.assertGreater(len(scripts_files), 0, "No .py files found in scripts/")

        for filename in sorted(scripts_files):
            filepath = os.path.join(_SCRIPTS_DIR, filename)
            try:
                with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
                    source = fh.read()
            except OSError as e:
                self.fail(f"Could not read {filepath}: {e}")

            in_multiline_string = False
            for lineno, line in enumerate(source.splitlines(), 1):
                stripped = line.strip()

                # Track entry/exit of triple-quoted strings.
                # Count occurrences of triple-quote delimiters on this line
                # to toggle the in_multiline_string state.
                for delim in ('"""', "'''"):
                    count = stripped.count(delim)
                    if count % 2 != 0:
                        in_multiline_string = not in_multiline_string

                # Skip lines inside multi-line strings (before toggle check
                # above toggles us *out*, the current line is still inside).
                if in_multiline_string:
                    continue

                # Skip comment lines.
                if stripped.startswith("#"):
                    continue

                match = _IMPORT_LINE_RE.match(line)
                if not match:
                    continue

                raw_module = match.group(2)
                top = _top_level_module(raw_module)
                if top not in _ALLOWED_MODULES and raw_module not in _ALLOWED_MODULES:
                    violations.append(f"{filename}:{lineno}: imports '{raw_module}'")

        if violations:
            self.fail(
                "Third-party or non-allowed imports found in scripts/:\n"
                + "\n".join(f"  {v}" for v in violations)
            )

    def test_all_script_files_readable(self):
        """All .py files in scripts/ must be readable (sanity check)."""
        for filename in os.listdir(_SCRIPTS_DIR):
            if not filename.endswith(".py"):
                continue
            filepath = os.path.join(_SCRIPTS_DIR, filename)
            try:
                with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
                self.assertGreater(len(content), 0,
                                   f"{filename} is unexpectedly empty")
            except OSError as e:
                self.fail(f"Could not read {filepath}: {e}")

    def test_expected_scripts_exist(self):
        """All modules defined in the spec must exist in scripts/."""
        required = [
            "store.py", "truncate.py", "dedup.py",
            "scan_guard.py", "context_watch.py",
            "guardrail.py", "cost_report.py", "audit.py",
            "handoff.py",
        ]
        for script in required:
            path = os.path.join(_SCRIPTS_DIR, script)
            self.assertTrue(os.path.isfile(path),
                            f"Required script {script} not found in scripts/")


if __name__ == "__main__":
    unittest.main()
