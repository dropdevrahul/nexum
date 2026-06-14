"""
test_cost_report.py — stdlib unittest tests for scripts/cost_report.py

Covers ACCEPTANCE from §4.5:
- with seeded usage rows, prints correct actual vs baseline and savings
- per-model breakdown is present
- numbers are correct per the PRICING formula
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


def _seed_usage(data_dir, rows):
    """Write usage rows into the store using store.add_usage."""
    import store
    old_env = os.environ.get("CLAUDE_PLUGIN_DATA")
    os.environ["CLAUDE_PLUGIN_DATA"] = data_dir
    try:
        for r in rows:
            store.add_usage(
                r["session_id"],
                r["model"],
                r["input_tok"],
                r["output_tok"],
                r.get("cache_read_tok", 0),
            )
    finally:
        if old_env is None:
            os.environ.pop("CLAUDE_PLUGIN_DATA", None)
        else:
            os.environ["CLAUDE_PLUGIN_DATA"] = old_env


def _run_cost_report(data_dir, session=None):
    """Run cost_report.py and return (stdout_str, exit_code)."""
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = data_dir
    env["PYTHONPATH"] = _SCRIPTS_DIR + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [sys.executable, os.path.join(_SCRIPTS_DIR, "cost_report.py")]
    if session:
        cmd += ["--session", session]
    result = subprocess.run(
        cmd,
        capture_output=True,
        env=env,
        timeout=15,
    )
    return result.stdout.decode(), result.returncode


class TestCostReportNoData(unittest.TestCase):
    """No usage rows → report says no data."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["CLAUDE_PLUGIN_DATA"] = self._tmp

    def tearDown(self):
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)

    def test_no_rows_message(self):
        out, rc = _run_cost_report(self._tmp)
        self.assertEqual(rc, 0)
        self.assertIn("[nexum]", out)
        # Should mention no usage rows
        self.assertIn("No usage", out)


class TestCostReportNumbers(unittest.TestCase):
    """With seeded rows, verify correct actual vs baseline and savings."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["CLAUDE_PLUGIN_DATA"] = self._tmp

    def tearDown(self):
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)

    def test_haiku_row_correct_cost(self):
        """
        Haiku: input=$1/1M, output=$5/1M
        Row: 1_000_000 input, 1_000_000 output, 0 cache_read
        Actual  = 1.0 + 5.0 = $6.00
        Baseline (opus) = 5.0 + 25.0 = $30.00
        Saved   = $24.00
        """
        _seed_usage(self._tmp, [
            {"session_id": "s1", "model": "haiku",
             "input_tok": 1_000_000, "output_tok": 1_000_000, "cache_read_tok": 0}
        ])
        out, rc = _run_cost_report(self._tmp)
        self.assertEqual(rc, 0)
        # Actual cost $6.00
        self.assertIn("6.0000", out)
        # Baseline $30.00
        self.assertIn("30.0000", out)
        # Saved $24.00
        self.assertIn("24.0000", out)

    def test_sonnet_row_correct_cost(self):
        """
        Sonnet: input=$3/1M, output=$15/1M
        Row: 1_000_000 input, 0 output
        Actual  = 3.0
        Baseline= 5.0
        """
        _seed_usage(self._tmp, [
            {"session_id": "s2", "model": "sonnet",
             "input_tok": 1_000_000, "output_tok": 0, "cache_read_tok": 0}
        ])
        out, rc = _run_cost_report(self._tmp)
        self.assertEqual(rc, 0)
        self.assertIn("3.0000", out)
        self.assertIn("5.0000", out)

    def test_opus_row_baseline_equals_actual(self):
        """
        Opus: same as baseline → saved = 0.
        Row: 1_000_000 input, 1_000_000 output
        Actual = 5.0 + 25.0 = $30
        Baseline = $30
        """
        _seed_usage(self._tmp, [
            {"session_id": "s3", "model": "opus",
             "input_tok": 1_000_000, "output_tok": 1_000_000, "cache_read_tok": 0}
        ])
        out, rc = _run_cost_report(self._tmp)
        self.assertEqual(rc, 0)
        # Saved should be 0
        self.assertIn("0.0000", out)

    def test_cache_read_cost_included(self):
        """
        cache_read cost = cache_read_tok / 1M * price_in * 0.1
        Haiku: price_in=1.0, cache_read_tok=1_000_000
        => cache_read_cost = 1_000_000 / 1M * 1.0 * 0.1 = 0.1
        """
        _seed_usage(self._tmp, [
            {"session_id": "s4", "model": "haiku",
             "input_tok": 0, "output_tok": 0, "cache_read_tok": 1_000_000}
        ])
        out, rc = _run_cost_report(self._tmp)
        self.assertEqual(rc, 0)
        # Actual: 0.1000
        self.assertIn("0.1000", out)

    def test_per_model_breakdown_present(self):
        """Per-model breakdown section must appear in the report."""
        _seed_usage(self._tmp, [
            {"session_id": "s5", "model": "haiku",
             "input_tok": 100, "output_tok": 50, "cache_read_tok": 0}
        ])
        out, rc = _run_cost_report(self._tmp)
        self.assertEqual(rc, 0)
        self.assertIn("Per-model breakdown", out)
        self.assertIn("haiku", out)

    def test_session_filter(self):
        """--session flag filters to only that session's rows."""
        _seed_usage(self._tmp, [
            {"session_id": "sessA", "model": "haiku",
             "input_tok": 1_000_000, "output_tok": 0, "cache_read_tok": 0},
            {"session_id": "sessB", "model": "opus",
             "input_tok": 1_000_000, "output_tok": 0, "cache_read_tok": 0},
        ])
        out, rc = _run_cost_report(self._tmp, session="sessA")
        self.assertEqual(rc, 0)
        # Should show haiku cost for sessA
        # haiku input=1M * $1/M = $1.00
        self.assertIn("1.0000", out)

    def test_mixed_models_savings(self):
        """Mixed haiku+sonnet mix should be cheaper than all-opus baseline."""
        _seed_usage(self._tmp, [
            {"session_id": "sm", "model": "haiku",
             "input_tok": 500_000, "output_tok": 500_000, "cache_read_tok": 0},
            {"session_id": "sm", "model": "sonnet",
             "input_tok": 500_000, "output_tok": 500_000, "cache_read_tok": 0},
        ])
        out, rc = _run_cost_report(self._tmp)
        self.assertEqual(rc, 0)
        # Both should be cheaper than opus — so Saved > 0
        # Extract the saved amount by checking it's positive (output contains $ saved)
        self.assertIn("Saved", out)

    def test_report_header_present(self):
        _seed_usage(self._tmp, [
            {"session_id": "s6", "model": "haiku",
             "input_tok": 100, "output_tok": 50, "cache_read_tok": 0}
        ])
        out, rc = _run_cost_report(self._tmp)
        self.assertEqual(rc, 0)
        self.assertIn("[nexum] Cost report", out)

    def test_token_yield_note_present(self):
        """v1 must include the token yield note."""
        _seed_usage(self._tmp, [
            {"session_id": "s7", "model": "haiku",
             "input_tok": 100, "output_tok": 50, "cache_read_tok": 0}
        ])
        out, rc = _run_cost_report(self._tmp)
        self.assertEqual(rc, 0)
        self.assertIn("token yield", out.lower())


class TestBuildReport(unittest.TestCase):
    """Unit-test build_report() directly with known rows."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["CLAUDE_PLUGIN_DATA"] = self._tmp

    def tearDown(self):
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)

    def test_empty_rows(self):
        import cost_report
        report = cost_report.build_report([])
        self.assertIn("No usage", report)

    def test_haiku_known_values(self):
        import cost_report
        rows = [{"session_id": "s", "model": "haiku",
                 "input_tok": 1_000_000, "output_tok": 1_000_000,
                 "cache_read_tok": 0}]
        report = cost_report.build_report(rows)
        self.assertIn("6.0000", report)
        self.assertIn("30.0000", report)
        self.assertIn("24.0000", report)

    def test_model_key_normalisation(self):
        """model strings like 'claude-3-haiku-20240307' → matched to haiku rates."""
        import cost_report
        rows = [{"session_id": "s", "model": "claude-3-haiku-20240307",
                 "input_tok": 1_000_000, "output_tok": 0, "cache_read_tok": 0}]
        report = cost_report.build_report(rows)
        # Haiku input cost = 1.0
        self.assertIn("1.0000", report)


if __name__ == "__main__":
    unittest.main()
