"""
test_handoff.py — stdlib unittest tests for scripts/handoff.py

Covers the deterministic handoff-skeleton writer:
- build_skeleton renders git state + task + tokens
- write_skeleton writes both per-session and latest.md
- task signature is humanized
- fail-open on a non-git / bad cwd
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


class TestHumanizeTask(unittest.TestCase):
    def test_unpacks_signature(self):
        import handoff
        sig = json.dumps(["__type__:feature", "billing", "invoice"])
        out = handoff._humanize_task(sig)
        self.assertIn("type: feature", out)
        self.assertIn("billing", out)
        self.assertIn("invoice", out)

    def test_none_returns_placeholder(self):
        import handoff
        self.assertEqual(handoff._humanize_task(None), "(none recorded)")

    def test_non_json_returns_raw(self):
        import handoff
        self.assertEqual(handoff._humanize_task("fix the login bug"), "fix the login bug")


class TestBuildSkeleton(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["CLAUDE_PLUGIN_DATA"] = self._tmp

    def tearDown(self):
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)

    def test_contains_core_sections(self):
        import handoff
        md = handoff.build_skeleton("sess1", os.getcwd(), token_total=105000)
        self.assertIn("# Handoff (auto-skeleton)", md)
        self.assertIn("**Session:** sess1", md)
        self.assertIn("105k tokens", md)
        self.assertIn("## Git state", md)
        self.assertIn("## How to resume", md)

    def test_non_git_cwd_does_not_raise(self):
        import handoff
        md = handoff.build_skeleton("sess1", self._tmp, token_total=0)
        # branch resolves to the unknown placeholder, no exception
        self.assertIn("(unknown)", md)


class TestWriteSkeleton(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["CLAUDE_PLUGIN_DATA"] = self._tmp

    def tearDown(self):
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)

    def test_writes_both_files(self):
        import handoff
        path = handoff.write_skeleton("sessW", cwd=os.getcwd(), token_total=101000)
        self.assertIsNotNone(path)
        per_session = os.path.join(self._tmp, "handoff", "sessW.md")
        latest = os.path.join(self._tmp, "handoff", "latest.md")
        self.assertTrue(os.path.isfile(per_session))
        self.assertTrue(os.path.isfile(latest))
        with open(latest) as f:
            self.assertEqual(f.read(), open(per_session).read())

    def test_latest_records_session_id(self):
        import handoff
        handoff.write_skeleton("sessW2", cwd=os.getcwd())
        with open(os.path.join(self._tmp, "handoff", "latest.md")) as f:
            self.assertIn("**Session:** sessW2", f.read())


class TestHandoffCLI(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._handoff = os.path.join(_SCRIPTS_DIR, "handoff.py")

    def _run(self, *args):
        env = os.environ.copy()
        env["CLAUDE_PLUGIN_DATA"] = self._tmp
        env["PYTHONPATH"] = _SCRIPTS_DIR + os.pathsep + env.get("PYTHONPATH", "")
        r = subprocess.run([sys.executable, self._handoff, *args],
                           capture_output=True, env=env, timeout=15)
        return r.stdout.decode(), r.returncode

    def test_write_cli(self):
        out, rc = self._run("write", "--session", "sCLI", "--cwd", os.getcwd(), "--tokens", "100001")
        self.assertEqual(rc, 0)
        res = json.loads(out)
        self.assertTrue(res["ok"])
        self.assertTrue(os.path.isfile(os.path.join(self._tmp, "handoff", "latest.md")))


if __name__ == "__main__":
    unittest.main()
