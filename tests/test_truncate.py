"""
test_truncate.py — stdlib unittest tests for scripts/truncate.py

Covers ACCEPTANCE from §3.1:
- no-op under threshold
- head + tail + error-line retention
- omitted-count correct
- emits valid JSON
- never raises on weird input
- malformed-input fail-open case for the hook
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


# Default config matching store defaults (avoids importing store in unit tests
# of pure shrink() logic, though store is safe to import here).
_DEFAULT_CFG = {
    "truncate_max_lines": 200,
    "truncate_head_lines": 120,
    "truncate_tail_lines": 60,
    "truncate_min_lines_to_act": 240,
    "keep_error_regex": "(?i)(error|exception|traceback|failed|fatal|warning)",
}


def _make_lines(n, *, error_at=None):
    """Build a text block of n lines; optionally insert an error line at index."""
    lines = [f"line {i}" for i in range(n)]
    if error_at is not None and 0 <= error_at < n:
        lines[error_at] = "ERROR: something went wrong at this line"
    return "\n".join(lines)


class TestShrinkNoOp(unittest.TestCase):
    """shrink() must not act when the text is below the threshold."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["CLAUDE_PLUGIN_DATA"] = self._tmp

    def tearDown(self):
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)

    def test_below_threshold_returns_unchanged(self):
        import truncate
        text = _make_lines(100)  # well below 240
        shrunk, acted = truncate.shrink(text, _DEFAULT_CFG)
        self.assertFalse(acted)
        self.assertEqual(shrunk, text)

    def test_exactly_at_threshold_minus_one(self):
        """239 lines < 240 threshold — no-op."""
        import truncate
        text = _make_lines(239)
        _, acted = truncate.shrink(text, _DEFAULT_CFG)
        self.assertFalse(acted)

    def test_empty_string_no_op(self):
        import truncate
        _, acted = truncate.shrink("", _DEFAULT_CFG)
        self.assertFalse(acted)

    def test_single_line_no_op(self):
        import truncate
        _, acted = truncate.shrink("just one line", _DEFAULT_CFG)
        self.assertFalse(acted)


class TestShrinkActsAboveThreshold(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["CLAUDE_PLUGIN_DATA"] = self._tmp

    def tearDown(self):
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)

    def test_shrinks_large_text(self):
        import truncate
        text = _make_lines(500)  # well above 240
        shrunk, acted = truncate.shrink(text, _DEFAULT_CFG)
        self.assertTrue(acted)

    def test_head_lines_retained(self):
        """First head_lines lines must appear in the output."""
        import truncate
        text = _make_lines(500)
        lines_in = text.split("\n")
        shrunk, acted = truncate.shrink(text, _DEFAULT_CFG)
        self.assertTrue(acted)
        shrunk_lines = shrunk.split("\n")
        # First 120 original lines must appear
        for i in range(120):
            self.assertIn(lines_in[i], shrunk_lines,
                          f"Head line {i} missing from shrunk output")

    def test_tail_lines_retained(self):
        """Last tail_lines lines must appear in the output."""
        import truncate
        text = _make_lines(500)
        lines_in = text.split("\n")
        shrunk, acted = truncate.shrink(text, _DEFAULT_CFG)
        self.assertTrue(acted)
        shrunk_lines = shrunk.split("\n")
        for i in range(-60, 0):
            self.assertIn(lines_in[i], shrunk_lines,
                          f"Tail line {i} missing from shrunk output")

    def test_error_line_in_middle_retained(self):
        """Error lines from the middle section must be preserved."""
        import truncate
        # Put an error line in the middle (index 200 of 500)
        text = _make_lines(500, error_at=200)
        shrunk, acted = truncate.shrink(text, _DEFAULT_CFG)
        self.assertTrue(acted)
        self.assertIn("ERROR: something went wrong at this line", shrunk)

    def test_marker_present(self):
        """The omission marker must appear in the shrunk output."""
        import truncate
        text = _make_lines(500)
        shrunk, acted = truncate.shrink(text, _DEFAULT_CFG)
        self.assertTrue(acted)
        self.assertIn("[nexum] omitted", shrunk)

    def test_omitted_count_correct(self):
        """Marker must contain the correct omitted line count."""
        import truncate
        n = 500
        text = _make_lines(n)
        shrunk, acted = truncate.shrink(text, _DEFAULT_CFG)
        self.assertTrue(acted)
        # Count kept lines (head=120, tail=60, no error lines in non-error text)
        # marker line is extra — but omitted count = original - kept (excl marker)
        # Just verify there's a number in the marker
        import re
        match = re.search(r"omitted (\d+) lines", shrunk)
        self.assertIsNotNone(match, "Could not find omitted count in marker")
        omitted = int(match.group(1))
        self.assertGreater(omitted, 0)
        # Total kept + omitted should roughly equal original lines
        shrunk_without_marker = [l for l in shrunk.split("\n") if "[nexum] omitted" not in l]
        self.assertEqual(len(shrunk_without_marker) + omitted, n)


class TestShrinkEdgeCases(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["CLAUDE_PLUGIN_DATA"] = self._tmp

    def tearDown(self):
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)

    def test_binary_blob_no_newlines(self):
        """Single huge blob (no newlines) must be hard-cut or returned safely."""
        import truncate
        blob = "x" * 20000  # >10000 chars, no newlines
        shrunk, acted = truncate.shrink(blob, _DEFAULT_CFG)
        # Must not raise, must return a string
        self.assertIsInstance(shrunk, str)

    def test_never_raises_on_none_config_values(self):
        """shrink() must not raise even with an empty config dict."""
        import truncate
        text = _make_lines(500)
        try:
            shrunk, acted = truncate.shrink(text, {})
        except Exception as e:
            self.fail(f"shrink() raised with empty config: {e}")

    def test_preserves_original_order(self):
        """Lines must appear in original relative order in the output."""
        import truncate
        n = 500
        text = "\n".join([str(i) for i in range(n)])
        shrunk, acted = truncate.shrink(text, _DEFAULT_CFG)
        self.assertTrue(acted)
        # Extract numbered lines from output
        import re
        kept_nums = [int(l) for l in shrunk.split("\n") if re.fullmatch(r"\d+", l)]
        self.assertEqual(kept_nums, sorted(kept_nums),
                         "Lines are not in original order")


class TestExtractOutput(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["CLAUDE_PLUGIN_DATA"] = self._tmp

    def tearDown(self):
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)

    def test_string_tool_response(self):
        import truncate
        data = {"tool_response": "hello world"}
        self.assertEqual(truncate.extract_output(data), "hello world")

    def test_tool_response_stdout(self):
        import truncate
        data = {"tool_response": {"stdout": "stdout text"}}
        self.assertEqual(truncate.extract_output(data), "stdout text")

    def test_tool_response_content(self):
        import truncate
        data = {"tool_response": {"content": "content text"}}
        self.assertEqual(truncate.extract_output(data), "content text")

    def test_tool_response_output(self):
        import truncate
        data = {"tool_response": {"output": "output text"}}
        self.assertEqual(truncate.extract_output(data), "output text")

    def test_missing_tool_response_returns_none(self):
        import truncate
        self.assertIsNone(truncate.extract_output({}))

    def test_empty_string_returns_none(self):
        import truncate
        data = {"tool_response": ""}
        self.assertIsNone(truncate.extract_output(data))

    def test_none_tool_response_returns_none(self):
        import truncate
        data = {"tool_response": None}
        self.assertIsNone(truncate.extract_output(data))


class TestTruncateHookViaSubprocess(unittest.TestCase):
    """Drive truncate.py as a subprocess: stdin JSON → stdout JSON, exit 0."""

    def _run(self, payload):
        """Run truncate.py with payload piped to stdin. Returns (stdout_str, exit_code)."""
        env = os.environ.copy()
        env["CLAUDE_PLUGIN_DATA"] = tempfile.mkdtemp()
        env["PYTHONPATH"] = _SCRIPTS_DIR + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, os.path.join(_SCRIPTS_DIR, "truncate.py")],
            input=json.dumps(payload).encode(),
            capture_output=True,
            env=env,
            timeout=15,
        )
        return result.stdout.decode(), result.returncode

    def test_hook_noop_small_output(self):
        """Small output → emits {} and exit 0."""
        payload = {"tool_response": "small text\nonly two lines"}
        out, rc = self._run(payload)
        self.assertEqual(rc, 0)
        parsed = json.loads(out)
        self.assertEqual(parsed, {})

    def test_hook_acts_on_large_output(self):
        """Large output → emits hookSpecificOutput with updatedToolOutput."""
        large = "\n".join([f"line {i}" for i in range(500)])
        payload = {"tool_response": large}
        out, rc = self._run(payload)
        self.assertEqual(rc, 0)
        parsed = json.loads(out)
        self.assertIn("hookSpecificOutput", parsed)
        self.assertIn("updatedToolOutput", parsed["hookSpecificOutput"])

    def test_hook_valid_json_output(self):
        """Output is always valid JSON."""
        payload = {"tool_response": "hello"}
        out, rc = self._run(payload)
        self.assertEqual(rc, 0)
        # Must not raise
        json.loads(out)

    def test_hook_malformed_input_fail_open(self):
        """Malformed JSON input → emits {} and exit 0 (fail-open)."""
        env = os.environ.copy()
        env["CLAUDE_PLUGIN_DATA"] = tempfile.mkdtemp()
        result = subprocess.run(
            [sys.executable, os.path.join(_SCRIPTS_DIR, "truncate.py")],
            input=b"NOT JSON {{{",
            capture_output=True,
            env=env,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), b"{}")

    def test_hook_empty_input_fail_open(self):
        """Empty stdin → emits {} and exit 0."""
        env = os.environ.copy()
        env["CLAUDE_PLUGIN_DATA"] = tempfile.mkdtemp()
        result = subprocess.run(
            [sys.executable, os.path.join(_SCRIPTS_DIR, "truncate.py")],
            input=b"",
            capture_output=True,
            env=env,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), b"{}")

    def test_hook_no_tool_response_field(self):
        """Input with no tool_response → {} exit 0."""
        payload = {"session_id": "test", "tool_name": "Read"}
        out, rc = self._run(payload)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), {})


if __name__ == "__main__":
    unittest.main()
