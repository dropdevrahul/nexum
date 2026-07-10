"""test_dispatch.py — end-to-end test for scripts/dispatch.py using a fake harness.

Drives dispatch.py against tests/fixtures/fake_harness.py (injected via
NEXUM_HARNESS_CMD_CLAUDE) in a throwaway git repo, so no real agent CLI is
needed. Asserts the verdict passes, a worktree was created, and the agents
registry row landed as done.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS_DIR = os.path.join(_ROOT, "scripts")
_FAKE = os.path.join(_ROOT, "tests", "fixtures", "fake_harness.py")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


def _git(cwd, *args):
    subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, check=False)


def _repo():
    d = tempfile.mkdtemp()
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    with open(os.path.join(d, "seed.txt"), "w") as f:
        f.write("seed")
    _git(d, "add", "seed.txt")
    _git(d, "commit", "-qm", "init")
    return d


def _run_dispatch(repo, data_dir, step, slug="demo", agent_id="agent_test"):
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = data_dir
    env["PYTHONPATH"] = _SCRIPTS_DIR + os.pathsep + env.get("PYTHONPATH", "")
    env["NEXUM_HARNESS_CMD_CLAUDE"] = f"{sys.executable} {_FAKE}"
    step_file = os.path.join(data_dir, "step.json")
    with open(step_file, "w") as f:
        json.dump(step, f)
    r = subprocess.run(
        [sys.executable, os.path.join(_SCRIPTS_DIR, "dispatch.py"),
         "--harness", "claude", "--model", "sonnet", "--repo", repo,
         "--new-worktree", "--slug", slug, "--step-file", step_file,
         "--agent-id", agent_id],
        capture_output=True, text=True, env=env, timeout=60,
    )
    return json.loads(r.stdout), r.returncode, env


class TestDispatch(unittest.TestCase):
    def setUp(self):
        self._repo = _repo()
        self._data = tempfile.mkdtemp()

    def test_pass_creates_worktree_and_agent_row(self):
        step = {
            "title": "fake step",
            "objective": "let the fake harness write a file",
            "contract": "n/a",
            "scope_deny": [],
            "acceptance": "test -f fake_out.txt",
            "files": ["fake_out.txt"],
        }
        verdict, rc, env = _run_dispatch(self._repo, self._data, step)
        self.assertEqual(rc, 0)
        self.assertTrue(verdict.get("pass"), f"expected pass, got: {verdict}")
        # worktree created under the repo
        wt = verdict.get("worktree", "")
        self.assertTrue(os.path.isdir(wt), f"worktree missing: {wt}")
        self.assertIn(os.path.join(".nexum-data", "worktrees"), wt)
        # the fake harness's edit is present in the worktree
        self.assertTrue(os.path.exists(os.path.join(wt, "fake_out.txt")))
        # harness token count flowed through
        self.assertEqual(verdict.get("tokens"), 10)

        # agents registry row is 'done'
        r = subprocess.run(
            [sys.executable, os.path.join(_SCRIPTS_DIR, "store.py"),
             "agent-get", "--id", "agent_test"],
            capture_output=True, text=True, env=env, timeout=30,
        )
        row = json.loads(r.stdout)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["harness"], "claude")
        self.assertEqual(row["repo_root"], os.path.realpath(self._repo))

    def test_failing_acceptance_marks_failed(self):
        step = {
            "title": "doomed step",
            "objective": "acceptance can never pass",
            "contract": "n/a",
            "scope_deny": [],
            "acceptance": "test -f this_file_never_exists_zzz",
            "files": ["fake_out.txt"],
        }
        verdict, rc, env = _run_dispatch(self._repo, self._data, step,
                                         slug="doomed", agent_id="agent_fail")
        self.assertEqual(rc, 0)
        self.assertFalse(verdict.get("pass"))
        # a failed step keeps the diff for a patch-retry
        self.assertIn("diff", verdict)


if __name__ == "__main__":
    unittest.main()
