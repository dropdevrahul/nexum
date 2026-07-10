"""test_worktree.py — stdlib unittest for scripts/worktree.py

Covers: is_dirty tracks tree state; create_worktree checks out HEAD, copies
configured extras (honouring the ignore list), reuses an existing path, and
fails open (None) outside a git repo.
"""

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

import worktree  # noqa: E402


def _git(cwd, *args):
    subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, check=False)


def _repo():
    d = tempfile.mkdtemp()
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    with open(os.path.join(d, "a.txt"), "w") as f:
        f.write("hello")
    _git(d, "add", "a.txt")
    _git(d, "commit", "-qm", "init")
    return d


class TestIsDirty(unittest.TestCase):
    def test_clean_then_dirty(self):
        d = _repo()
        self.assertFalse(worktree.is_dirty(d))
        with open(os.path.join(d, "a.txt"), "w") as f:
            f.write("changed")
        self.assertTrue(worktree.is_dirty(d))

    def test_untracked_is_dirty(self):
        d = _repo()
        with open(os.path.join(d, "new.txt"), "w") as f:
            f.write("x")
        self.assertTrue(worktree.is_dirty(d))

    def test_non_repo_not_dirty(self):
        self.assertFalse(worktree.is_dirty(tempfile.mkdtemp()))


class TestCreateWorktree(unittest.TestCase):
    def test_creates_at_head_and_copies_extras(self):
        d = _repo()
        with open(os.path.join(d, ".env"), "w") as f:
            f.write("SECRET=1")
        with open(os.path.join(d, "skip.local"), "w") as f:
            f.write("nope")
        wt = worktree.create_worktree(
            d, "feature-abc123",
            copy_globs=[".env", "*.local"],
            ignore_globs=["*.local"],
        )
        self.assertIsNotNone(wt)
        self.assertTrue(os.path.isdir(wt))
        # under .nexum-data/worktrees
        self.assertIn(os.path.join(".nexum-data", "worktrees"), wt)
        # tracked file present at HEAD
        with open(os.path.join(wt, "a.txt")) as f:
            self.assertEqual(f.read(), "hello")
        # copied extra present, ignored extra absent
        self.assertTrue(os.path.exists(os.path.join(wt, ".env")))
        self.assertFalse(os.path.exists(os.path.join(wt, "skip.local")))

    def test_reuse_returns_same_path(self):
        d = _repo()
        wt1 = worktree.create_worktree(d, "fix-deadbe")
        wt2 = worktree.create_worktree(d, "fix-deadbe")
        self.assertEqual(wt1, wt2)

    def test_non_repo_returns_none(self):
        self.assertIsNone(worktree.create_worktree(tempfile.mkdtemp(), "x-000000"))


if __name__ == "__main__":
    unittest.main()
