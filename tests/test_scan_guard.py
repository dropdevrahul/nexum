"""
test_scan_guard.py — stdlib unittest tests for scripts/scan_guard.py

Covers ACCEPTANCE from §5.1:
- grep -r foo → deny
- grep -r foo src/ → allow
- Read node_modules/x → deny
- Read src/app.py → allow
- disabled flag → always allow
- malformed input → fail-open (allow)
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


def _run_scan_guard(payload, data_dir=None, extra_config=None):
    """Run scan_guard.py and return (parsed_output, exit_code)."""
    env = os.environ.copy()
    tmp = data_dir or tempfile.mkdtemp()
    env["CLAUDE_PLUGIN_DATA"] = tmp
    env["PYTHONPATH"] = _SCRIPTS_DIR + os.pathsep + env.get("PYTHONPATH", "")

    if extra_config:
        cfg_path = os.path.join(tmp, "config.json")
        with open(cfg_path, "w") as f:
            json.dump(extra_config, f)

    result = subprocess.run(
        [sys.executable, os.path.join(_SCRIPTS_DIR, "scan_guard.py")],
        input=json.dumps(payload).encode(),
        capture_output=True,
        env=env,
        timeout=15,
    )
    return json.loads(result.stdout.decode()), result.returncode


def _is_deny(out):
    """Check if the output is a deny decision."""
    try:
        return out["hookSpecificOutput"]["permissionDecision"] == "deny"
    except (KeyError, TypeError):
        return False


def _is_allow(out):
    """Check if the output is an allow ({} or no deny decision)."""
    return out == {} or not _is_deny(out)


class TestScanGuardGrepDeny(unittest.TestCase):
    """grep -r without scoped path → deny."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_grep_r_no_path_denied(self):
        """grep -r foo with no path is denied."""
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "grep -r foo"},
        }
        out, rc = _run_scan_guard(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertTrue(_is_deny(out), f"Expected deny, got: {out}")

    def test_grep_r_dot_path_denied(self):
        """grep -r foo . (root) is denied."""
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "grep -r foo ."},
        }
        out, rc = _run_scan_guard(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertTrue(_is_deny(out), f"Expected deny for 'grep -r foo .', got: {out}")

    def test_grep_recursive_upper_denied(self):
        """grep -R (uppercase) is also denied."""
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "grep -R foo"},
        }
        out, rc = _run_scan_guard(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertTrue(_is_deny(out))


class TestScanGuardGrepAllow(unittest.TestCase):
    """grep -r foo src/ (scoped) → allow."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_grep_r_scoped_path_allowed(self):
        """grep -r foo src/ has explicit non-root path → allowed."""
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "grep -r foo src/"},
        }
        out, rc = _run_scan_guard(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertTrue(_is_allow(out), f"Expected allow for scoped grep, got: {out}")

    def test_grep_r_subdir_allowed(self):
        """grep -r pattern lib/ — scoped to lib, must allow."""
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "grep -r pattern lib/"},
        }
        out, rc = _run_scan_guard(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertTrue(_is_allow(out))

    def test_non_recursive_grep_allowed(self):
        """grep foo file.txt (not recursive) must always be allowed."""
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "grep foo file.txt"},
        }
        out, rc = _run_scan_guard(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertTrue(_is_allow(out))


class TestScanGuardNodeModulesDeny(unittest.TestCase):
    """Read of node_modules/ file → deny."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_read_node_modules_denied(self):
        payload = {
            "tool_name": "Read",
            "tool_input": {"file_path": "node_modules/lodash/index.js"},
        }
        out, rc = _run_scan_guard(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertTrue(_is_deny(out), f"Expected deny for node_modules read, got: {out}")

    def test_read_deep_node_modules_denied(self):
        payload = {
            "tool_name": "Read",
            "tool_input": {"file_path": "node_modules/@scope/pkg/dist/index.js"},
        }
        out, rc = _run_scan_guard(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertTrue(_is_deny(out))

    def test_grep_into_node_modules_denied(self):
        """grep -r pattern node_modules/ → deny."""
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "grep -r pattern node_modules/"},
        }
        out, rc = _run_scan_guard(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertTrue(_is_deny(out))


class TestScanGuardNormalReadAllow(unittest.TestCase):
    """Read of a normal source file → allow."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_read_src_file_allowed(self):
        payload = {
            "tool_name": "Read",
            "tool_input": {"file_path": "src/app.py"},
        }
        out, rc = _run_scan_guard(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertTrue(_is_allow(out), f"Expected allow for src/app.py, got: {out}")

    def test_read_root_file_allowed(self):
        payload = {
            "tool_name": "Read",
            "tool_input": {"file_path": "README.md"},
        }
        out, rc = _run_scan_guard(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertTrue(_is_allow(out))

    def test_read_nested_src_file_allowed(self):
        payload = {
            "tool_name": "Read",
            "tool_input": {"file_path": "src/components/Button.tsx"},
        }
        out, rc = _run_scan_guard(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertTrue(_is_allow(out))


class TestScanGuardDisabled(unittest.TestCase):
    """scan_guard_enabled=false → always allow, even dangerous commands."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_disabled_allows_grep_r(self):
        """With guard disabled, even an unscoped grep -r is allowed."""
        cfg_path = os.path.join(self._tmp, "config.json")
        with open(cfg_path, "w") as f:
            json.dump({"scan_guard_enabled": False}, f)

        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "grep -r foo"},
        }
        out, rc = _run_scan_guard(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertTrue(_is_allow(out), f"Expected allow when disabled, got: {out}")

    def test_disabled_allows_node_modules_read(self):
        """With guard disabled, reading node_modules is allowed."""
        cfg_path = os.path.join(self._tmp, "config.json")
        with open(cfg_path, "w") as f:
            json.dump({"scan_guard_enabled": False}, f)

        payload = {
            "tool_name": "Read",
            "tool_input": {"file_path": "node_modules/foo/index.js"},
        }
        out, rc = _run_scan_guard(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertTrue(_is_allow(out))


class TestScanGuardFindCommand(unittest.TestCase):
    """find . (unscoped) → deny; find . -maxdepth 1 → allow."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_find_dot_denied(self):
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "find ."},
        }
        out, rc = _run_scan_guard(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertTrue(_is_deny(out), f"Expected deny for 'find .', got: {out}")

    def test_find_with_maxdepth_allowed(self):
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "find . -maxdepth 2 -name '*.py'"},
        }
        out, rc = _run_scan_guard(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertTrue(_is_allow(out), f"Expected allow for find with maxdepth, got: {out}")

    def test_find_specific_dir_allowed(self):
        """find src/ — explicit non-root path → allowed."""
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "find src/ -name '*.py'"},
        }
        out, rc = _run_scan_guard(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertTrue(_is_allow(out))


class TestScanGuardGrepToolDeny(unittest.TestCase):
    """Grep tool with broad pattern at root → deny."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def test_grep_tool_broad_pattern_denied(self):
        """Grep tool with path='' and pattern='**/*' is denied."""
        payload = {
            "tool_name": "Grep",
            "tool_input": {"path": "", "pattern": "**/*"},
        }
        out, rc = _run_scan_guard(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertTrue(_is_deny(out), f"Expected deny for broad Grep, got: {out}")

    def test_grep_tool_node_modules_path_denied(self):
        """Grep tool targeting node_modules path → deny."""
        payload = {
            "tool_name": "Grep",
            "tool_input": {"path": "node_modules", "pattern": "foo"},
        }
        out, rc = _run_scan_guard(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertTrue(_is_deny(out))

    def test_grep_tool_scoped_allowed(self):
        """Grep tool with a scoped path → allow."""
        payload = {
            "tool_name": "Grep",
            "tool_input": {"path": "src/", "pattern": "TODO"},
        }
        out, rc = _run_scan_guard(payload, self._tmp)
        self.assertEqual(rc, 0)
        self.assertTrue(_is_allow(out))


class TestScanGuardFailOpen(unittest.TestCase):
    """Malformed input → fail-open (allow, exit 0)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()

    def _run_raw(self, raw_bytes):
        env = os.environ.copy()
        env["CLAUDE_PLUGIN_DATA"] = self._tmp
        env["PYTHONPATH"] = _SCRIPTS_DIR + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, os.path.join(_SCRIPTS_DIR, "scan_guard.py")],
            input=raw_bytes,
            capture_output=True,
            env=env,
            timeout=15,
        )
        return result.stdout.decode(), result.returncode

    def test_malformed_json_fail_open(self):
        out_str, rc = self._run_raw(b"NOT JSON {{{")
        self.assertEqual(rc, 0)
        out = json.loads(out_str)
        self.assertTrue(_is_allow(out), f"Expected allow on malformed input, got: {out}")

    def test_empty_input_fail_open(self):
        out_str, rc = self._run_raw(b"")
        self.assertEqual(rc, 0)
        out = json.loads(out_str)
        self.assertTrue(_is_allow(out))

    def test_valid_json_unknown_tool_allowed(self):
        """Unknown tool name → allow."""
        payload = {
            "tool_name": "UnknownTool",
            "tool_input": {"something": "value"},
        }
        env = os.environ.copy()
        env["CLAUDE_PLUGIN_DATA"] = self._tmp
        env["PYTHONPATH"] = _SCRIPTS_DIR + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, os.path.join(_SCRIPTS_DIR, "scan_guard.py")],
            input=json.dumps(payload).encode(),
            capture_output=True,
            env=env,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0)
        out = json.loads(result.stdout.decode())
        self.assertTrue(_is_allow(out))


if __name__ == "__main__":
    unittest.main()
