"""test_mcp_server.py — stdlib unittest tests for scripts/mcp_server.py"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SCRIPTS_DIR = os.path.join(_ROOT, "scripts")
_FAKE = os.path.join(_ROOT, "tests", "fixtures", "fake_harness.py")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import mcp_server  # noqa: E402


def call(msg):
    return mcp_server.handle_message(msg)


class TestProtocol(unittest.TestCase):
    def test_initialize_echoes_protocol_and_names_server(self):
        r = call({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(r["result"]["protocolVersion"], "2025-06-18")
        self.assertEqual(r["result"]["serverInfo"]["name"], "nexum-delegate")
        self.assertIn("tools", r["result"]["capabilities"])

    def test_notifications_get_no_reply(self):
        self.assertIsNone(call({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_tools_list_exposes_all_tools(self):
        r = call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {t["name"] for t in r["result"]["tools"]}
        self.assertEqual(names, {"delegate", "delegate_async", "check", "list_agents"})

    def test_unknown_method_is_jsonrpc_error(self):
        r = call({"jsonrpc": "2.0", "id": 3, "method": "bogus/method"})
        self.assertEqual(r["error"]["code"], -32601)

    def test_unknown_tool_is_error(self):
        r = call({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                  "params": {"name": "nope", "arguments": {}}})
        self.assertIn("error", r)


class TestDelegateValidation(unittest.TestCase):
    def test_bad_harness_rejected_before_running(self):
        r = call({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                  "params": {"name": "delegate",
                             "arguments": {"harness": "bogus", "task": "x"}}})
        self.assertIn("harness must be one of", r["result"]["content"][0]["text"])

    def test_empty_task_rejected(self):
        r = call({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                  "params": {"name": "delegate",
                             "arguments": {"harness": "cursor", "task": "   "}}})
        self.assertIn("task is required", r["result"]["content"][0]["text"])


class TestParseLastJson(unittest.TestCase):
    def test_returns_last_object_ignoring_noise(self):
        self.assertEqual(
            mcp_server._parse_last_json('log line\n{"a":1}\n{"pass":true}\n'),
            {"pass": True},
        )

    def test_none_when_no_json(self):
        self.assertIsNone(mcp_server._parse_last_json("no json here\n"))


def _git(cwd, *args):
    subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, check=False)


def _seed_repo():
    d = tempfile.mkdtemp()
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@t")
    _git(d, "config", "user.name", "t")
    with open(os.path.join(d, "seed.txt"), "w") as f:
        f.write("seed")
    _git(d, "add", "seed.txt")
    _git(d, "commit", "-qm", "init")
    return d


class TestDelegateEndToEnd(unittest.TestCase):
    """Drive the real delegate tool through dispatch.py with a fake harness in a
    throwaway git repo — proves the whole worktree→run→guardrail→verdict path."""

    def setUp(self):
        self._cwd = os.getcwd()
        self._repo = _seed_repo()
        os.chdir(self._repo)
        self._saved = {k: os.environ.get(k) for k in
                       ("NEXUM_HARNESS_CMD_CURSOR", "CLAUDE_PLUGIN_DATA")}
        os.environ["NEXUM_HARNESS_CMD_CURSOR"] = f"{sys.executable} {_FAKE}"
        os.environ["CLAUDE_PLUGIN_DATA"] = os.path.join(self._repo, ".nexum-data")

    def tearDown(self):
        os.chdir(self._cwd)
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_delegate_runs_harness_and_returns_pass_verdict(self):
        out = mcp_server._tool_delegate({
            "harness": "cursor",
            "task": "let the fake harness write a file",
            "acceptance": "test -f fake_out.txt",
            "files": ["fake_out.txt"],
        })
        verdict = json.loads(out)
        self.assertTrue(verdict.get("pass"), out)
        self.assertEqual(verdict.get("harness"), "cursor")
        self.assertIn(".nexum-data", verdict.get("worktree", ""))
        self.assertTrue(os.path.exists(os.path.join(verdict["worktree"], "fake_out.txt")))
        self.assertTrue(verdict.get("agent_id"))

        # the delegated sub-agent is now visible via list_agents (the TUI's data)
        listed = json.loads(mcp_server._tool_list_agents({}))
        ids = {a["id"] for a in listed}
        self.assertIn(verdict["agent_id"], ids)

    def test_delegate_failing_acceptance_reports_not_pass(self):
        out = mcp_server._tool_delegate({
            "harness": "cursor",
            "task": "acceptance can never pass",
            "acceptance": "test -f never_exists_zzz",
        })
        verdict = json.loads(out)
        self.assertFalse(verdict.get("pass"), out)

    def test_delegate_async_then_check_reaches_done(self):
        started = json.loads(mcp_server._tool_delegate_async({
            "harness": "cursor",
            "task": "async fake harness writes a file",
            "acceptance": "test -f fake_out.txt",
            "files": ["fake_out.txt"],
        }))
        aid = started["agent_id"]
        self.assertEqual(started["status"], "running")

        # poll check() until the detached dispatch finishes
        deadline = time.time() + 20
        state = {}
        while time.time() < deadline:
            state = json.loads(mcp_server._tool_check({"agent_id": aid}))
            if state.get("done"):
                break
            time.sleep(0.2)
        self.assertTrue(state.get("done"), f"never finished: {state}")
        self.assertTrue(state.get("pass"), state)
        self.assertEqual(state.get("status"), "done")

    def test_check_unknown_agent(self):
        state = json.loads(mcp_server._tool_check({"agent_id": "does_not_exist_zzz"}))
        self.assertEqual(state["status"], "unknown")

    def test_delegate_async_validates(self):
        self.assertIn("harness must be one of",
                      mcp_server._tool_delegate_async({"harness": "x", "task": "t"}))
        self.assertIn("agent_id is required", mcp_server._tool_check({}))


if __name__ == "__main__":
    unittest.main()
