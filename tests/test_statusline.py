"""
test_statusline.py — stdlib unittest tests for scripts/statusline.py

Covers:
- format_tokens: boundary cases (< 1000, >= 1000)
- render: full data with saved tokens, empty data without saved tokens
- end-to-end subprocess: valid JSON input → exit 0, non-empty single line
- end-to-end subprocess: malformed JSON → exit 0, non-empty single line
"""

import json
import os
import subprocess
import sys
import unittest

_SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import statusline  # noqa: E402


class TestFormatTokens(unittest.TestCase):
    """Unit tests for format_tokens."""

    def test_below_1000(self):
        self.assertEqual(statusline.format_tokens(950), "950")

    def test_zero(self):
        self.assertEqual(statusline.format_tokens(0), "0")

    def test_999(self):
        self.assertEqual(statusline.format_tokens(999), "999")

    def test_1000(self):
        self.assertEqual(statusline.format_tokens(1000), "1.0k")

    def test_16700(self):
        self.assertEqual(statusline.format_tokens(16700), "16.7k")

    def test_1200(self):
        self.assertEqual(statusline.format_tokens(1200), "1.2k")


class TestRender(unittest.TestCase):
    """Unit tests for render."""

    def _full_data(self):
        return {
            "model": {"display_name": "Opus"},
            "context_window": {
                "used_percentage": 25,
                "total_input_tokens": 15000,
                "total_output_tokens": 1700,
            },
            "cost": {"total_cost_usd": 0.42},
        }

    def test_render_with_saved_tokens(self):
        result = statusline.render(self._full_data(), 1200)
        # Must be a single line (no embedded newlines)
        self.assertNotIn("\n", result)
        # Must contain required segments
        self.assertIn("Opus", result)
        self.assertIn("25%", result)
        self.assertIn("$0.42", result)
        self.assertIn("saved 1.2k", result)

    def test_render_with_saved_tokens_tok_count(self):
        result = statusline.render(self._full_data(), 1200)
        # total_input_tokens + total_output_tokens = 16700 → "16.7k tok"
        self.assertIn("16.7k tok", result)

    def test_render_no_saved_tokens(self):
        result = statusline.render({}, 0)
        self.assertNotIn("saved", result)
        self.assertIn("$0.00", result)

    def test_render_empty_dict_zero_saved(self):
        result = statusline.render({}, 0)
        # Must be non-empty
        self.assertTrue(result)
        # Must not contain "saved"
        self.assertNotIn("saved", result)

    def test_render_negative_saved_tokens_omits_segment(self):
        # saved_tokens <= 0 must not add the "saved" segment
        result = statusline.render(self._full_data(), -5)
        self.assertNotIn("saved", result)

    def test_render_bar_25pct(self):
        """25% fill → 3 filled + 7 empty (round(25/10)=3, but let's verify contract)."""
        result = statusline.render(self._full_data(), 0)
        # filled = round(25/10) = round(2.5) = 2 (banker's rounding) or 3 in Python 3
        # Python 3 banker's rounding: round(2.5) = 2
        filled = round(25 / 10)
        expected_bar = "▓" * filled + "░" * (10 - filled)
        self.assertIn(expected_bar, result)

    def test_render_0pct_bar(self):
        data = {"context_window": {"used_percentage": 0}}
        result = statusline.render(data, 0)
        self.assertIn("░" * 10, result)

    def test_render_100pct_bar(self):
        data = {"context_window": {"used_percentage": 100}}
        result = statusline.render(data, 0)
        self.assertIn("▓" * 10, result)

    def test_render_unknown_model(self):
        result = statusline.render({}, 0)
        self.assertIn("?", result)

    def test_render_separator(self):
        result = statusline.render(self._full_data(), 0)
        self.assertIn("  ·  ", result)

    def test_render_compaction_warn_above_threshold(self):
        """Context at 85% >= warn_pct=80 → warning appended."""
        result = statusline.render({"context_window": {"used_percentage": 85}}, 0, 80)
        self.assertIn("⚠ /compact", result)

    def test_render_compaction_warn_below_threshold(self):
        """Context at 50% < warn_pct=80 → no warning."""
        result = statusline.render({"context_window": {"used_percentage": 50}}, 0, 80)
        self.assertNotIn("⚠", result)

    def test_render_compaction_warn_disabled(self):
        """warn_pct=0 disables warning even at 99%."""
        result = statusline.render({"context_window": {"used_percentage": 99}}, 0, 0)
        self.assertNotIn("⚠", result)

    def test_render_compaction_warn_boundary_inclusive(self):
        """Context at exactly 80% with warn_pct=80 → warning fires (>= is inclusive)."""
        result = statusline.render({"context_window": {"used_percentage": 80}}, 0, 80)
        self.assertIn("⚠ /compact", result)


class TestSubprocessEndToEnd(unittest.TestCase):
    """End-to-end subprocess tests for statusline.py."""

    _statusline_path = os.path.join(_SCRIPTS_DIR, "statusline.py")

    def _run(self, input_bytes, env_override=None):
        env = os.environ.copy()
        env["PYTHONPATH"] = _SCRIPTS_DIR + os.pathsep + env.get("PYTHONPATH", "")
        if env_override:
            env.update(env_override)
        result = subprocess.run(
            [sys.executable, self._statusline_path],
            input=input_bytes,
            capture_output=True,
            env=env,
            timeout=15,
        )
        return result

    def test_valid_json_exit_0_nonempty_single_line(self):
        payload = {
            "model": {"display_name": "Opus"},
            "session_id": "s1",
            "context_window": {"used_percentage": 0},
            "cost": {"total_cost_usd": 0},
        }
        result = self._run(json.dumps(payload).encode())
        self.assertEqual(result.returncode, 0)
        stdout = result.stdout.decode().strip()
        self.assertTrue(stdout, "stdout must be non-empty")
        # Must be a single line (after stripping)
        self.assertEqual(len(stdout.splitlines()), 1, f"Expected single line, got: {stdout!r}")

    def test_malformed_json_exit_0_nonempty_stdout(self):
        result = self._run(b"not json")
        self.assertEqual(result.returncode, 0)
        stdout = result.stdout.decode().strip()
        self.assertTrue(stdout, "stdout must be non-empty even for bad input")

    def test_empty_input_exit_0_nonempty_stdout(self):
        result = self._run(b"")
        self.assertEqual(result.returncode, 0)
        stdout = result.stdout.decode().strip()
        self.assertTrue(stdout, "stdout must be non-empty for empty input")

    def test_valid_input_contains_model_name(self):
        payload = {
            "model": {"display_name": "Opus"},
            "session_id": "s1",
            "context_window": {"used_percentage": 0},
            "cost": {"total_cost_usd": 0},
        }
        result = self._run(json.dumps(payload).encode())
        self.assertEqual(result.returncode, 0)
        self.assertIn(b"Opus", result.stdout)


if __name__ == "__main__":
    unittest.main()
