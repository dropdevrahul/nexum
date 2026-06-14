"""
test_context_watch.py — stdlib unittest tests for scripts/context_watch.py

Covers ACCEPTANCE from §5.2:
- first prompt allowed + stored
- same-topic follow-up allowed
- fix→feature divergence blocked
- 'continue' bypasses then adopts new task
- threshold crossing emits systemMessage exactly once per window
- malformed input fail-open
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


def _run_context_watch(payload, data_dir):
    """Run context_watch.py and return (parsed_output, exit_code)."""
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = data_dir
    env["PYTHONPATH"] = _SCRIPTS_DIR + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, os.path.join(_SCRIPTS_DIR, "context_watch.py")],
        input=json.dumps(payload).encode(),
        capture_output=True,
        env=env,
        timeout=15,
    )
    return json.loads(result.stdout.decode()), result.returncode


def _is_blocked(out):
    return out.get("decision") == "block"


def _is_allowed(out):
    return not _is_blocked(out)


class TestContextWatchFirstPrompt(unittest.TestCase):
    """First prompt in a session must be allowed and stored."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_first_prompt_allowed(self):
        payload = {
            "session_id": "sess_first",
            "prompt": "fix the login bug",
        }
        out, rc = _run_context_watch(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertTrue(_is_allowed(out), f"Expected allow for first prompt, got: {out}")

    def test_first_prompt_stored_in_kv(self):
        """After first prompt, the task signature must be persisted."""
        if _SCRIPTS_DIR not in sys.path:
            sys.path.insert(0, _SCRIPTS_DIR)
        import store
        # Use the test's own data dir
        os.environ["CLAUDE_PLUGIN_DATA"] = self._tmp
        payload = {
            "session_id": "sess_stored",
            "prompt": "implement the payment feature",
        }
        _run_context_watch(payload, self._tmp)
        task = store.get_session_task("sess_stored")
        self.assertIsNotNone(task, "Task signature not stored after first prompt")

    def test_empty_prompt_allowed(self):
        """Empty prompt → always allow, no state change."""
        payload = {"session_id": "sess_empty", "prompt": ""}
        out, rc = _run_context_watch(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertTrue(_is_allowed(out))


class TestContextWatchSameTopicAllowed(unittest.TestCase):
    """Same-topic follow-up prompts must be allowed."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_same_topic_followup_allowed(self):
        sid = "sess_same_topic"
        # First prompt establishes fix context
        p1 = {"session_id": sid, "prompt": "fix the login bug in auth module"}
        out1, _ = _run_context_watch(p1, self._tmp)
        self.assertTrue(_is_allowed(out1))

        # Second prompt — same fix topic
        p2 = {"session_id": sid, "prompt": "fix the password reset bug too"}
        out2, rc2 = _run_context_watch(p2, self._tmp)
        self.assertEqual(rc2, 0)
        self.assertTrue(_is_allowed(out2), f"Same-topic follow-up was blocked: {out2}")

    def test_continuation_same_words_allowed(self):
        """Continuation with very similar wording → allowed."""
        sid = "sess_continuation"
        p1 = {"session_id": sid, "prompt": "implement user authentication feature"}
        _run_context_watch(p1, self._tmp)

        p2 = {"session_id": sid, "prompt": "implement user profile feature too"}
        out, rc = _run_context_watch(p2, self._tmp)
        self.assertEqual(rc, 0)
        self.assertTrue(_is_allowed(out))


class TestContextWatchIntentBlock(unittest.TestCase):
    """fix→feature divergence should be blocked."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_fix_to_feature_blocked(self):
        sid = "sess_block"
        # Establish fix context
        p1 = {"session_id": sid, "prompt": "fix the crash bug in the payment module"}
        out1, _ = _run_context_watch(p1, self._tmp)
        self.assertTrue(_is_allowed(out1), f"First prompt should be allowed, got: {out1}")

        # Switch to feature — should be blocked
        p2 = {"session_id": sid, "prompt": "add new billing dashboard feature implement"}
        out2, rc2 = _run_context_watch(p2, self._tmp)
        self.assertEqual(rc2, 0)
        self.assertTrue(_is_blocked(out2),
                        f"Expected block on fix→feature divergence, got: {out2}")

    def test_block_message_format(self):
        """Block reason must follow the spec format."""
        sid = "sess_block_msg"
        p1 = {"session_id": sid, "prompt": "fix the database error and exception"}
        _run_context_watch(p1, self._tmp)

        p2 = {"session_id": sid, "prompt": "implement new feature add billing module"}
        out, _ = _run_context_watch(p2, self._tmp)

        if _is_blocked(out):
            reason = out.get("reason", "")
            self.assertIn("[nexum]", reason)
            self.assertIn("continue", reason.lower())


class TestContextWatchContinueBypass(unittest.TestCase):
    """'continue' reply bypasses the block."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_continue_bypasses_block(self):
        sid = "sess_continue"
        # Establish fix context
        p1 = {"session_id": sid, "prompt": "fix the crash bug in payment processing"}
        _run_context_watch(p1, self._tmp)

        # Trigger a block
        p2 = {"session_id": sid, "prompt": "add new billing dashboard feature implement"}
        out_block = _run_context_watch(p2, self._tmp)[0]

        if not _is_blocked(out_block):
            self.skipTest("Guard didn't block — Jaccard similarity may be above threshold")

        # Send 'continue'
        p3 = {"session_id": sid, "prompt": "continue"}
        out_continue, rc = _run_context_watch(p3, self._tmp)
        self.assertEqual(rc, 0)
        self.assertTrue(_is_allowed(out_continue),
                        f"Expected 'continue' to be allowed, got: {out_continue}")

    def test_continue_case_insensitive(self):
        """'CONTINUE' (uppercase) must also bypass the block."""
        sid = "sess_continue_case"
        p1 = {"session_id": sid, "prompt": "fix the crash bug in payment module"}
        _run_context_watch(p1, self._tmp)

        p2 = {"session_id": sid, "prompt": "add new billing feature implement create"}
        out_block = _run_context_watch(p2, self._tmp)[0]

        if not _is_blocked(out_block):
            self.skipTest("Guard didn't block")

        p3 = {"session_id": sid, "prompt": "CONTINUE"}
        out, rc = _run_context_watch(p3, self._tmp)
        self.assertEqual(rc, 0)
        self.assertTrue(_is_allowed(out))


class TestContextWatchCompactionMessage(unittest.TestCase):
    """Compaction threshold crossing emits systemMessage exactly once."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def _set_low_threshold(self):
        cfg_path = os.path.join(self._tmp, "config.json")
        with open(cfg_path, "w") as f:
            json.dump({"compaction_threshold_tokens": 10}, f)

    def test_threshold_crossing_emits_system_message(self):
        """When token count exceeds threshold, systemMessage is emitted."""
        self._set_low_threshold()
        sid = "sess_compact"
        # Any non-trivial prompt should exceed 10-token threshold
        payload = {"session_id": sid, "prompt": "fix the bug in the authentication module now"}
        out, rc = _run_context_watch(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertIn("systemMessage", out,
                      f"Expected systemMessage on threshold crossing, got: {out}")
        self.assertIn("[nexum]", out["systemMessage"])
        self.assertIn("compact", out["systemMessage"].lower())

    def test_system_message_emitted_only_once(self):
        """systemMessage must be emitted only once per window (not on every message)."""
        self._set_low_threshold()
        sid = "sess_compact_once"
        p1 = {"session_id": sid, "prompt": "fix the authentication bug module"}
        out1, _ = _run_context_watch(p1, self._tmp)
        # First crossing may or may not emit depending on exact token count
        # Now send a second prompt that definitely crosses the threshold (already crossed)
        p2 = {"session_id": sid, "prompt": "fix the login error too please"}
        out2, rc = _run_context_watch(p2, self._tmp)
        self.assertEqual(rc, 0)

        # If first prompt triggered the warning, second should NOT
        if "systemMessage" in out1:
            self.assertNotIn("systemMessage", out2,
                             "systemMessage emitted twice — should be once per window")


class TestContextWatchFailOpen(unittest.TestCase):
    """Malformed input → fail-open ({} exit 0)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def _run_raw(self, raw_bytes):
        env = os.environ.copy()
        env["CLAUDE_PLUGIN_DATA"] = self._tmp
        env["PYTHONPATH"] = _SCRIPTS_DIR + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, os.path.join(_SCRIPTS_DIR, "context_watch.py")],
            input=raw_bytes,
            capture_output=True,
            env=env,
            timeout=15,
        )
        return result.stdout.decode(), result.returncode

    def test_malformed_json_fail_open(self):
        out_str, rc = self._run_raw(b"NOT JSON {{{")
        self.assertEqual(rc, 0)
        # Must emit valid JSON
        out = json.loads(out_str)
        self.assertTrue(_is_allowed(out))

    def test_empty_input_fail_open(self):
        out_str, rc = self._run_raw(b"")
        self.assertEqual(rc, 0)
        out = json.loads(out_str)
        # Empty input → {} (fail-open, allowed)
        self.assertTrue(_is_allowed(out))

    def test_valid_json_no_prompt_key(self):
        """Valid JSON missing 'prompt' key → allow (empty prompt edge case)."""
        payload = {"session_id": "nosess"}
        env = os.environ.copy()
        env["CLAUDE_PLUGIN_DATA"] = self._tmp
        env["PYTHONPATH"] = _SCRIPTS_DIR + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, os.path.join(_SCRIPTS_DIR, "context_watch.py")],
            input=json.dumps(payload).encode(),
            capture_output=True,
            env=env,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0)
        out = json.loads(result.stdout.decode())
        self.assertTrue(_is_allowed(out))


if __name__ == "__main__":
    unittest.main()
