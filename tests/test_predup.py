"""
test_predup.py — stdlib unittest tests for scripts/predup.py

Covers:
  (a) no prior call → allow {}
  (b) seeded prior + identical Read input → deny with savings recorded
  (c) predup_enabled=false → allow {}
  (d) Read with changed mtime vs seeded prior → allow {}
  (e) malformed stdin → allow {} exit 0
  (f) Bash with predup_bash_readonly default False → allow {}
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


def _run_predup(payload, data_dir, extra_config=None):
    """Run predup.py subprocess and return (parsed_output, exit_code)."""
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = data_dir
    env["PYTHONPATH"] = _SCRIPTS_DIR + os.pathsep + env.get("PYTHONPATH", "")

    if extra_config:
        cfg_path = os.path.join(data_dir, "config.json")
        with open(cfg_path, "w") as fh:
            json.dump(extra_config, fh)

    result = subprocess.run(
        [sys.executable, os.path.join(_SCRIPTS_DIR, "predup.py")],
        input=json.dumps(payload).encode(),
        capture_output=True,
        env=env,
        timeout=15,
    )
    return json.loads(result.stdout.decode()), result.returncode


def _run_predup_raw(raw_bytes, data_dir):
    """Run predup.py with raw bytes on stdin."""
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = data_dir
    env["PYTHONPATH"] = _SCRIPTS_DIR + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, os.path.join(_SCRIPTS_DIR, "predup.py")],
        input=raw_bytes,
        capture_output=True,
        env=env,
        timeout=15,
    )
    return result.stdout.decode().strip(), result.returncode


def _seed_tool_call(data_dir, session_id, tool_name, tool_input, token_count=500, mtime=None):
    """Seed a tool_call row in the store for the given data_dir."""
    old_val = os.environ.get("CLAUDE_PLUGIN_DATA")
    os.environ["CLAUDE_PLUGIN_DATA"] = data_dir
    try:
        import importlib
        import store as _store
        importlib.reload(_store)
        sig = _store.tool_call_sig(tool_name, tool_input)
        _store.record_tool_call(session_id, sig, tool_name, token_count, mtime=mtime)
        return sig
    finally:
        if old_val is None:
            os.environ.pop("CLAUDE_PLUGIN_DATA", None)
        else:
            os.environ["CLAUDE_PLUGIN_DATA"] = old_val


def _get_session_savings(data_dir, session_id):
    """Read session_savings from the store for the given data_dir."""
    old_val = os.environ.get("CLAUDE_PLUGIN_DATA")
    os.environ["CLAUDE_PLUGIN_DATA"] = data_dir
    try:
        import importlib
        import store as _store
        importlib.reload(_store)
        return _store.session_savings(session_id)
    finally:
        if old_val is None:
            os.environ.pop("CLAUDE_PLUGIN_DATA", None)
        else:
            os.environ["CLAUDE_PLUGIN_DATA"] = old_val


class TestPredupNoPrior(unittest.TestCase):
    """No prior tool call → predup emits {} (allow)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_no_prior_allows(self):
        payload = {
            "session_id": "s_new",
            "tool_name": "Read",
            "tool_input": {"file_path": "/some/file.py"},
        }
        out, rc = _run_predup(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertEqual(out, {}, f"Expected {{}} for no-prior call, got: {out}")


class TestPredupDenyOnRepeat(unittest.TestCase):
    """Seeded prior + identical Read input → deny with savings recorded."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_repeat_read_is_denied(self):
        session_id = "s_repeat"
        tool_input = {"file_path": "/tmp/some_file.py"}

        # Seed the prior call in the SAME db
        _seed_tool_call(self._tmp, session_id, "Read", tool_input, token_count=400)

        # Now run the hook — it should deny
        payload = {
            "session_id": session_id,
            "tool_name": "Read",
            "tool_input": tool_input,
        }
        out, rc = _run_predup(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertIn("hookSpecificOutput", out, f"Expected deny output, got: {out}")
        decision = out["hookSpecificOutput"].get("permissionDecision")
        self.assertEqual(decision, "deny", f"Expected deny, got: {decision}")

    def test_repeat_savings_recorded(self):
        session_id = "s_savings"
        tool_input = {"file_path": "/tmp/savings_file.py"}

        # Seed prior
        _seed_tool_call(self._tmp, session_id, "Read", tool_input, token_count=600)

        payload = {
            "session_id": session_id,
            "tool_name": "Read",
            "tool_input": tool_input,
        }
        _run_predup(payload, self._tmp)

        savings = _get_session_savings(self._tmp, session_id)
        self.assertGreater(savings, 0, "Expected savings > 0 after predup deny")

    def test_repeat_grep_is_denied(self):
        session_id = "s_grep"
        tool_input = {"pattern": "TODO", "path": "src/"}

        _seed_tool_call(self._tmp, session_id, "Grep", tool_input, token_count=300)

        payload = {
            "session_id": session_id,
            "tool_name": "Grep",
            "tool_input": tool_input,
        }
        out, rc = _run_predup(payload, self._tmp)
        self.assertEqual(rc, 0)
        decision = out.get("hookSpecificOutput", {}).get("permissionDecision")
        self.assertEqual(decision, "deny")


class TestPredupDisabled(unittest.TestCase):
    """predup_enabled=false → always allow {}."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_disabled_allows_repeat(self):
        session_id = "s_disabled"
        tool_input = {"file_path": "/tmp/disabled_test.py"}

        # Seed prior
        _seed_tool_call(self._tmp, session_id, "Read", tool_input, token_count=500)

        payload = {
            "session_id": session_id,
            "tool_name": "Read",
            "tool_input": tool_input,
        }
        out, rc = _run_predup(payload, self._tmp, extra_config={"predup_enabled": False})
        self.assertEqual(rc, 0)
        self.assertEqual(out, {}, f"Expected {{}} when disabled, got: {out}")


class TestPredupMtimeGuard(unittest.TestCase):
    """Read with changed mtime → allow {} (file changed since cached)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_changed_mtime_allows(self):
        session_id = "s_mtime"

        # Create a real temp file so we can get its actual mtime
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".py")
        f.write(b"content\n")
        f.close()
        fp = f.name

        tool_input = {"file_path": fp}

        try:
            # Seed with a stale mtime (different from current)
            stale_mtime = os.path.getmtime(fp) - 100.0
            _seed_tool_call(self._tmp, session_id, "Read", tool_input,
                            token_count=500, mtime=stale_mtime)

            payload = {
                "session_id": session_id,
                "tool_name": "Read",
                "tool_input": tool_input,
            }
            out, rc = _run_predup(payload, self._tmp)
            self.assertEqual(rc, 0)
            self.assertEqual(out, {}, f"Expected {{}} when mtime changed, got: {out}")
        finally:
            os.unlink(fp)

    def test_same_mtime_denies(self):
        session_id = "s_mtime_same"

        f = tempfile.NamedTemporaryFile(delete=False, suffix=".py")
        f.write(b"content\n")
        f.close()
        fp = f.name

        tool_input = {"file_path": fp}

        try:
            # Seed with the EXACT current mtime
            cur_mtime = os.path.getmtime(fp)
            _seed_tool_call(self._tmp, session_id, "Read", tool_input,
                            token_count=500, mtime=cur_mtime)

            payload = {
                "session_id": session_id,
                "tool_name": "Read",
                "tool_input": tool_input,
            }
            out, rc = _run_predup(payload, self._tmp)
            self.assertEqual(rc, 0)
            decision = out.get("hookSpecificOutput", {}).get("permissionDecision")
            self.assertEqual(decision, "deny", f"Expected deny for same mtime, got: {out}")
        finally:
            os.unlink(fp)


class TestPredupMalformedStdin(unittest.TestCase):
    """Malformed stdin → {} exit 0."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_malformed_json(self):
        out_str, rc = _run_predup_raw(b"NOT JSON {{{", self._tmp)
        self.assertEqual(rc, 0)
        self.assertEqual(out_str, "{}")

    def test_empty_stdin(self):
        out_str, rc = _run_predup_raw(b"", self._tmp)
        self.assertEqual(rc, 0)
        self.assertEqual(out_str, "{}")

    def test_non_dict_json(self):
        out_str, rc = _run_predup_raw(b"[1, 2, 3]", self._tmp)
        self.assertEqual(rc, 0)
        self.assertEqual(out_str, "{}")


class TestPredupBashDefault(unittest.TestCase):
    """Bash with predup_bash_readonly=False (default) → always allow {}."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_bash_not_eligible_by_default(self):
        session_id = "s_bash"
        tool_input = {"command": "cat /tmp/foo.py"}

        # Seed a prior Bash call
        _seed_tool_call(self._tmp, session_id, "Bash", tool_input, token_count=300)

        payload = {
            "session_id": session_id,
            "tool_name": "Bash",
            "tool_input": tool_input,
        }
        out, rc = _run_predup(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertEqual(out, {}, f"Expected {{}} for Bash (readonly=False), got: {out}")

    def test_bash_readonly_enabled_cat_denies(self):
        """With predup_bash_readonly=True, cat is eligible and repeat is denied."""
        session_id = "s_bash_ro"
        tool_input = {"command": "cat /tmp/foo.py"}

        _seed_tool_call(self._tmp, session_id, "Bash", tool_input, token_count=200)

        payload = {
            "session_id": session_id,
            "tool_name": "Bash",
            "tool_input": tool_input,
        }
        out, rc = _run_predup(payload, self._tmp, extra_config={"predup_bash_readonly": True})
        self.assertEqual(rc, 0)
        decision = out.get("hookSpecificOutput", {}).get("permissionDecision")
        self.assertEqual(decision, "deny", f"Expected deny for cat with readonly=True, got: {out}")


def _age_tool_call(data_dir, session_id, sig, age_seconds):
    """Backdate a seeded tool_call row's ts by age_seconds."""
    import time as _time
    old_val = os.environ.get("CLAUDE_PLUGIN_DATA")
    os.environ["CLAUDE_PLUGIN_DATA"] = data_dir
    try:
        import importlib
        import store as _store
        importlib.reload(_store)
        conn = _store.db()
        with conn:
            conn.execute(
                "UPDATE tool_calls SET ts=? WHERE session_id=? AND input_sig=?",
                (_time.time() - age_seconds, session_id, sig),
            )
        conn.close()
    finally:
        if old_val is None:
            os.environ.pop("CLAUDE_PLUGIN_DATA", None)
        else:
            os.environ["CLAUDE_PLUGIN_DATA"] = old_val


class TestPredupFreshnessGuard(unittest.TestCase):
    """A stale prior row (older than predup_max_age_seconds) → allow {}."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_stale_row_allows(self):
        payload = {
            "session_id": "s_age",
            "tool_name": "Read",
            "tool_input": {"file_path": "/some/file.py"},
        }
        sig = _seed_tool_call(self._tmp, "s_age", "Read",
                              {"file_path": "/some/file.py"})
        # Backdate well beyond the default 900s window.
        _age_tool_call(self._tmp, "s_age", sig, age_seconds=5000)
        out, rc = _run_predup(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertEqual(out, {}, f"Expected {{}} for stale row, got: {out}")

    def test_fresh_row_still_denies(self):
        """Control: a recent row within the window is still deduped."""
        payload = {
            "session_id": "s_fresh",
            "tool_name": "Read",
            "tool_input": {"file_path": "/some/file.py"},
        }
        _seed_tool_call(self._tmp, "s_fresh", "Read",
                        {"file_path": "/some/file.py"})
        out, rc = _run_predup(payload, self._tmp)
        self.assertEqual(rc, 0)
        decision = out.get("hookSpecificOutput", {}).get("permissionDecision")
        self.assertEqual(decision, "deny", f"Expected deny for fresh row, got: {out}")

    def test_age_check_disabled_denies_stale(self):
        """predup_max_age_seconds=0 reverts to ever-recorded behaviour."""
        payload = {
            "session_id": "s_off",
            "tool_name": "Read",
            "tool_input": {"file_path": "/some/file.py"},
        }
        sig = _seed_tool_call(self._tmp, "s_off", "Read",
                              {"file_path": "/some/file.py"})
        _age_tool_call(self._tmp, "s_off", sig, age_seconds=5000)
        out, rc = _run_predup(payload, self._tmp,
                              extra_config={"predup_max_age_seconds": 0})
        self.assertEqual(rc, 0)
        decision = out.get("hookSpecificOutput", {}).get("permissionDecision")
        self.assertEqual(decision, "deny", f"Expected deny when age check off, got: {out}")


if __name__ == "__main__":
    unittest.main()
