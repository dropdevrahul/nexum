"""
test_gaps.py — covers the v0.4.x gap-closing work:

#1 clear_tool_calls + precompact/session_reset invalidation (predup safety)
#2 canonicalised tool_call_sig (predup recall)
#4 transcript_usage_totals + subagent_usage hook (real per-tier usage)
#5 Fable pricing in cost_report
#6 prune / maybe_prune retention
"""

import importlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

_SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


def _run(script, payload, data_dir):
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_DATA"] = data_dir
    env["PYTHONPATH"] = _SCRIPTS_DIR + os.pathsep + env.get("PYTHONPATH", "")
    r = subprocess.run(
        [sys.executable, os.path.join(_SCRIPTS_DIR, script)],
        input=json.dumps(payload).encode(),
        capture_output=True, env=env, timeout=15,
    )
    return r.stdout.decode().strip(), r.returncode


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["CLAUDE_PLUGIN_DATA"] = self._tmp
        import store as _store
        importlib.reload(_store)
        import store
        self.store = store


class TestCanonicalSig(_Base):
    def test_path_spelling_collapses(self):
        a = self.store.tool_call_sig("Read", {"file_path": "./foo.py"})
        b = self.store.tool_call_sig("Read", {"file_path": "foo.py"})
        c = self.store.tool_call_sig("Read", {"file_path": os.path.realpath("foo.py")})
        self.assertEqual(a, b)
        self.assertEqual(a, c)

    def test_different_range_stays_distinct(self):
        a = self.store.tool_call_sig("Read", {"file_path": "foo.py"})
        b = self.store.tool_call_sig("Read", {"file_path": "foo.py", "offset": 100})
        self.assertNotEqual(a, b)


class TestClearToolCalls(_Base):
    def test_clear_removes_session_rows(self):
        self.store.record_tool_call("s", "sig1", "Read", 100)
        self.assertIsNotNone(self.store.seen_tool_call("s", "sig1"))
        removed = self.store.clear_tool_calls("s")
        self.assertEqual(removed, 1)
        self.assertIsNone(self.store.seen_tool_call("s", "sig1"))


class TestPrune(_Base):
    def _insert_old(self, sig, age_days):
        conn = self.store.db()
        with conn:
            conn.execute(
                "INSERT INTO tool_calls(session_id,input_sig,tool_name,token_count,file_path,mtime,ts) "
                "VALUES (?,?,?,?,?,?,?)",
                ("s", sig, "Read", 100, None, None, time.time() - age_days * 86400),
            )
        conn.close()

    def test_prune_removes_aged_keeps_fresh(self):
        self._insert_old("old", 100)
        self.store.record_tool_call("s", "fresh", "Read", 100)
        removed = self.store.prune(7)
        self.assertGreaterEqual(removed, 1)
        self.assertIsNone(self.store.seen_tool_call("s", "old"))
        self.assertIsNotNone(self.store.seen_tool_call("s", "fresh"))

    def test_prune_zero_is_noop(self):
        self._insert_old("old", 100)
        self.assertEqual(self.store.prune(0), 0)
        self.assertIsNotNone(self.store.seen_tool_call("s", "old"))

    def test_maybe_prune_throttles(self):
        self._insert_old("old", 100)
        first = self.store.maybe_prune()
        self.assertGreaterEqual(first, 1)
        self._insert_old("old2", 100)
        # Within the same day → throttled, does nothing.
        self.assertEqual(self.store.maybe_prune(), 0)


class TestTranscriptUsage(_Base):
    def test_sums_usage_blocks(self):
        tp = os.path.join(self._tmp, "t.jsonl")
        with open(tp, "w") as fh:
            fh.write(json.dumps({"message": {"usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 50}}}) + "\n")
            fh.write(json.dumps({"message": {"role": "user", "content": "hi"}}) + "\n")
            fh.write(json.dumps({"message": {"usage": {"input_tokens": 200, "output_tokens": 30}}}) + "\n")
        t = self.store.transcript_usage_totals(tp)
        self.assertEqual(t["input_tok"], 300)
        self.assertEqual(t["output_tok"], 50)
        self.assertEqual(t["cache_read_tok"], 50)

    def test_missing_file_zeros(self):
        t = self.store.transcript_usage_totals("/nonexistent/x.jsonl")
        self.assertEqual(t, {"input_tok": 0, "output_tok": 0, "cache_read_tok": 0})


class TestFablePricing(_Base):
    def test_pricing_has_fable(self):
        self.assertIn("fable", self.store.DEFAULT_PRICING)
        self.assertEqual(self.store.DEFAULT_PRICING["fable"], (10.0, 50.0))

    def test_cost_report_maps_fable(self):
        import cost_report
        importlib.reload(cost_report)
        self.assertEqual(cost_report._model_key("claude-fable-5"), "fable")
        self.assertEqual(cost_report._model_key("anthropic.claude-opus-4-8"), "opus")


class TestPrecompactHook(_Base):
    def test_clears_tool_calls(self):
        self.store.record_tool_call("psess", "sig", "Read", 100)
        out, rc = _run("precompact.py", {
            "session_id": "psess", "cwd": self._tmp,
            "transcript_path": "", "trigger": "auto",
        }, self._tmp)
        self.assertEqual(rc, 0)
        self.assertEqual(out, "{}")  # never blocks compaction
        self.assertIsNone(self.store.seen_tool_call("psess", "sig"))


class TestSessionResetHook(_Base):
    def test_clear_source_invalidates(self):
        self.store.record_tool_call("rsess", "sig", "Read", 100)
        _run("session_reset.py", {"session_id": "rsess", "source": "clear"}, self._tmp)
        self.assertIsNone(self.store.seen_tool_call("rsess", "sig"))

    def test_startup_source_keeps(self):
        self.store.record_tool_call("rsess2", "sig", "Read", 100)
        _run("session_reset.py", {"session_id": "rsess2", "source": "startup"}, self._tmp)
        self.assertIsNotNone(self.store.seen_tool_call("rsess2", "sig"))


class TestSubagentUsageHook(_Base):
    def test_records_tier_usage(self):
        tp = os.path.join(self._tmp, "sub.jsonl")
        with open(tp, "w") as fh:
            fh.write(json.dumps({"message": {"usage": {"input_tokens": 500, "output_tokens": 80}}}) + "\n")
        out, rc = _run("subagent_usage.py", {
            "session_id": "usess", "agent_type": "nexum-impl-haiku",
            "transcript_path": tp,
        }, self._tmp)
        self.assertEqual(rc, 0)
        rows = self.store.usage_rows("usess")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["model"], "haiku")
        self.assertEqual(rows[0]["input_tok"], 500)
        self.assertEqual(rows[0]["output_tok"], 80)

    def test_ignores_non_nexum_agent(self):
        _run("subagent_usage.py", {
            "session_id": "usess2", "agent_type": "Explore", "transcript_path": "",
        }, self._tmp)
        self.assertEqual(self.store.usage_rows("usess2"), [])


if __name__ == "__main__":
    unittest.main()
