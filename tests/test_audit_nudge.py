"""
test_audit_nudge.py — stdlib unittest tests for scripts/audit_nudge.py

Covers:
  (a) actionable findings (unignored noise dir, no ignore file) → output has /nx-audit
  (b) second run within the throttle window → {}
  (c) audit_nudge_enabled=false → {}
  (d) audit.run_audit raising → {} exit 0 (fail-open)
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

_SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

_AUDIT_NUDGE = os.path.join(_SCRIPTS_DIR, "audit_nudge.py")


def _make_dirty_cwd() -> str:
    """Create a temp dir with an unignored noise dir and no ignore file."""
    cwd = tempfile.mkdtemp()
    os.makedirs(os.path.join(cwd, "node_modules"), exist_ok=True)
    return cwd


def _run_nudge(payload: dict, data_dir: str, cwd: str, extra_config: dict = None):
    """Run audit_nudge.py and return (parsed_output, exit_code)."""
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = data_dir
    env["PYTHONPATH"] = _SCRIPTS_DIR + os.pathsep + env.get("PYTHONPATH", "")

    if extra_config is not None:
        with open(os.path.join(data_dir, "config.json"), "w") as fh:
            json.dump(extra_config, fh)

    result = subprocess.run(
        [sys.executable, _AUDIT_NUDGE],
        input=json.dumps(payload).encode(),
        capture_output=True,
        env=env,
        cwd=cwd,
        timeout=15,
    )
    return json.loads(result.stdout.decode()), result.returncode


class TestAuditNudgeFires(unittest.TestCase):
    """Actionable findings → output contains /nx-audit."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._cwd = _make_dirty_cwd()

    def test_fires_with_findings(self):
        out, rc = _run_nudge({"cwd": self._cwd}, self._tmp, self._cwd)
        self.assertEqual(rc, 0)
        self.assertNotEqual(out, {}, f"Expected a hint, got: {out}")
        self.assertIn("/nx-audit", json.dumps(out))


class TestAuditNudgeThrottle(unittest.TestCase):
    """Second run within the throttle window → {}."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._cwd = _make_dirty_cwd()

    def test_throttled_second_run(self):
        first, rc1 = _run_nudge({"cwd": self._cwd}, self._tmp, self._cwd)
        self.assertEqual(rc1, 0)
        self.assertNotEqual(first, {}, "First run should nudge")

        second, rc2 = _run_nudge({"cwd": self._cwd}, self._tmp, self._cwd)
        self.assertEqual(rc2, 0)
        self.assertEqual(second, {}, f"Second run should be throttled, got: {second}")


class TestAuditNudgeDisabled(unittest.TestCase):
    """audit_nudge_enabled=false → {}."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._cwd = _make_dirty_cwd()

    def test_disabled_suppresses(self):
        out, rc = _run_nudge({"cwd": self._cwd}, self._tmp, self._cwd,
                             extra_config={"audit_nudge_enabled": False})
        self.assertEqual(rc, 0)
        self.assertEqual(out, {}, f"Expected {{}} when disabled, got: {out}")


class TestAuditNudgeFailOpen(unittest.TestCase):
    """audit.run_audit raising → {} exit 0 (fail-open)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._cwd = _make_dirty_cwd()
        os.environ["CLAUDE_PLUGIN_DATA"] = self._tmp

    def tearDown(self):
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)

    def test_run_audit_raises_is_fail_open(self):
        import audit_nudge
        payload = json.dumps({"cwd": self._cwd})
        buf = io.StringIO()
        with mock.patch("audit.run_audit", side_effect=RuntimeError("boom")), \
                mock.patch("sys.stdin", io.StringIO(payload)), \
                redirect_stdout(buf):
            audit_nudge.main()
        self.assertEqual(buf.getvalue().strip(), "{}")


if __name__ == "__main__":
    unittest.main()
