"""
test_resume_nudge.py — stdlib unittest tests for scripts/resume_nudge.py

Covers:
  (a) fresh ts + matching branch → output contains "/nx-load"
  (b) timestamp older than 24h → {}
  (c) handoff branch != current branch → {}
  (d) resume_nudge_enabled=false → {}
  (e) source="compact" → {}
  (f) malformed stdin → {} exit 0
"""

import datetime
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

_RESUME_NUDGE = os.path.join(_SCRIPTS_DIR, "resume_nudge.py")


def _init_git_repo(path: str) -> str:
    """Init a git repo in path and return the current branch name."""
    subprocess.run(["git", "init", path], capture_output=True)
    subprocess.run(["git", "-C", path, "config", "user.email", "test@test.com"], capture_output=True)
    subprocess.run(["git", "-C", path, "config", "user.name", "Test"], capture_output=True)
    # Create an initial commit so HEAD is valid
    dummy = os.path.join(path, "README.md")
    with open(dummy, "w") as fh:
        fh.write("test\n")
    subprocess.run(["git", "-C", path, "add", "README.md"], capture_output=True)
    subprocess.run(["git", "-C", path, "commit", "-m", "init"], capture_output=True)
    result = subprocess.run(
        ["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True,
    )
    return result.stdout.strip()


def _write_handoff(data_dir: str, branch: str, ts: str) -> None:
    """Write a handoff/latest.md with the given branch and ISO timestamp."""
    handoff_dir = os.path.join(data_dir, "handoff")
    os.makedirs(handoff_dir, exist_ok=True)
    content = (
        f"# Handoff (auto-skeleton): {branch}\n\n"
        f"**Session:** testsession   **Branch:** {branch}   **Written:** {ts}\n"
        f"**Why now:** context crossed the auto-handoff threshold.\n"
    )
    with open(os.path.join(handoff_dir, "latest.md"), "w") as fh:
        fh.write(content)


def _run_nudge(payload: dict, data_dir: str, cwd: str, extra_config: dict = None):
    """Run resume_nudge.py and return (parsed_output, exit_code)."""
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = data_dir
    env["PYTHONPATH"] = _SCRIPTS_DIR + os.pathsep + env.get("PYTHONPATH", "")

    if extra_config:
        cfg_path = os.path.join(data_dir, "config.json")
        with open(cfg_path, "w") as fh:
            json.dump(extra_config, fh)

    result = subprocess.run(
        [sys.executable, _RESUME_NUDGE],
        input=json.dumps(payload).encode(),
        capture_output=True,
        env=env,
        cwd=cwd,
        timeout=15,
    )
    return json.loads(result.stdout.decode()), result.returncode


def _run_nudge_raw(raw_bytes: bytes, data_dir: str, cwd: str):
    """Run resume_nudge.py with raw bytes."""
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = data_dir
    env["PYTHONPATH"] = _SCRIPTS_DIR + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, _RESUME_NUDGE],
        input=raw_bytes,
        capture_output=True,
        env=env,
        cwd=cwd,
        timeout=15,
    )
    return result.stdout.decode().strip(), result.returncode


def _now_iso() -> str:
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


def _old_iso(hours: float = 25) -> str:
    dt = datetime.datetime.now().astimezone() - datetime.timedelta(hours=hours)
    return dt.isoformat(timespec="seconds")


class TestResumeNudgeFreshMatchingBranch(unittest.TestCase):
    """Fresh timestamp + matching branch → output contains /nx-load."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._git_dir = tempfile.mkdtemp()
        self._branch = _init_git_repo(self._git_dir)

    def test_fresh_matching_branch_nudges(self):
        ts = _now_iso()
        _write_handoff(self._tmp, self._branch, ts)

        payload = {"cwd": self._git_dir}
        out, rc = _run_nudge(payload, self._tmp, self._git_dir)
        self.assertEqual(rc, 0)
        self.assertNotEqual(out, {}, f"Expected hint, got: {out}")
        # Check for /nx-load in system message or hook output
        text = json.dumps(out)
        self.assertIn("/nx-load", text, f"Expected /nx-load in output: {out}")


class TestResumeNudgeOldTimestamp(unittest.TestCase):
    """Timestamp older than 24h → {}."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._git_dir = tempfile.mkdtemp()
        self._branch = _init_git_repo(self._git_dir)

    def test_old_timestamp_suppressed(self):
        ts = _old_iso(hours=25)
        _write_handoff(self._tmp, self._branch, ts)

        payload = {"cwd": self._git_dir}
        out, rc = _run_nudge(payload, self._tmp, self._git_dir)
        self.assertEqual(rc, 0)
        self.assertEqual(out, {}, f"Expected {{}} for old timestamp, got: {out}")


class TestResumeNudgeBranchMismatch(unittest.TestCase):
    """Handoff branch != current branch → {}."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._git_dir = tempfile.mkdtemp()
        _init_git_repo(self._git_dir)

    def test_branch_mismatch_suppressed(self):
        ts = _now_iso()
        # Write handoff for a different branch
        _write_handoff(self._tmp, "some-other-branch-xyz", ts)

        payload = {"cwd": self._git_dir}
        out, rc = _run_nudge(payload, self._tmp, self._git_dir)
        self.assertEqual(rc, 0)
        self.assertEqual(out, {}, f"Expected {{}} for branch mismatch, got: {out}")


class TestResumeNudgeDisabled(unittest.TestCase):
    """resume_nudge_enabled=false → {}."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._git_dir = tempfile.mkdtemp()
        self._branch = _init_git_repo(self._git_dir)

    def test_disabled_suppresses(self):
        ts = _now_iso()
        _write_handoff(self._tmp, self._branch, ts)

        payload = {"cwd": self._git_dir}
        out, rc = _run_nudge(payload, self._tmp, self._git_dir,
                             extra_config={"resume_nudge_enabled": False})
        self.assertEqual(rc, 0)
        self.assertEqual(out, {}, f"Expected {{}} when disabled, got: {out}")


class TestResumeNudgeSourceCompact(unittest.TestCase):
    """source='compact' → {}."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._git_dir = tempfile.mkdtemp()
        self._branch = _init_git_repo(self._git_dir)

    def test_compact_source_suppressed(self):
        ts = _now_iso()
        _write_handoff(self._tmp, self._branch, ts)

        payload = {"cwd": self._git_dir, "source": "compact"}
        out, rc = _run_nudge(payload, self._tmp, self._git_dir)
        self.assertEqual(rc, 0)
        self.assertEqual(out, {}, f"Expected {{}} for source=compact, got: {out}")

    def test_resume_source_suppressed(self):
        ts = _now_iso()
        _write_handoff(self._tmp, self._branch, ts)

        payload = {"cwd": self._git_dir, "source": "resume"}
        out, rc = _run_nudge(payload, self._tmp, self._git_dir)
        self.assertEqual(rc, 0)
        self.assertEqual(out, {}, f"Expected {{}} for source=resume, got: {out}")


class TestResumeNudgeMalformedStdin(unittest.TestCase):
    """Malformed stdin → {} exit 0."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._git_dir = tempfile.mkdtemp()
        _init_git_repo(self._git_dir)

    def test_malformed_json(self):
        out_str, rc = _run_nudge_raw(b"NOT JSON {{{", self._tmp, self._git_dir)
        self.assertEqual(rc, 0)
        self.assertEqual(out_str, "{}")

    def test_empty_stdin(self):
        out_str, rc = _run_nudge_raw(b"", self._tmp, self._git_dir)
        self.assertEqual(rc, 0)
        self.assertEqual(out_str, "{}")

    def test_non_dict_json(self):
        out_str, rc = _run_nudge_raw(b"[1, 2, 3]", self._tmp, self._git_dir)
        self.assertEqual(rc, 0)
        self.assertEqual(out_str, "{}")


if __name__ == "__main__":
    unittest.main()
