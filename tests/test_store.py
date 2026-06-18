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
            "handoff_threshold_tokens",
            "max_same_tier_retries",
            "max_steps_per_dispatch",
            "orchestrator_resume_enabled",
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


class TestTranscriptToolResultLen(unittest.TestCase):
    """transcript_tool_result_len reads the correct length from a JSONL transcript."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["CLAUDE_PLUGIN_DATA"] = self._tmp

    def tearDown(self):
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)

    def _write_transcript(self, path, tool_use_id, content):
        """Write a minimal transcript JSONL file with one tool_result entry."""
        line = json.dumps({
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": content,
                    }
                ]
            },
        })
        with open(path, "w") as f:
            f.write(line + "\n")

    def test_known_tool_use_id_returns_length(self):
        """Returns the character length of the content for a known tool_use_id."""
        import store
        tf = os.path.join(self._tmp, "transcript.jsonl")
        self._write_transcript(tf, "toolu_1", "hello world")
        result = store.transcript_tool_result_len(tf, "toolu_1")
        self.assertEqual(result, 11)

    def test_unknown_tool_use_id_returns_none(self):
        """Returns None when the tool_use_id is not in the transcript."""
        import store
        tf = os.path.join(self._tmp, "transcript.jsonl")
        self._write_transcript(tf, "toolu_1", "hello world")
        result = store.transcript_tool_result_len(tf, "toolu_unknown")
        self.assertIsNone(result)

    def test_nonexistent_path_returns_none(self):
        """Returns None for a path that does not exist."""
        import store
        result = store.transcript_tool_result_len(
            "/nonexistent/path/transcript.jsonl", "toolu_1"
        )
        self.assertIsNone(result)


class TestStepLedger(unittest.TestCase):
    """Step ledger: durable per-step state for /nx-build resume."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["CLAUDE_PLUGIN_DATA"] = self._tmp

    def tearDown(self):
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)

    def test_record_and_get_roundtrip(self):
        import store
        store.record_step("s1", "h1", 0, "done", title="t", route="mechanical", tier_used="haiku")
        row = store.get_step("s1", "h1", 0)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["title"], "t")
        self.assertEqual(row["route"], "mechanical")
        self.assertEqual(row["tier_used"], "haiku")
        self.assertEqual(row["attempts"], 0)

    def test_get_absent_returns_none(self):
        import store
        self.assertIsNone(store.get_step("s1", "h1", 99))

    def test_partial_update_preserves_other_fields(self):
        """Re-recording with only status set must keep title/route/tier/diff."""
        import store
        store.record_step("s1", "h1", 1, "failed", title="wire", route="standard",
                          tier_used="sonnet", attempts=1)
        # Update only the diff; everything else preserved.
        store.record_step("s1", "h1", 1, "failed", last_diff="diff --git a/x b/x")
        row = store.get_step("s1", "h1", 1)
        self.assertEqual(row["title"], "wire")
        self.assertEqual(row["route"], "standard")
        self.assertEqual(row["tier_used"], "sonnet")
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["last_diff"], "diff --git a/x b/x")

    def test_status_transition_failed_to_done(self):
        import store
        store.record_step("s1", "h1", 2, "failed", title="x", route="standard")
        store.record_step("s1", "h1", 2, "done", tier_used="sonnet")
        row = store.get_step("s1", "h1", 2)
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["title"], "x")  # preserved

    def test_plan_hash_isolates_state(self):
        """A different plan_hash sees no rows — editing the plan discards stale state."""
        import store
        store.record_step("s1", "h1", 0, "done", title="a")
        self.assertEqual(len(store.step_ledger_rows("s1", "h1")), 1)
        self.assertEqual(store.step_ledger_rows("s1", "h2"), [])

    def test_list_ordered_by_index(self):
        import store
        store.record_step("s1", "h1", 2, "pending")
        store.record_step("s1", "h1", 0, "done")
        store.record_step("s1", "h1", 1, "failed")
        rows = store.step_ledger_rows("s1", "h1")
        self.assertEqual([r["step_index"] for r in rows], [0, 1, 2])

    def test_clear_scoped_to_plan(self):
        import store
        store.record_step("s1", "h1", 0, "done")
        store.record_step("s1", "h2", 0, "done")
        store.clear_step_ledger("s1", "h1")
        self.assertEqual(store.step_ledger_rows("s1", "h1"), [])
        self.assertEqual(len(store.step_ledger_rows("s1", "h2")), 1)

    def test_clear_all_for_session(self):
        import store
        store.record_step("s1", "h1", 0, "done")
        store.record_step("s1", "h2", 0, "done")
        store.clear_step_ledger("s1")
        self.assertEqual(store.step_ledger_rows("s1", "h1"), [])
        self.assertEqual(store.step_ledger_rows("s1", "h2"), [])


class TestPartitionSteps(unittest.TestCase):
    """Deterministic, order-preserving sub-batch partition (dispatch cap)."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["CLAUDE_PLUGIN_DATA"] = self._tmp

    def tearDown(self):
        os.environ.pop("CLAUDE_PLUGIN_DATA", None)

    def test_chunks_at_cap(self):
        import store
        self.assertEqual(
            store.partition_steps([0, 1, 2, 3, 4, 5, 6, 7], 6),
            [[0, 1, 2, 3, 4, 5], [6, 7]],
        )

    def test_fits_in_one_batch(self):
        import store
        self.assertEqual(store.partition_steps([0, 1], 6), [[0, 1]])

    def test_zero_or_negative_means_no_cap(self):
        import store
        self.assertEqual(store.partition_steps([0, 1, 2, 3], 0), [[0, 1, 2, 3]])
        self.assertEqual(store.partition_steps([0, 1, 2, 3], -1), [[0, 1, 2, 3]])

    def test_empty_input(self):
        import store
        self.assertEqual(store.partition_steps([], 6), [])

    def test_order_preserved(self):
        import store
        flat = [x for batch in store.partition_steps(list(range(10)), 3) for x in batch]
        self.assertEqual(flat, list(range(10)))


class TestPlanBatchesCLI(unittest.TestCase):
    """The plan-batches CLI the orchestrator calls to size dispatches."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._store = os.path.join(_SCRIPTS_DIR, "store.py")

    def _run(self, *args):
        env = os.environ.copy()
        env["CLAUDE_PLUGIN_DATA"] = self._tmp
        env["PYTHONPATH"] = _SCRIPTS_DIR + os.pathsep + env.get("PYTHONPATH", "")
        import subprocess
        r = subprocess.run([sys.executable, self._store, *args],
                           capture_output=True, env=env, timeout=15)
        return r.stdout.decode(), r.returncode

    def test_explicit_max(self):
        out, rc = self._run("plan-batches", "--indices", "0,1,2,3,4,5,6,7", "--max", "3")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), [[0, 1, 2], [3, 4, 5], [6, 7]])

    def test_default_max_from_config(self):
        with open(os.path.join(self._tmp, "config.json"), "w") as f:
            json.dump({"max_steps_per_dispatch": 2}, f)
        out, _ = self._run("plan-batches", "--indices", "0,1,2,3,4")
        self.assertEqual(json.loads(out), [[0, 1], [2, 3], [4]])

    def test_empty_indices(self):
        out, rc = self._run("plan-batches", "--indices", "")
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), [])


class TestStepLedgerCLI(unittest.TestCase):
    """The store.py CLI subcommands the orchestrator drives via bash."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self._store = os.path.join(_SCRIPTS_DIR, "store.py")

    def _run(self, *args, stdin=None):
        env = os.environ.copy()
        env["CLAUDE_PLUGIN_DATA"] = self._tmp
        env["PYTHONPATH"] = _SCRIPTS_DIR + os.pathsep + env.get("PYTHONPATH", "")
        import subprocess
        r = subprocess.run([sys.executable, self._store, *args],
                           input=(stdin.encode() if stdin else None),
                           capture_output=True, env=env, timeout=15)
        return r.stdout.decode(), r.returncode

    def test_plan_hash_deterministic(self):
        p = os.path.join(self._tmp, "plan.md")
        with open(p, "w") as f:
            f.write("step 1\nstep 2\n")
        out1, rc = self._run("plan-hash", "--file", p)
        self.assertEqual(rc, 0)
        out2, _ = self._run("plan-hash", "--file", p)
        self.assertEqual(out1, out2)
        self.assertEqual(len(out1.strip()), 64)  # sha256 hex

    def test_set_list_get_cycle(self):
        out, rc = self._run("step-set", "--session", "s1", "--plan-hash", "h1",
                            "--index", "0", "--status", "done", "--title", "x",
                            "--route", "mechanical", "--tier", "haiku")
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out)["ok"])
        out, _ = self._run("step-list", "--session", "s1", "--plan-hash", "h1")
        rows = json.loads(out)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "done")
        out, _ = self._run("step-get", "--session", "s1", "--plan-hash", "h1", "--index", "0")
        self.assertEqual(json.loads(out)["title"], "x")

    def test_get_absent_prints_null(self):
        out, rc = self._run("step-get", "--session", "s1", "--plan-hash", "h1", "--index", "0")
        self.assertEqual(rc, 0)
        self.assertIsNone(json.loads(out))

    def test_diff_via_stdin(self):
        self._run("step-set", "--session", "s1", "--plan-hash", "h1", "--index", "1",
                  "--status", "failed", "--title", "wire")
        self._run("step-set", "--session", "s1", "--plan-hash", "h1", "--index", "1",
                  "--status", "failed", "--diff-file", "-", stdin="diff line\nsecond\n")
        out, _ = self._run("step-get", "--session", "s1", "--plan-hash", "h1", "--index", "1")
        row = json.loads(out)
        self.assertEqual(row["last_diff"], "diff line\nsecond\n")
        self.assertEqual(row["title"], "wire")  # preserved across diff-only update

    def test_invalid_status_rejected(self):
        _, rc = self._run("step-set", "--session", "s1", "--plan-hash", "h1",
                          "--index", "0", "--status", "bogus")
        self.assertNotEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
