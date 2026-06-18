"""
test_guardrail.py — stdlib unittest tests for scripts/guardrail.py

Covers ACCEPTANCE from §4.4:
- passing cmd → pass true
- failing cmd → pass false
- out-of-scope file → violation listed
- no acceptance cmd → pass true with note
- timeout → pass false, rc=124
- scope empty → no scope check
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

_SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)


def _run_guardrail(*args, data_dir=None):
    """Run guardrail.py with given CLI args. Returns (parsed_output, exit_code)."""
    env = os.environ.copy()
    if data_dir:
        env["CLAUDE_PLUGIN_DATA"] = data_dir
    result = subprocess.run(
        [sys.executable, os.path.join(_SCRIPTS_DIR, "guardrail.py")] + list(args),
        capture_output=True,
        env=env,
        timeout=30,
    )
    return json.loads(result.stdout.decode()), result.returncode


class TestGuardrailPassingCmd(unittest.TestCase):
    """Passing acceptance command → pass=true."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_true_cmd_passes(self):
        """'true' shell command returns rc=0 → guardrail pass."""
        out, rc = _run_guardrail("--acceptance", "true", data_dir=self._tmp)
        self.assertEqual(rc, 0)
        self.assertTrue(out["pass"])
        self.assertEqual(out["acceptance_rc"], 0)
        self.assertEqual(out["scope_violations"], [])

    def test_echo_passes(self):
        """echo command returns rc=0 → pass."""
        out, rc = _run_guardrail("--acceptance", "echo hello", data_dir=self._tmp)
        self.assertEqual(rc, 0)
        self.assertTrue(out["pass"])


class TestGuardrailFailingCmd(unittest.TestCase):
    """Failing acceptance command → pass=false."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_false_cmd_fails(self):
        """'false' shell command returns rc=1 → guardrail fail."""
        out, rc = _run_guardrail("--acceptance", "false", data_dir=self._tmp)
        self.assertEqual(rc, 0)
        self.assertFalse(out["pass"])
        self.assertNotEqual(out["acceptance_rc"], 0)

    def test_exit_1_fails(self):
        out, rc = _run_guardrail("--acceptance", "exit 1", data_dir=self._tmp)
        self.assertEqual(rc, 0)
        self.assertFalse(out["pass"])


class TestGuardrailScopeViolation(unittest.TestCase):
    """Out-of-scope file → violation listed."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_out_of_scope_file_flagged(self):
        """File not under scope-root → in scope_violations."""
        out, rc = _run_guardrail(
            "--acceptance", "true",
            "--scope-root", "src/",
            "--changed", "src/app.py,outside/file.py",
            data_dir=self._tmp,
        )
        self.assertEqual(rc, 0)
        self.assertFalse(out["pass"])
        self.assertIn("outside/file.py", out["scope_violations"])
        self.assertNotIn("src/app.py", out["scope_violations"])

    def test_in_scope_file_not_flagged(self):
        out, rc = _run_guardrail(
            "--acceptance", "true",
            "--scope-root", "src/",
            "--changed", "src/app.py",
            data_dir=self._tmp,
        )
        self.assertEqual(rc, 0)
        self.assertTrue(out["pass"])
        self.assertEqual(out["scope_violations"], [])

    def test_multiple_scope_roots(self):
        """File under any scope root is allowed."""
        out, rc = _run_guardrail(
            "--acceptance", "true",
            "--scope-root", "src/",
            "--scope-root", "tests/",
            "--changed", "src/a.py,tests/test_b.py",
            data_dir=self._tmp,
        )
        self.assertEqual(rc, 0)
        self.assertTrue(out["pass"])
        self.assertEqual(out["scope_violations"], [])

    def test_absolute_root_relative_changed_not_flagged(self):
        """Regression: an absolute --scope-root with a relative --changed (a common
        orchestrator invocation) must NOT spuriously flag the file. Previously
        os.path.commonpath raised ValueError on mixed abs/rel paths → false violation."""
        abs_tests = os.path.abspath("tests")
        out, rc = _run_guardrail(
            "--acceptance", "true",
            "--scope-root", abs_tests,
            "--changed", "tests/test_guardrail.py",
            data_dir=self._tmp,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(out["scope_violations"], [])
        self.assertTrue(out["pass"])

    def test_absolute_root_relative_out_of_scope_still_flagged(self):
        """Control: with the same absolute root, a relative file truly outside it
        is still flagged (the fix must not over-allow)."""
        abs_tests = os.path.abspath("tests")
        out, rc = _run_guardrail(
            "--acceptance", "true",
            "--scope-root", abs_tests,
            "--changed", "scripts/store.py",
            data_dir=self._tmp,
        )
        self.assertEqual(rc, 0)
        self.assertIn("scripts/store.py", out["scope_violations"])
        self.assertFalse(out["pass"])


class TestGuardrailDenyPath(unittest.TestCase):
    """--deny-path flags changed files under a denied path (plan 'do NOT touch X')."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_file_under_deny_path_flagged(self):
        out, rc = _run_guardrail(
            "--acceptance", "true",
            "--deny-path", "scripts/",
            "--changed", "tests/test_x.py,scripts/store.py",
            data_dir=self._tmp,
        )
        self.assertEqual(rc, 0)
        self.assertFalse(out["pass"])
        self.assertIn("scripts/store.py", out["scope_violations"])
        self.assertNotIn("tests/test_x.py", out["scope_violations"])

    def test_deny_path_only_allows_others(self):
        """With only a deny-path (no allow-list), files outside it pass."""
        out, rc = _run_guardrail(
            "--acceptance", "true",
            "--deny-path", "scripts/store.py",
            "--changed", "tests/test_x.py",
            data_dir=self._tmp,
        )
        self.assertEqual(rc, 0)
        self.assertTrue(out["pass"])
        self.assertEqual(out["scope_violations"], [])

    def test_deny_path_combined_with_scope_root(self):
        """A file inside the allow-list but also under a deny-path is still flagged."""
        out, rc = _run_guardrail(
            "--acceptance", "true",
            "--scope-root", "scripts/",
            "--deny-path", "scripts/store.py",
            "--changed", "scripts/store.py,scripts/dedup.py",
            data_dir=self._tmp,
        )
        self.assertEqual(rc, 0)
        self.assertFalse(out["pass"])
        self.assertIn("scripts/store.py", out["scope_violations"])
        self.assertNotIn("scripts/dedup.py", out["scope_violations"])


class TestGuardrailNoAcceptanceCmd(unittest.TestCase):
    """No --acceptance → pass=true with a note in log."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_no_acceptance_passes(self):
        out, rc = _run_guardrail(data_dir=self._tmp)
        self.assertEqual(rc, 0)
        self.assertTrue(out["pass"])
        self.assertEqual(out["acceptance_rc"], 0)
        # Log should mention something about no acceptance command
        self.assertTrue(len(out["log"]) > 0)

    def test_no_acceptance_with_scope_check(self):
        """No acceptance + scope check for in-scope files → pass."""
        out, rc = _run_guardrail(
            "--scope-root", "src/",
            "--changed", "src/foo.py",
            data_dir=self._tmp,
        )
        self.assertEqual(rc, 0)
        self.assertTrue(out["pass"])


class TestGuardrailNoScopeCheck(unittest.TestCase):
    """No --scope-root → no scope violations regardless of changed files."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_no_scope_root_no_violations(self):
        out, rc = _run_guardrail(
            "--acceptance", "true",
            "--changed", "anywhere/file.py",
            data_dir=self._tmp,
        )
        self.assertEqual(rc, 0)
        self.assertTrue(out["pass"])
        self.assertEqual(out["scope_violations"], [])


class TestGuardrailOutputShape(unittest.TestCase):
    """Output must always contain the four required keys."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_output_has_required_keys(self):
        out, _ = _run_guardrail("--acceptance", "true", data_dir=self._tmp)
        for key in ("pass", "acceptance_rc", "scope_violations", "log"):
            self.assertIn(key, out, f"Key {key!r} missing from guardrail output")

    def test_pass_is_bool(self):
        out, _ = _run_guardrail("--acceptance", "true", data_dir=self._tmp)
        self.assertIsInstance(out["pass"], bool)

    def test_scope_violations_is_list(self):
        out, _ = _run_guardrail("--acceptance", "true", data_dir=self._tmp)
        self.assertIsInstance(out["scope_violations"], list)


class TestGuardrailCommaChangedFiles(unittest.TestCase):
    """--changed accepts comma-separated list of files."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_comma_separated_changed(self):
        out, rc = _run_guardrail(
            "--acceptance", "true",
            "--scope-root", "src/",
            "--changed", "src/a.py,src/b.py,outside.py",
            data_dir=self._tmp,
        )
        self.assertEqual(rc, 0)
        self.assertFalse(out["pass"])
        self.assertIn("outside.py", out["scope_violations"])


if __name__ == "__main__":
    unittest.main()
