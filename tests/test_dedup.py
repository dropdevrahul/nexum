"""
test_dedup.py — stdlib unittest tests for scripts/dedup.py

Covers ACCEPTANCE from §3.2:
- first occurrence stored + shrunk (emits updatedToolOutput)
- exact repeat → pointer, no body
- changed content → not collapsed
- tiny output → untouched ({})
- malformed-input fail-open
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

# Large enough to trigger dedup (>= 30 lines or >= 2000 chars)
_LARGE_TEXT = "\n".join([f"line {i}: some content here" for i in range(50)])
_TINY_TEXT = "tiny"


def _run_dedup(payload, data_dir=None):
    """Run dedup.py subprocess and return (parsed_output, exit_code)."""
    env = os.environ.copy()
    if data_dir:
        env["CLAUDE_PLUGIN_DATA"] = data_dir
    env["PYTHONPATH"] = _SCRIPTS_DIR + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, os.path.join(_SCRIPTS_DIR, "dedup.py")],
        input=json.dumps(payload).encode(),
        capture_output=True,
        env=env,
        timeout=15,
    )
    return json.loads(result.stdout.decode()), result.returncode


class TestDedupTinyOutput(unittest.TestCase):
    """Tiny outputs (< 30 lines AND < 2000 chars) must be left untouched."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_tiny_string_passthrough(self):
        payload = {
            "session_id": "s1",
            "tool_name": "Read",
            "tool_response": _TINY_TEXT,
        }
        out, rc = _run_dedup(payload, self._tmp)
        self.assertEqual(rc, 0)
        # Should emit {}
        self.assertEqual(out, {})

    def test_small_multiline_passthrough(self):
        """5 lines and < 2000 chars → no dedup."""
        text = "\n".join(["line"] * 5)
        payload = {
            "session_id": "s1",
            "tool_name": "Read",
            "tool_response": text,
        }
        out, rc = _run_dedup(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertEqual(out, {})


class TestDedupFirstOccurrence(unittest.TestCase):
    """First time a large output is seen: store and emit (possibly shrunk) content."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_first_occurrence_stored_and_emitted(self):
        payload = {
            "session_id": "sess_first",
            "tool_name": "Read",
            "tool_response": _LARGE_TEXT,
        }
        out, rc = _run_dedup(payload, self._tmp)
        self.assertEqual(rc, 0)
        # Must emit hookSpecificOutput with updatedToolOutput
        self.assertIn("hookSpecificOutput", out)
        hook_out = out["hookSpecificOutput"]
        self.assertIn("updatedToolOutput", hook_out)
        # updatedToolOutput should be non-empty
        self.assertTrue(hook_out["updatedToolOutput"])


class TestDedupRepeat(unittest.TestCase):
    """Exact repeat of a large output → pointer collapse, no body."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_exact_repeat_becomes_pointer(self):
        payload = {
            "session_id": "sess_repeat",
            "tool_name": "Read",
            "tool_response": _LARGE_TEXT,
        }
        # First call — stores the output
        out1, rc1 = _run_dedup(payload, self._tmp)
        self.assertEqual(rc1, 0)

        # Second call with IDENTICAL content
        out2, rc2 = _run_dedup(payload, self._tmp)
        self.assertEqual(rc2, 0)

        # Second output must be a pointer
        self.assertIn("hookSpecificOutput", out2)
        updated = out2["hookSpecificOutput"]["updatedToolOutput"]
        self.assertIn("[nexum] identical", updated)
        self.assertIn("omitted to save context", updated)

    def test_pointer_contains_hash_prefix(self):
        """Pointer message must include the first 8 chars of the hash."""
        import store
        payload = {
            "session_id": "sess_hash_check",
            "tool_name": "Bash",
            "tool_response": _LARGE_TEXT,
        }
        # First call
        _run_dedup(payload, self._tmp)
        # Second call
        out, _ = _run_dedup(payload, self._tmp)
        updated = out["hookSpecificOutput"]["updatedToolOutput"]
        expected_hash = store.sha256(_LARGE_TEXT)[:8]
        self.assertIn(expected_hash, updated)


class TestDedupChangedContent(unittest.TestCase):
    """Changed content must NOT be collapsed (different hash)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_changed_content_not_collapsed(self):
        base = _LARGE_TEXT
        modified = base + "\nextra line that changes the hash"

        payload1 = {
            "session_id": "sess_changed",
            "tool_name": "Read",
            "tool_response": base,
        }
        payload2 = {
            "session_id": "sess_changed",
            "tool_name": "Read",
            "tool_response": modified,
        }
        # First call
        _run_dedup(payload1, self._tmp)
        # Second call with different content
        out, rc = _run_dedup(payload2, self._tmp)
        self.assertEqual(rc, 0)
        # Must NOT be a pointer
        self.assertIn("hookSpecificOutput", out)
        updated = out["hookSpecificOutput"]["updatedToolOutput"]
        self.assertNotIn("[nexum] identical", updated,
                         "Changed content was wrongly collapsed to a pointer")


class TestDedupFailOpen(unittest.TestCase):
    """Malformed input → {} exit 0 (fail-open)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def _run_raw(self, raw_bytes):
        env = os.environ.copy()
        env["CLAUDE_PLUGIN_DATA"] = self._tmp
        env["PYTHONPATH"] = _SCRIPTS_DIR + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, os.path.join(_SCRIPTS_DIR, "dedup.py")],
            input=raw_bytes,
            capture_output=True,
            env=env,
            timeout=15,
        )
        return result.stdout.strip(), result.returncode

    def test_malformed_json_fail_open(self):
        out, rc = self._run_raw(b"NOT JSON {{{{")
        self.assertEqual(rc, 0)
        self.assertEqual(out, b"{}")

    def test_empty_input_fail_open(self):
        out, rc = self._run_raw(b"")
        self.assertEqual(rc, 0)
        self.assertEqual(out, b"{}")

    def test_non_dict_json_fail_open(self):
        out, rc = self._run_raw(b"[1,2,3]")
        self.assertEqual(rc, 0)
        self.assertEqual(out, b"{}")


class TestDedupValidJson(unittest.TestCase):
    """All outputs from dedup.py must be valid JSON."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def _run(self, payload):
        env = os.environ.copy()
        env["CLAUDE_PLUGIN_DATA"] = self._tmp
        env["PYTHONPATH"] = _SCRIPTS_DIR + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, os.path.join(_SCRIPTS_DIR, "dedup.py")],
            input=json.dumps(payload).encode(),
            capture_output=True,
            env=env,
            timeout=15,
        )
        return result.stdout.decode(), result.returncode

    def test_large_output_valid_json(self):
        payload = {
            "session_id": "json_test",
            "tool_name": "Read",
            "tool_response": _LARGE_TEXT,
        }
        out, rc = self._run(payload)
        self.assertEqual(rc, 0)
        # Must parse without exception
        try:
            json.loads(out)
        except json.JSONDecodeError as e:
            self.fail(f"dedup.py emitted invalid JSON: {e}\nOutput: {out!r}")

    def test_pointer_output_valid_json(self):
        payload = {
            "session_id": "json_pointer_test",
            "tool_name": "Read",
            "tool_response": _LARGE_TEXT,
        }
        # Store first
        self._run(payload)
        # Pointer second
        out, rc = self._run(payload)
        self.assertEqual(rc, 0)
        try:
            json.loads(out)
        except json.JSONDecodeError as e:
            self.fail(f"Pointer output is invalid JSON: {e}")


class TestDedupSavingsRecorded(unittest.TestCase):
    """Savings are recorded in the savings table for both action paths."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_savings_recorded_after_two_calls(self):
        """First call (new-content branch) and second call (pointer branch) each record savings."""
        import store as _store

        session_id = "sess_savings_test"
        # Use a payload large enough to guarantee dedup acts
        large_payload = "\n".join([f"savings line {i}: " + "x" * 60 for i in range(60)])
        payload = {
            "session_id": session_id,
            "tool_name": "Read",
            "tool_response": large_payload,
        }

        # First call: new-content branch (truncate savings recorded if shrunk < output)
        out1, rc1 = _run_dedup(payload, self._tmp)
        self.assertEqual(rc1, 0)

        # Second call: pointer branch (dedup savings recorded)
        out2, rc2 = _run_dedup(payload, self._tmp)
        self.assertEqual(rc2, 0)
        self.assertIn("hookSpecificOutput", out2)
        self.assertIn("[nexum] identical", out2["hookSpecificOutput"]["updatedToolOutput"])

        # Now read back savings using the same CLAUDE_PLUGIN_DATA
        import os as _os
        old_env = _os.environ.get("CLAUDE_PLUGIN_DATA")
        _os.environ["CLAUDE_PLUGIN_DATA"] = self._tmp
        try:
            total = _store.session_savings(session_id)
        finally:
            if old_env is None:
                _os.environ.pop("CLAUDE_PLUGIN_DATA", None)
            else:
                _os.environ["CLAUDE_PLUGIN_DATA"] = old_env

        self.assertGreater(total, 0, "session_savings should be > 0 after pointer collapse")


if __name__ == "__main__":
    unittest.main()
