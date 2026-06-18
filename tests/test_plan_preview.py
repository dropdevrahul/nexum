"""
test_plan_preview.py — stdlib unittest tests for scripts/plan_preview.py

Covers:
  (a) parse_plan_steps on 3-step sample returns correct routes
  (b) build_preview output contains "Plan cost preview" and "Projected:" with non-zero savings
  (c) main on missing path prints "No plan file" and exits 0
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
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import plan_preview
import store


_SAMPLE_PLAN = """
# Sample Plan

### Step 1: mechanical task
- route: mechanical
- scope: scripts/foo.py
- acceptance: python3 -c "print('ok')"

### Step 2: standard task
- route: standard
- scope: scripts/bar.py
- acceptance: python3 -c "print('ok')"

### Step 3: needs-strong task
- route: needs-strong
- scope: scripts/baz.py
- acceptance: python3 -c "print('ok')"
"""


class TestParsePlanSteps(unittest.TestCase):
    """parse_plan_steps correctly extracts index, title, and route."""

    def test_three_steps_returned(self):
        steps = plan_preview.parse_plan_steps(_SAMPLE_PLAN)
        self.assertEqual(len(steps), 3, f"Expected 3 steps, got: {steps}")

    def test_step_indices(self):
        steps = plan_preview.parse_plan_steps(_SAMPLE_PLAN)
        self.assertEqual(steps[0]["index"], 1)
        self.assertEqual(steps[1]["index"], 2)
        self.assertEqual(steps[2]["index"], 3)

    def test_step_routes(self):
        steps = plan_preview.parse_plan_steps(_SAMPLE_PLAN)
        self.assertEqual(steps[0]["route"], "mechanical")
        self.assertEqual(steps[1]["route"], "standard")
        self.assertEqual(steps[2]["route"], "needs-strong")

    def test_step_titles(self):
        steps = plan_preview.parse_plan_steps(_SAMPLE_PLAN)
        self.assertIn("mechanical task", steps[0]["title"])
        self.assertIn("standard task", steps[1]["title"])
        self.assertIn("needs-strong task", steps[2]["title"])

    def test_empty_plan_returns_empty(self):
        steps = plan_preview.parse_plan_steps("")
        self.assertEqual(steps, [])

    def test_no_steps_plan(self):
        steps = plan_preview.parse_plan_steps("# Just a header\n\nSome text.\n")
        self.assertEqual(steps, [])


class TestBuildPreview(unittest.TestCase):
    """build_preview returns a correctly formatted estimate string."""

    def setUp(self):
        self._cfg = store.get_config()

    def test_contains_plan_cost_preview(self):
        steps = plan_preview.parse_plan_steps(_SAMPLE_PLAN)
        output = plan_preview.build_preview(steps, self._cfg)
        self.assertIn("Plan cost preview", output)

    def test_contains_projected_line(self):
        steps = plan_preview.parse_plan_steps(_SAMPLE_PLAN)
        output = plan_preview.build_preview(steps, self._cfg)
        self.assertIn("Projected:", output)

    def test_savings_nonzero(self):
        """Haiku/sonnet are cheaper than opus so saves must be > 0."""
        steps = plan_preview.parse_plan_steps(_SAMPLE_PLAN)
        output = plan_preview.build_preview(steps, self._cfg)
        # Extract the saves amount from "saves $X.XXXX"
        import re
        m = re.search(r'saves \$([0-9.]+)', output)
        self.assertIsNotNone(m, f"Could not find 'saves $...' in output:\n{output}")
        saved = float(m.group(1))
        self.assertGreater(saved, 0.0, f"Expected non-zero savings, got: {saved}")

    def test_empty_steps_returns_no_steps_message(self):
        output = plan_preview.build_preview([], self._cfg)
        self.assertIn("No steps found", output)

    def test_all_opus_steps_zero_savings(self):
        """All needs-strong (opus) steps → 0 savings vs all-opus baseline."""
        steps = [
            {"index": 1, "title": "a", "route": "needs-strong"},
            {"index": 2, "title": "b", "route": "needs-strong"},
        ]
        output = plan_preview.build_preview(steps, self._cfg)
        import re
        m = re.search(r'saves \$([0-9.]+)', output)
        self.assertIsNotNone(m)
        saved = float(m.group(1))
        self.assertAlmostEqual(saved, 0.0, places=6)


class TestMainMissingPlan(unittest.TestCase):
    """main prints 'No plan file' and exits 0 when plan path doesn't exist."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_missing_plan_exit_0(self):
        missing_path = os.path.join(self._tmp, "nonexistent_plan.md")
        env = os.environ.copy()
        env["CLAUDE_PLUGIN_DATA"] = self._tmp
        env["PYTHONPATH"] = _SCRIPTS_DIR + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, os.path.join(_SCRIPTS_DIR, "plan_preview.py"),
             "--plan", missing_path],
            capture_output=True,
            env=env,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0)
        output = result.stdout.decode()
        self.assertIn("No plan file", output, f"Expected 'No plan file' in output: {output!r}")

    def test_real_plan_exit_0(self):
        """A valid plan file runs successfully."""
        plan_path = os.path.join(self._tmp, "test_plan.md")
        with open(plan_path, "w") as fh:
            fh.write(_SAMPLE_PLAN)

        env = os.environ.copy()
        env["CLAUDE_PLUGIN_DATA"] = self._tmp
        env["PYTHONPATH"] = _SCRIPTS_DIR + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, os.path.join(_SCRIPTS_DIR, "plan_preview.py"),
             "--plan", plan_path],
            capture_output=True,
            env=env,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0)
        output = result.stdout.decode()
        self.assertIn("Plan cost preview", output)
        self.assertIn("Projected:", output)


if __name__ == "__main__":
    unittest.main()
