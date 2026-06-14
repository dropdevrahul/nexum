"""
test_audit.py — stdlib unittest tests for scripts/audit.py

Covers ACCEPTANCE from §5.3:
- repo with unignored node_modules → flagged
- --write adds it once and is a no-op on second run
- missing ignore file → recommends creating one
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


def _run_audit(root, data_dir=None, write=False):
    """Run audit.py and return (stdout_str, exit_code)."""
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = data_dir or tempfile.mkdtemp()
    env["PYTHONPATH"] = _SCRIPTS_DIR + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [sys.executable, os.path.join(_SCRIPTS_DIR, "audit.py"), "--root", root]
    if write:
        cmd.append("--write")
    result = subprocess.run(
        cmd,
        capture_output=True,
        env=env,
        timeout=30,
    )
    return result.stdout.decode(), result.returncode


class TestAuditUnignoredNodeModules(unittest.TestCase):
    """Repo with unignored node_modules must be flagged in the report."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._data_dir = tempfile.mkdtemp()

    def _create_repo_with_node_modules(self, ignore_file=None, ignore_content=None):
        """Create a minimal repo structure with node_modules/."""
        nm = os.path.join(self._tmp, "node_modules")
        os.makedirs(nm, exist_ok=True)
        # Create a file inside to make it a real dir
        with open(os.path.join(nm, "index.js"), "w") as f:
            f.write("// package")
        if ignore_file and ignore_content is not None:
            with open(os.path.join(self._tmp, ignore_file), "w") as f:
                f.write(ignore_content)

    def test_node_modules_flagged_no_ignore(self):
        """No ignore file + node_modules → flagged as unignored noise dir."""
        self._create_repo_with_node_modules()
        out, rc = _run_audit(self._tmp, self._data_dir)
        self.assertEqual(rc, 0)
        # Should mention node_modules in finding 2
        self.assertIn("node_modules", out)

    def test_node_modules_flagged_empty_gitignore(self):
        """Empty .gitignore + node_modules/ on disk → flagged."""
        self._create_repo_with_node_modules(".gitignore", "")
        out, rc = _run_audit(self._tmp, self._data_dir)
        self.assertEqual(rc, 0)
        self.assertIn("node_modules", out)

    def test_node_modules_not_flagged_when_ignored(self):
        """node_modules in .gitignore → not flagged as unignored."""
        self._create_repo_with_node_modules(".gitignore", "node_modules/\n")
        out, rc = _run_audit(self._tmp, self._data_dir)
        self.assertEqual(rc, 0)
        # Finding 2 should say all noise dirs are absent or already ignored
        self.assertIn("Finding 2", out)
        # The key test: node_modules should NOT appear in "not ignored" listing
        lines = out.split("\n")
        # Gather only Finding 2 lines
        in_f2 = False
        f2_lines = []
        for line in lines:
            if "Finding 2" in line:
                in_f2 = True
            elif in_f2 and "Finding 3" in line:
                break
            if in_f2:
                f2_lines.append(line)
        f2_text = "\n".join(f2_lines)
        # node_modules should not appear as a "noise dir not ignored" entry
        if "node_modules/" in f2_text:
            # This means it WAS listed — test fails
            self.fail(f"node_modules flagged even though it's in .gitignore: {f2_text}")


class TestAuditMissingIgnoreFile(unittest.TestCase):
    """No ignore file at all → report recommends creating one."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._data_dir = tempfile.mkdtemp()

    def test_missing_ignore_mentioned(self):
        out, rc = _run_audit(self._tmp, self._data_dir)
        self.assertEqual(rc, 0)
        self.assertIn("Finding 1", out)
        # Either says "NO ignore file" or recommends creating one
        lower = out.lower()
        self.assertTrue(
            "no ignore file" in lower or "recommend" in lower or "not found" in lower,
            f"Expected recommendation to create ignore file, got:\n{out}"
        )


class TestAuditWriteIdempotent(unittest.TestCase):
    """--write adds node_modules once; second run is a no-op."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._data_dir = tempfile.mkdtemp()
        # Create node_modules so it's flagged
        nm = os.path.join(self._tmp, "node_modules")
        os.makedirs(nm, exist_ok=True)
        with open(os.path.join(nm, "x.js"), "w") as f:
            f.write("// x")

    def test_write_adds_pattern_once(self):
        """First --write run adds node_modules to the ignore file."""
        out, rc = _run_audit(self._tmp, self._data_dir, write=True)
        self.assertEqual(rc, 0)
        # Either created a new file or added to existing
        ignore_candidates = [
            os.path.join(self._tmp, ".claudeignore"),
            os.path.join(self._tmp, ".gitignore"),
        ]
        ignore_exists = any(os.path.isfile(p) for p in ignore_candidates)
        self.assertTrue(ignore_exists, "No ignore file was created by --write")

        # The file must contain node_modules
        for candidate in ignore_candidates:
            if os.path.isfile(candidate):
                with open(candidate) as f:
                    content = f.read()
                if "node_modules" in content:
                    break
        else:
            self.fail("node_modules pattern not written to ignore file")

    def test_write_is_idempotent(self):
        """Second --write run adds nothing new (idempotent)."""
        # First run
        _run_audit(self._tmp, self._data_dir, write=True)

        # Read content after first run
        ignore_candidates = [
            os.path.join(self._tmp, ".claudeignore"),
            os.path.join(self._tmp, ".gitignore"),
        ]
        content_before = None
        for candidate in ignore_candidates:
            if os.path.isfile(candidate):
                with open(candidate) as f:
                    content_before = f.read()
                break

        if content_before is None:
            self.skipTest("No ignore file created on first --write run")

        # Second run
        _run_audit(self._tmp, self._data_dir, write=True)

        content_after = None
        for candidate in ignore_candidates:
            if os.path.isfile(candidate):
                with open(candidate) as f:
                    content_after = f.read()
                break

        # Check that node_modules is not duplicated
        node_mod_count_before = content_before.count("node_modules")
        node_mod_count_after = content_after.count("node_modules")
        self.assertEqual(
            node_mod_count_before, node_mod_count_after,
            f"Second --write duplicated patterns: before={node_mod_count_before}, "
            f"after={node_mod_count_after}"
        )


class TestAuditRunAuditUnit(unittest.TestCase):
    """Unit-test run_audit() directly."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._data_dir = tempfile.mkdtemp()
        os.environ["CLAUDE_PLUGIN_DATA"] = self._data_dir

    def tearDown(self):
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)

    def test_missing_ignore_flag(self):
        import audit
        findings = audit.run_audit(self._tmp)
        self.assertTrue(findings["missing_ignore"])
        self.assertIsNone(findings["ignore_path"])

    def test_node_modules_in_unignored_noise(self):
        import audit
        nm = os.path.join(self._tmp, "node_modules")
        os.makedirs(nm, exist_ok=True)
        findings = audit.run_audit(self._tmp)
        self.assertIn("node_modules", findings["unignored_noise_dirs"])

    def test_node_modules_ignored_not_in_noise(self):
        import audit
        nm = os.path.join(self._tmp, "node_modules")
        os.makedirs(nm, exist_ok=True)
        gi_path = os.path.join(self._tmp, ".gitignore")
        with open(gi_path, "w") as f:
            f.write("node_modules/\n")
        findings = audit.run_audit(self._tmp)
        self.assertNotIn("node_modules", findings["unignored_noise_dirs"])

    def test_clean_repo_no_findings(self):
        """Completely empty repo (no noise dirs, no ignore file)."""
        import audit
        findings = audit.run_audit(self._tmp)
        # missing_ignore is a finding but unignored noise is empty
        self.assertEqual(findings["unignored_noise_dirs"], [])
        self.assertEqual(findings["large_or_binary"], [])


class TestAuditWriteIgnoreUnit(unittest.TestCase):
    """Unit-test _write_ignore() for idempotency."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._data_dir = tempfile.mkdtemp()
        os.environ["CLAUDE_PLUGIN_DATA"] = self._data_dir

    def tearDown(self):
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)

    def test_write_adds_nexum_block(self):
        import audit
        ignore_path = os.path.join(self._tmp, ".gitignore")
        with open(ignore_path, "w") as f:
            f.write("dist/\n")
        added, skipped = audit._write_ignore(ignore_path, ["node_modules/", "build/"])
        self.assertIn("node_modules/", added)
        self.assertIn("build/", added)

    def test_write_skips_existing(self):
        import audit
        ignore_path = os.path.join(self._tmp, ".gitignore")
        with open(ignore_path, "w") as f:
            f.write("node_modules/\n")
        added, skipped = audit._write_ignore(ignore_path, ["node_modules/", "dist/"])
        self.assertIn("node_modules/", skipped)
        self.assertIn("dist/", added)

    def test_write_idempotent_twice(self):
        import audit
        ignore_path = os.path.join(self._tmp, ".gitignore")
        with open(ignore_path, "w") as f:
            f.write("")
        patterns = ["node_modules/", ".venv/"]
        audit._write_ignore(ignore_path, patterns)
        audit._write_ignore(ignore_path, patterns)
        with open(ignore_path) as f:
            content = f.read()
        self.assertEqual(content.count("node_modules/"), 1,
                         "node_modules/ duplicated after second write")

    def test_nexum_block_marker_present(self):
        import audit
        ignore_path = os.path.join(self._tmp, ".gitignore")
        with open(ignore_path, "w") as f:
            f.write("")
        audit._write_ignore(ignore_path, ["dist/"])
        with open(ignore_path) as f:
            content = f.read()
        self.assertIn("# nexum", content)


class TestAuditIgnoreFilesFunction(unittest.TestCase):
    """ignore_files() detection logic."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._data_dir = tempfile.mkdtemp()
        os.environ["CLAUDE_PLUGIN_DATA"] = self._data_dir

    def tearDown(self):
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)

    def test_claudeignore_preferred(self):
        import audit
        # Create both
        with open(os.path.join(self._tmp, ".claudeignore"), "w") as f:
            f.write("node_modules/\n")
        with open(os.path.join(self._tmp, ".gitignore"), "w") as f:
            f.write("dist/\n")
        path, label = audit.ignore_files(self._tmp)
        self.assertIsNotNone(path)
        self.assertIn(".claudeignore", path)

    def test_gitignore_fallback(self):
        import audit
        with open(os.path.join(self._tmp, ".gitignore"), "w") as f:
            f.write("dist/\n")
        path, label = audit.ignore_files(self._tmp)
        self.assertIsNotNone(path)
        self.assertIn(".gitignore", path)

    def test_no_ignore_file(self):
        import audit
        path, label = audit.ignore_files(self._tmp)
        self.assertIsNone(path)
        self.assertIsNone(label)


if __name__ == "__main__":
    unittest.main()
