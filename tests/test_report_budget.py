"""
test_report_budget.py — B (wasted-context tracker + /nx-report) and
C (tiered budget alerts).
"""

import importlib
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

    def _write_config(self, cfg: dict):
        with open(os.path.join(self._tmp, "config.json"), "w") as fh:
            json.dump(cfg, fh)


class TestFileActivity(_Base):
    def test_read_then_edit_marks_useful(self):
        self.store.record_file_read("s", "./foo.py", 1200, partial=False)
        self.store.record_file_read("s", "foo.py", 800)  # same file, diff spelling
        rows = self.store.file_activity_rows("s")
        self.assertEqual(len(rows), 1)  # canonicalised to one row
        self.assertEqual(rows[0]["reads"], 2)
        self.assertEqual(rows[0]["tokens_read"], 2000)
        # Never edited yet → wasted.
        self.assertEqual(len(self.store.wasted_files("s")), 1)
        self.store.record_file_edit("s", "foo.py")
        self.assertEqual(self.store.wasted_files("s"), [])  # now useful

    def test_wasted_files_ranked_by_tokens(self):
        self.store.record_file_read("s", "/a.py", 500)
        self.store.record_file_read("s", "/b.py", 5000)
        wf = self.store.wasted_files("s", limit=5)
        self.assertEqual(os.path.basename(wf[0]["file_path"]), "b.py")

    def test_prune_includes_file_activity(self):
        conn = self.store.db()
        with conn:
            conn.execute(
                "INSERT INTO file_activity(session_id,file_path,reads,partial_reads,edits,tokens_read,ts) "
                "VALUES (?,?,?,?,?,?,?)",
                ("s", "/old.py", 1, 0, 0, 100, __import__("time").time() - 100 * 86400),
            )
        conn.close()
        removed = self.store.prune(7)
        self.assertGreaterEqual(removed, 1)
        self.assertEqual(self.store.file_activity_rows("s"), [])


class TestReport(_Base):
    def test_waste_section_grades_and_suggests(self):
        import report
        importlib.reload(report)
        self.store.record_file_read("s", "/wasted_big.py", 8000)
        self.store.record_file_read("s", "/edited.py", 1000)
        self.store.record_file_edit("s", "/edited.py")
        section = report.build_waste_section(self.store.file_activity_rows("s"))
        self.assertIn("Efficiency grade", section)
        self.assertIn("WASTED", section)
        self.assertIn("wasted_big.py", section)
        self.assertIn("drop", section)

    def test_digest_runs_clean_with_no_data(self):
        out, rc = _run("report.py", {}, self._tmp)
        self.assertEqual(rc, 0)
        self.assertIn("Session report", out)


class TestSavingsSplit(_Base):
    """D1: savings split into realized / bounded / theoretical buckets."""

    def test_savings_by_source_aggregates(self):
        self.store.record_saving("s", "predup", 1000, 100)
        self.store.record_saving("s", "predup", 500, 50)
        self.store.record_saving("s", "grep_narrow", 0, 0)
        agg = self.store.savings_by_source("s")
        self.assertEqual(agg["predup"]["count"], 2)
        self.assertEqual(agg["predup"]["saved_tok"], 1500)
        self.assertEqual(agg["predup"]["effective_tok"], 150)
        self.assertEqual(agg["grep_narrow"]["count"], 1)
        self.assertEqual(agg["grep_narrow"]["effective_tok"], 0)

    def test_realized_only_counts_predup_tokens(self):
        import report
        importlib.reload(report)
        # predup is realized (measured); grep_narrow/read_guard are bounded
        # (count only); dedup is theoretical (PostToolUse, inert).
        self.store.record_saving("s", "predup", 2000, 200)
        self.store.record_saving("s", "grep_narrow", 0, 0)
        self.store.record_saving("s", "read_guard", 0, 0)
        section = report.build_savings_section(self.store.savings_by_source("s"))
        self.assertIn("Realized", section)
        self.assertIn("Bounded", section)
        self.assertIn("Theoretical", section)
        # The realized total reflects predup's effective tokens (~200), not the
        # bounded interventions which carry 0 tokens.
        self.assertIn("TOTAL realized", section)
        self.assertIn("repeat tool calls denied", section)
        self.assertIn("broad searches bounded", section)

    def test_theoretical_empty_states_inert(self):
        import report
        importlib.reload(report)
        section = report.build_savings_section({})
        self.assertIn("contributes 0 today", section)

    def test_dedup_classified_theoretical(self):
        import report
        importlib.reload(report)
        self.store.record_saving("s", "dedup", 5000, 500)
        section = report.build_savings_section(self.store.savings_by_source("s"))
        self.assertIn("would save once the field is honored", section)
        # With only dedup recorded, the realized bucket stays empty — its tokens
        # are never folded into the realized headline.
        realized_idx = section.index("Realized")
        bounded_idx = section.index("Bounded")
        realized_block = section[realized_idx:bounded_idx]
        self.assertIn("none recorded", realized_block)
        self.assertNotIn("5.0k", realized_block)


class TestDedupActivityTracking(_Base):
    def test_read_and_edit_recorded(self):
        _run("dedup.py", {
            "session_id": "d1", "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/x.py"},
            "tool_response": "line\n" * 40,
        }, self._tmp)
        rows = self.store.file_activity_rows("d1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reads"], 1)
        self.assertGreater(rows[0]["tokens_read"], 0)
        self.assertEqual(rows[0]["edits"], 0)

        out, rc = _run("dedup.py", {
            "session_id": "d1", "tool_name": "Edit",
            "tool_input": {"file_path": "/tmp/x.py"},
            "tool_response": "ok",
        }, self._tmp)
        self.assertEqual(out, "{}")  # edits never dedup
        rows = self.store.file_activity_rows("d1")
        self.assertEqual(rows[0]["edits"], 1)
        self.assertEqual(self.store.wasted_files("d1"), [])


class TestBudgetAlert(_Base):
    def test_tier_fires_with_systemMessage(self):
        self._write_config({"budget_usd": 1.0, "intent_guard_enabled": False})
        self.store.upsert_session_cost("bsess", "Sonnet", 0.60, 1000, 200)
        out, rc = _run("context_watch.py",
                       {"session_id": "bsess", "prompt": "do the thing", "transcript_path": ""},
                       self._tmp)
        self.assertEqual(rc, 0)
        msg = json.loads(out).get("systemMessage", "")
        self.assertIn("Budget", msg)
        self.assertIn("60%", msg)

    def test_high_tier_names_wasted_files_and_compact(self):
        self._write_config({"budget_usd": 1.0, "intent_guard_enabled": False})
        self.store.upsert_session_cost("bs2", "Sonnet", 0.95, 1000, 200)
        self.store.record_file_read("bs2", "/huge_unedited.py", 9000)
        out, _ = _run("context_watch.py",
                      {"session_id": "bs2", "prompt": "go", "transcript_path": ""},
                      self._tmp)
        msg = json.loads(out).get("systemMessage", "")
        self.assertIn("huge_unedited.py", msg)
        self.assertIn("/compact", msg)

    def test_disabled_by_default(self):
        self.store.upsert_session_cost("bs3", "Sonnet", 5.0, 1000, 200)
        out, _ = _run("context_watch.py",
                      {"session_id": "bs3", "prompt": "go", "transcript_path": ""},
                      self._tmp)
        self.assertNotIn("Budget", json.loads(out).get("systemMessage", ""))


if __name__ == "__main__":
    unittest.main()
