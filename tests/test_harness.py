"""test_harness.py — stdlib unittest tests for scripts/harness.py"""

import os
import sys
import unittest

_SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import harness  # noqa: E402


class TestBuildCommand(unittest.TestCase):
    def test_claude_shape(self):
        cmd = harness.build_command("claude", "sonnet", "do the thing", "/repo")
        self.assertEqual(
            cmd,
            ["claude", "-p", "do the thing", "--output-format", "stream-json", "--verbose", "--model", "sonnet"],
        )

    def test_opencode_shape(self):
        cmd = harness.build_command("opencode", "gpt-5", "do the thing", "/repo")
        self.assertEqual(
            cmd,
            ["opencode", "run", "do the thing", "--model", "gpt-5", "--format", "json"],
        )

    def test_cursor_shape(self):
        cmd = harness.build_command("cursor", "auto", "do the thing", "/repo")
        self.assertEqual(
            cmd,
            ["cursor-agent", "-p", "do the thing", "--output-format", "stream-json", "--model", "auto"],
        )

    def test_unknown_harness_raises(self):
        with self.assertRaises(ValueError):
            harness.build_command("nope", "model", "prompt", "/repo")

    def test_env_override(self):
        old = os.environ.get("NEXUM_HARNESS_CMD_CLAUDE")
        os.environ["NEXUM_HARNESS_CMD_CLAUDE"] = "/usr/bin/env echo --flag value"
        try:
            cmd = harness.build_command("claude", "sonnet", "hello world", "/repo")
            self.assertEqual(cmd, ["/usr/bin/env", "echo", "--flag", "value", "hello world"])
        finally:
            if old is None:
                os.environ.pop("NEXUM_HARNESS_CMD_CLAUDE", None)
            else:
                os.environ["NEXUM_HARNESS_CMD_CLAUDE"] = old


class TestParseStream(unittest.TestCase):
    def test_result_line(self):
        lines = ['{"type":"result","tokens":5,"cost_usd":0.0}']
        parsed = harness.parse_stream("claude", lines)
        self.assertEqual(parsed["status"], "done")
        self.assertEqual(parsed["tokens"], 5)
        self.assertEqual(parsed["cost_usd"], 0.0)
        self.assertEqual(parsed["final_text"], "")

    def test_non_json_lines_ignored(self):
        lines = ["not json", "", '{"type":"result","tokens":3,"cost_usd":1.5,"result":"done text"}']
        parsed = harness.parse_stream("claude", lines)
        self.assertEqual(parsed["status"], "done")
        self.assertEqual(parsed["tokens"], 3)
        self.assertEqual(parsed["cost_usd"], 1.5)
        self.assertEqual(parsed["final_text"], "done text")

    def test_no_result_line_defaults(self):
        parsed = harness.parse_stream("claude", [])
        self.assertEqual(parsed, {"status": "running", "final_text": "", "tokens": 0, "cost_usd": 0.0})

    def test_error_type(self):
        lines = ['{"type":"error","message":"boom"}']
        parsed = harness.parse_stream("claude", lines)
        self.assertEqual(parsed["status"], "error")


class TestRun(unittest.TestCase):
    def test_fail_open_missing_binary(self):
        old = os.environ.get("NEXUM_HARNESS_CMD_CLAUDE")
        os.environ["NEXUM_HARNESS_CMD_CLAUDE"] = "/definitely/not/a/real/binary-nexum"
        try:
            result = harness.run(
                "claude", "sonnet", "hi", "/tmp", "/tmp/nexum-test-harness.log", timeout=5
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["rc"], -1)
            self.assertEqual(result["tokens"], 0)
            self.assertEqual(result["cost_usd"], 0.0)
            self.assertEqual(result["final_text"], "")
            self.assertEqual(result["log_path"], "/tmp/nexum-test-harness.log")
        finally:
            if old is None:
                os.environ.pop("NEXUM_HARNESS_CMD_CLAUDE", None)
            else:
                os.environ["NEXUM_HARNESS_CMD_CLAUDE"] = old

    def test_unknown_harness_fail_open(self):
        result = harness.run("nope", "model", "prompt", "/tmp", "/tmp/nexum-test-harness2.log")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["rc"], -1)


if __name__ == "__main__":
    unittest.main()
