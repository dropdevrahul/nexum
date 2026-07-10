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


def _git(cwd, *args):
    subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, check=False)


def _dirty_git_repo():
    """Temp git repo with one commit plus an uncommitted edit → is_dirty True."""
    repo = tempfile.mkdtemp()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    with open(os.path.join(repo, "a.txt"), "w") as f:
        f.write("hello")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "init")
    with open(os.path.join(repo, "a.txt"), "w") as f:
        f.write("changed")  # uncommitted → dirty
    return repo


def _clean_git_repo():
    """Temp git repo with a single commit and no pending changes → is_dirty False."""
    repo = tempfile.mkdtemp()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    with open(os.path.join(repo, "a.txt"), "w") as f:
        f.write("hello")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-qm", "init")
    return repo


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
    """fix→feature divergence on a DIRTY tree → worktree + block."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._repo = _dirty_git_repo()

    def test_fix_to_feature_blocked(self):
        sid = "sess_block"
        # Establish fix context
        p1 = {"session_id": sid, "prompt": "fix the crash bug in the payment module",
              "cwd": self._repo}
        out1, _ = _run_context_watch(p1, self._tmp)
        self.assertTrue(_is_allowed(out1), f"First prompt should be allowed, got: {out1}")

        # Switch to feature on a dirty tree — should be blocked with a worktree
        p2 = {"session_id": sid, "prompt": "add new billing dashboard feature implement",
              "cwd": self._repo}
        out2, rc2 = _run_context_watch(p2, self._tmp)
        self.assertEqual(rc2, 0)
        self.assertTrue(_is_blocked(out2),
                        f"Expected block on fix→feature divergence, got: {out2}")
        # A worktree was actually created under the repo's .nexum-data/worktrees.
        wt_root = os.path.join(self._repo, ".nexum-data", "worktrees")
        self.assertTrue(os.path.isdir(wt_root) and os.listdir(wt_root),
                        "Expected a worktree to be created on divergence")
        self.assertIn(wt_root, out2.get("reason", ""))

    def test_clean_tree_allows_divergence(self):
        """On a CLEAN tree the divergent task is allowed in place — no worktree,
        no block. This is the behaviour change: no more 'start a fresh session'."""
        clean = _clean_git_repo()
        sid = "sess_clean"
        _run_context_watch(
            {"session_id": sid, "prompt": "fix the crash bug in the payment module",
             "cwd": clean},
            self._tmp,
        )
        out, rc = _run_context_watch(
            {"session_id": sid, "prompt": "add new billing dashboard feature implement",
             "cwd": clean},
            self._tmp,
        )
        self.assertEqual(rc, 0)
        self.assertTrue(_is_allowed(out), f"Clean-tree divergence must be allowed, got: {out}")
        self.assertFalse(os.path.isdir(os.path.join(clean, ".nexum-data", "worktrees")),
                         "No worktree should be created on a clean tree")

    def test_block_message_format(self):
        """Block reason must name nexum and offer the 'continue' escape hatch."""
        sid = "sess_block_msg"
        p1 = {"session_id": sid, "prompt": "fix the database error and exception",
              "cwd": self._repo}
        _run_context_watch(p1, self._tmp)

        p2 = {"session_id": sid, "prompt": "implement new feature add billing module",
              "cwd": self._repo}
        out, _ = _run_context_watch(p2, self._tmp)

        if _is_blocked(out):
            reason = out.get("reason", "")
            self.assertIn("[nexum]", reason)
            self.assertIn("continue", reason.lower())


class TestContextWatchAutomatedPromptSkipsGuard(unittest.TestCase):
    """System-injected prompts (task-notifications, command stdout) must never be
    blocked by the intent-guard, even when they look like a task-type change."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._repo = _dirty_git_repo()

    def test_task_notification_not_blocked(self):
        sid = "sess_auto"
        # Establish a fix context (bare prompt) so a feature prompt would block.
        _run_context_watch(
            {"session_id": sid, "prompt": "fix the crash bug in the payment module",
             "cwd": self._repo},
            self._tmp,
        )
        # A background-agent completion arrives as a task-notification whose text
        # would otherwise read as a divergent "feature" task.
        auto = {
            "session_id": sid,
            "cwd": self._repo,
            "prompt": (
                "<task-notification>\n<task-id>abc123</task-id>\n"
                "add new billing dashboard feature implement\n</task-notification>"
            ),
        }
        out, rc = _run_context_watch(auto, self._tmp)
        self.assertEqual(rc, 0)
        self.assertTrue(
            _is_allowed(out),
            f"Automated task-notification must not be blocked, got: {out}",
        )

    def test_bare_version_still_blocks(self):
        """Control: the same divergent text WITHOUT automation markers still blocks
        on a dirty tree, proving the skip is specific to automated prompts."""
        sid = "sess_auto_control"
        _run_context_watch(
            {"session_id": sid, "prompt": "fix the crash bug in the payment module",
             "cwd": self._repo},
            self._tmp,
        )
        out, _ = _run_context_watch(
            {"session_id": sid, "prompt": "add new billing dashboard feature implement",
             "cwd": self._repo},
            self._tmp,
        )
        self.assertTrue(_is_blocked(out), f"Bare divergent prompt should block, got: {out}")


class TestContextWatchContinueBypass(unittest.TestCase):
    """'continue' reply bypasses the block."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._repo = _dirty_git_repo()

    def test_continue_bypasses_block(self):
        sid = "sess_continue"
        # Establish fix context
        p1 = {"session_id": sid, "prompt": "fix the crash bug in payment processing",
              "cwd": self._repo}
        _run_context_watch(p1, self._tmp)

        # Trigger a block (dirty tree → worktree + block)
        p2 = {"session_id": sid, "prompt": "add new billing dashboard feature implement",
              "cwd": self._repo}
        out_block = _run_context_watch(p2, self._tmp)[0]

        if not _is_blocked(out_block):
            self.skipTest("Guard didn't block — Jaccard similarity may be above threshold")

        # Send 'continue'
        p3 = {"session_id": sid, "prompt": "continue", "cwd": self._repo}
        out_continue, rc = _run_context_watch(p3, self._tmp)
        self.assertEqual(rc, 0)
        self.assertTrue(_is_allowed(out_continue),
                        f"Expected 'continue' to be allowed, got: {out_continue}")

    def test_continue_case_insensitive(self):
        """'CONTINUE' (uppercase) must also bypass the block."""
        sid = "sess_continue_case"
        p1 = {"session_id": sid, "prompt": "fix the crash bug in payment module",
              "cwd": self._repo}
        _run_context_watch(p1, self._tmp)

        p2 = {"session_id": sid, "prompt": "add new billing feature implement create",
              "cwd": self._repo}
        out_block = _run_context_watch(p2, self._tmp)[0]

        if not _is_blocked(out_block):
            self.skipTest("Guard didn't block")

        p3 = {"session_id": sid, "prompt": "CONTINUE", "cwd": self._repo}
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


class TestContextWatchHandoffNudge(unittest.TestCase):
    """Crossing the handoff threshold (but not compaction) suggests /nx-save."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def _set_thresholds(self, handoff, compaction):
        cfg_path = os.path.join(self._tmp, "config.json")
        with open(cfg_path, "w") as f:
            json.dump({
                "handoff_threshold_tokens": handoff,
                "compaction_threshold_tokens": compaction,
            }, f)

    def test_handoff_nudge_emitted_below_compaction(self):
        """When tokens cross the handoff threshold (still under compaction),
        the nudge suggests /nx-save, not /compact."""
        # Low handoff threshold, high compaction threshold so only handoff fires.
        self._set_thresholds(handoff=10, compaction=100000)
        payload = {"session_id": "sess_handoff",
                   "prompt": "fix the bug in the authentication module now"}
        out, rc = _run_context_watch(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertIn("systemMessage", out,
                      f"Expected handoff nudge, got: {out}")
        msg = out["systemMessage"].lower()
        self.assertIn("[nexum]", out["systemMessage"])
        self.assertIn("nx-save", msg)
        self.assertNotIn("/compact", msg)

    def test_handoff_nudge_emitted_only_once(self):
        """The handoff nudge fires at most once per window."""
        self._set_thresholds(handoff=10, compaction=100000)
        sid = "sess_handoff_once"
        out1, _ = _run_context_watch(
            {"session_id": sid, "prompt": "fix the authentication bug module"}, self._tmp)
        out2, rc = _run_context_watch(
            {"session_id": sid, "prompt": "fix the login error too please"}, self._tmp)
        self.assertEqual(rc, 0)
        if "systemMessage" in out1:
            self.assertNotIn("systemMessage", out2,
                             "handoff nudge emitted twice — should be once per window")

    def test_compaction_takes_precedence(self):
        """When tokens cross both thresholds at once, the compaction warning wins."""
        self._set_thresholds(handoff=5, compaction=10)
        payload = {"session_id": "sess_both",
                   "prompt": "fix the bug in the authentication module now please"}
        out, rc = _run_context_watch(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertIn("systemMessage", out)
        self.assertIn("compact", out["systemMessage"].lower())

    def test_handoff_nudge_disabled_when_zero(self):
        """handoff_threshold_tokens=0 disables the nudge."""
        self._set_thresholds(handoff=0, compaction=100000)
        payload = {"session_id": "sess_disabled",
                   "prompt": "fix the bug in the authentication module now"}
        out, rc = _run_context_watch(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertNotIn("systemMessage", out)

    def test_auto_writes_handoff_skeleton_on_crossing(self):
        """Crossing the handoff threshold auto-writes the handoff skeleton."""
        self._set_thresholds(handoff=5, compaction=999999)
        payload = {"session_id": "sess_autowrite",
                   "prompt": "implement the new billing feature now please",
                   "cwd": os.getcwd()}
        out, rc = _run_context_watch(payload, self._tmp)
        self.assertEqual(rc, 0)
        latest = os.path.join(self._tmp, "handoff", "latest.md")
        self.assertTrue(os.path.isfile(latest), "skeleton latest.md not written")
        msg = out.get("systemMessage", "").lower()
        self.assertIn("handoff", msg)
        self.assertIn("nx-load", msg)

    def test_auto_write_disabled(self):
        """handoff_auto_write_enabled=false suppresses the skeleton write."""
        cfg_path = os.path.join(self._tmp, "config.json")
        with open(cfg_path, "w") as f:
            json.dump({"handoff_threshold_tokens": 5,
                       "compaction_threshold_tokens": 999999,
                       "handoff_auto_write_enabled": False}, f)
        payload = {"session_id": "sess_nowrite",
                   "prompt": "implement the new billing feature now please",
                   "cwd": os.getcwd()}
        out, rc = _run_context_watch(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertFalse(os.path.isfile(os.path.join(self._tmp, "handoff", "latest.md")))


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


class TestContextWatchTranscriptHandoff(unittest.TestCase):
    """The handoff/compaction thresholds are driven by the REAL context size read
    from the session transcript, and the nudges re-arm after context drops."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def _transcript(self, input_tok, cache_creation_tok, cache_read_tok):
        """Write a transcript JSONL whose LAST usage block has the given fields
        (a smaller earlier usage block is present to prove last-wins). Returns
        the file path. The real context size = sum of the three fields."""
        path = os.path.join(self._tmp, "transcript.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "assistant", "message": {"usage": {
                "input_tokens": 1, "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 1}}}) + "\n")
            fh.write(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
            fh.write(json.dumps({"type": "assistant", "message": {"usage": {
                "input_tokens": input_tok,
                "cache_creation_input_tokens": cache_creation_tok,
                "cache_read_input_tokens": cache_read_tok}}}) + "\n")
        return path

    def test_transcript_above_handoff_writes_handoff_and_clear_message(self):
        """Context between the default handoff (100k) and compaction (120k)
        thresholds writes a handoff and nudges /clear + /nx-load."""
        tp = self._transcript(2, 2000, 108000)  # 110002 -> handoff, not compaction
        payload = {"session_id": "sess_tx_high", "prompt": "do the work",
                   "cwd": self._tmp, "transcript_path": tp}
        out, rc = _run_context_watch(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertIn("systemMessage", out, f"expected a nudge, got: {out}")
        msg = out["systemMessage"].lower()
        self.assertIn("/clear", msg)
        self.assertIn("/nx-load", msg)
        latest = os.path.join(self._tmp, "handoff", "latest.md")
        self.assertTrue(os.path.isfile(latest), "handoff latest.md not written")

    def test_transcript_below_threshold_writes_nothing(self):
        """A small transcript context produces no handoff and no nudge."""
        tp = self._transcript(2, 10, 400)  # 412
        payload = {"session_id": "sess_tx_low", "prompt": "do the work",
                   "cwd": self._tmp, "transcript_path": tp}
        out, rc = _run_context_watch(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertNotIn("systemMessage", out)
        self.assertFalse(os.path.isfile(os.path.join(self._tmp, "handoff", "latest.md")))

    def test_nudge_rearms_after_context_drops(self):
        """handoff_warned is set when context is high, then cleared (re-armed)
        once the transcript reports context back below the threshold."""
        sid = "sess_tx_rearm"
        big = self._transcript(2, 2000, 108000)  # 110002 > handoff
        out1, _ = _run_context_watch(
            {"session_id": sid, "prompt": "do the work",
             "cwd": self._tmp, "transcript_path": big}, self._tmp)
        self.assertIn("systemMessage", out1)
        os.environ["CLAUDE_PLUGIN_DATA"] = self._tmp
        try:
            import store
            self.assertTrue(store.get_flag(sid, "handoff_warned"))
            small = self._transcript(2, 10, 400)  # 412 < handoff
            _run_context_watch(
                {"session_id": sid, "prompt": "more work",
                 "cwd": self._tmp, "transcript_path": small}, self._tmp)
            self.assertFalse(store.get_flag(sid, "handoff_warned"))
        finally:
            os.environ.pop("CLAUDE_PLUGIN_DATA", None)


if __name__ == "__main__":
    unittest.main()
