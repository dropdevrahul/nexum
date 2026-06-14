"""
test_store.py — stdlib unittest tests for scripts/store.py

Covers ACCEPTANCE from §2:
- import store; store.db() creates nexum.db
- round-trip every helper
- two processes writing concurrently don't error
- missing CLAUDE_PLUGIN_DATA falls back correctly
"""

import json
import multiprocessing
import os
import sqlite3
import sys
import tempfile
import time
import unittest


# ---------------------------------------------------------------------------
# Make scripts/ importable
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


class TestNexumDataDir(unittest.TestCase):
    """nexum_data_dir() resolution."""

    def _fresh_env(self, tmp):
        """Return a dict of env vars that isolate the test."""
        env = os.environ.copy()
        env["CLAUDE_PLUGIN_DATA"] = tmp
        env.pop("CLAUDE_PLUGIN_ROOT", None)
        return env

    def test_uses_claude_plugin_data(self):
        """CLAUDE_PLUGIN_DATA env var is respected."""
        import importlib
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.copy()
            os.environ["CLAUDE_PLUGIN_DATA"] = tmp
            os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
            try:
                import store
                importlib.reload(store)
                result = store.nexum_data_dir()
                self.assertEqual(os.path.realpath(result), os.path.realpath(tmp))
            finally:
                os.environ.clear()
                os.environ.update(old)

    def test_creates_directory(self):
        """nexum_data_dir() creates the directory if it doesn't exist."""
        with tempfile.TemporaryDirectory() as tmp:
            new_dir = os.path.join(tmp, "subdir", "nexum-data")
            old_env = os.environ.get("CLAUDE_PLUGIN_DATA")
            os.environ["CLAUDE_PLUGIN_DATA"] = new_dir
            try:
                import store
                result = store.nexum_data_dir()
                self.assertTrue(os.path.isdir(result))
            finally:
                if old_env is None:
                    os.environ.pop("CLAUDE_PLUGIN_DATA", None)
                else:
                    os.environ["CLAUDE_PLUGIN_DATA"] = old_env

    def test_fallback_no_env(self):
        """Without CLAUDE_PLUGIN_DATA or CLAUDE_PLUGIN_ROOT, falls back to .nexum-data."""
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.copy()
            os.environ.pop("CLAUDE_PLUGIN_DATA", None)
            os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                import store
                result = store.nexum_data_dir()
                # Should be a path ending in .nexum-data
                self.assertTrue(result.endswith(".nexum-data"))
                self.assertTrue(os.path.isdir(result))
            finally:
                os.chdir(old_cwd)
                os.environ.clear()
                os.environ.update(old)


class TestDb(unittest.TestCase):
    """store.db() opens/creates nexum.db."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["CLAUDE_PLUGIN_DATA"] = self._tmp

    def tearDown(self):
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)

    def test_db_creates_file(self):
        """db() creates nexum.db in the data dir."""
        import store
        conn = store.db()
        conn.close()
        self.assertTrue(os.path.isfile(os.path.join(self._tmp, "nexum.db")))

    def test_db_tables_exist(self):
        """db() creates all required tables."""
        import store
        conn = store.db()
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        for table in ("outputs", "memo", "session_kv", "usage"):
            self.assertIn(table, tables, f"Table {table!r} missing from nexum.db")

    def test_db_returns_connection(self):
        """db() returns a sqlite3.Connection."""
        import store
        conn = store.db()
        self.assertIsInstance(conn, sqlite3.Connection)
        conn.close()

    def test_db_wal_mode(self):
        """db() enables WAL journal mode."""
        import store
        conn = store.db()
        row = conn.execute("PRAGMA journal_mode").fetchone()
        conn.close()
        self.assertEqual(row[0].lower(), "wal")


class TestGetConfig(unittest.TestCase):
    """get_config() returns defaults merged with config.json."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["CLAUDE_PLUGIN_DATA"] = self._tmp

    def tearDown(self):
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)

    def test_defaults_present(self):
        """get_config() returns all documented default keys."""
        import store
        cfg = store.get_config()
        required = [
            "truncate_max_lines",
            "truncate_head_lines",
            "truncate_tail_lines",
            "truncate_min_lines_to_act",
            "keep_error_regex",
            "compaction_threshold_tokens",
            "scan_guard_enabled",
            "scan_deny_paths",
            "intent_guard_enabled",
            "intent_similarity_threshold",
        ]
        for key in required:
            self.assertIn(key, cfg, f"Default config key {key!r} missing")

    def test_config_json_overrides(self):
        """config.json values win over defaults."""
        import store
        cfg_path = os.path.join(self._tmp, "config.json")
        with open(cfg_path, "w") as f:
            json.dump({"truncate_max_lines": 999}, f)
        cfg = store.get_config()
        self.assertEqual(cfg["truncate_max_lines"], 999)

    def test_corrupt_config_json_silently_ignored(self):
        """Corrupt config.json falls back to defaults without error."""
        import store
        cfg_path = os.path.join(self._tmp, "config.json")
        with open(cfg_path, "w") as f:
            f.write("NOT JSON{{{{")
        cfg = store.get_config()
        self.assertIn("truncate_max_lines", cfg)

    def test_default_scan_deny_paths(self):
        """scan_deny_paths default includes node_modules."""
        import store
        cfg = store.get_config()
        self.assertIn("node_modules", cfg["scan_deny_paths"])


class TestSha256(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["CLAUDE_PLUGIN_DATA"] = self._tmp

    def tearDown(self):
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)

    def test_returns_hex_string(self):
        import store
        result = store.sha256("hello")
        self.assertIsInstance(result, str)
        self.assertEqual(len(result), 64)  # SHA-256 hex

    def test_deterministic(self):
        import store
        self.assertEqual(store.sha256("foo"), store.sha256("foo"))

    def test_different_inputs_differ(self):
        import store
        self.assertNotEqual(store.sha256("foo"), store.sha256("bar"))


class TestEstimateTokens(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["CLAUDE_PLUGIN_DATA"] = self._tmp

    def tearDown(self):
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)

    def test_empty_string_returns_1(self):
        import store
        self.assertEqual(store.estimate_tokens(""), 1)

    def test_formula(self):
        import store
        text = "a" * 100
        self.assertEqual(store.estimate_tokens(text), max(1, 100 // 4))

    def test_minimum_one(self):
        import store
        self.assertGreaterEqual(store.estimate_tokens("x"), 1)


class TestDedupHelpers(unittest.TestCase):
    """seen_output / record_output round-trips."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["CLAUDE_PLUGIN_DATA"] = self._tmp

    def tearDown(self):
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)

    def test_seen_output_none_when_absent(self):
        import store
        result = store.seen_output("sess1", "abc123")
        self.assertIsNone(result)

    def test_record_then_seen(self):
        import store
        h = store.sha256("some content")
        store.record_output("sess1", "Read", h, "summary", 100)
        row = store.seen_output("sess1", h)
        self.assertIsNotNone(row)
        self.assertEqual(row["tool_name"], "Read")
        self.assertEqual(row["token_count"], 100)

    def test_different_session_not_seen(self):
        import store
        h = store.sha256("some content 2")
        store.record_output("sessA", "Read", h, "summary", 50)
        self.assertIsNone(store.seen_output("sessB", h))


class TestMemoHelpers(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["CLAUDE_PLUGIN_DATA"] = self._tmp

    def tearDown(self):
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)

    def test_memo_get_none_when_absent(self):
        import store
        self.assertIsNone(store.memo_get("nonexistent"))

    def test_memo_put_then_get(self):
        import store
        store.memo_put("hash1", "result text")
        self.assertEqual(store.memo_get("hash1"), "result text")

    def test_memo_put_overwrites(self):
        import store
        store.memo_put("hash2", "v1")
        store.memo_put("hash2", "v2")
        self.assertEqual(store.memo_get("hash2"), "v2")


class TestSessionKV(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["CLAUDE_PLUGIN_DATA"] = self._tmp

    def tearDown(self):
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)

    def test_get_flag_none_when_absent(self):
        import store
        self.assertIsNone(store.get_flag("sess1", "mykey"))

    def test_set_then_get_flag(self):
        import store
        store.set_flag("sess1", "mykey", "myval")
        self.assertEqual(store.get_flag("sess1", "mykey"), "myval")

    def test_set_flag_overwrites(self):
        import store
        store.set_flag("sess1", "k", "v1")
        store.set_flag("sess1", "k", "v2")
        self.assertEqual(store.get_flag("sess1", "k"), "v2")

    def test_get_session_task_none(self):
        import store
        self.assertIsNone(store.get_session_task("newsess"))

    def test_set_session_task_round_trip(self):
        import store
        store.set_session_task("sess2", "implement payment")
        self.assertEqual(store.get_session_task("sess2"), "implement payment")


class TestUsageHelpers(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["CLAUDE_PLUGIN_DATA"] = self._tmp

    def tearDown(self):
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)

    def test_add_usage_round_trip(self):
        import store
        store.add_usage("sess1", "sonnet", 1000, 500, 200)
        rows = store.usage_rows("sess1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "sonnet")
        self.assertEqual(rows[0]["input_tok"], 1000)
        self.assertEqual(rows[0]["output_tok"], 500)
        self.assertEqual(rows[0]["cache_read_tok"], 200)

    def test_usage_rows_no_session_returns_all(self):
        import store
        store.add_usage("sessA", "haiku", 100, 50)
        store.add_usage("sessB", "opus", 200, 100)
        rows = store.usage_rows()
        sessions = {r["session_id"] for r in rows}
        self.assertIn("sessA", sessions)
        self.assertIn("sessB", sessions)

    def test_usage_rows_filtered(self):
        import store
        store.add_usage("sessX", "sonnet", 300, 150)
        store.add_usage("sessY", "haiku", 100, 50)
        rows = store.usage_rows("sessX")
        self.assertTrue(all(r["session_id"] == "sessX" for r in rows))


def _worker_write(tmp_dir, n):
    """Write n usage rows from a child process."""
    os.environ["CLAUDE_PLUGIN_DATA"] = tmp_dir
    # Fresh import in the child process
    if _SCRIPTS_DIR not in sys.path:
        sys.path.insert(0, _SCRIPTS_DIR)
    import store
    for i in range(n):
        store.add_usage(f"sess_{n}_{i}", "haiku", i, i)


class TestConcurrentWrites(unittest.TestCase):
    """Two processes writing concurrently don't error (§2 ACCEPTANCE)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["CLAUDE_PLUGIN_DATA"] = self._tmp

    def tearDown(self):
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)

    def test_concurrent_writes(self):
        p1 = multiprocessing.Process(target=_worker_write, args=(self._tmp, 5))
        p2 = multiprocessing.Process(target=_worker_write, args=(self._tmp, 5))
        p1.start()
        p2.start()
        p1.join(timeout=10)
        p2.join(timeout=10)
        self.assertEqual(p1.exitcode, 0, "Process 1 crashed during concurrent write")
        self.assertEqual(p2.exitcode, 0, "Process 2 crashed during concurrent write")


if __name__ == "__main__":
    unittest.main()
