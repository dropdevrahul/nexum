"""
test_metering.py — cache-aware savings + session-cost capture in store.py.

Covers:
- record_saving stores both raw and effective tokens; session_savings sums the
  effective (cache-adjusted) value.
- a legacy-style row (effective omitted) falls back to raw under session_savings.
- upsert_session_cost snapshots cumulative cost (UPSERT, not append).
- session_cost_rows reads back the snapshot and is session-isolated.
"""

import importlib
import os
import sys
import tempfile
import unittest

_SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


class TestMetering(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["CLAUDE_PLUGIN_DATA"] = self._tmp
        import store as _store
        importlib.reload(_store)
        import store
        self.store = store

    def test_effective_savings_summed(self):
        # dedup-style row: raw 1000, effective 100 (cache-weighted)
        self.store.record_saving("s1", "dedup", 1000, 100)
        # truncate-style row: full weight (effective defaults to raw)
        self.store.record_saving("s1", "truncate", 50)
        self.assertEqual(self.store.session_savings("s1"), 150)

    def test_effective_defaults_to_raw(self):
        self.store.record_saving("s1", "dedup", 80)
        self.assertEqual(self.store.session_savings("s1"), 80)

    def test_session_cost_upsert_is_not_append(self):
        self.store.upsert_session_cost("s1", "Sonnet", 0.10, 1000, 200)
        self.store.upsert_session_cost("s1", "Sonnet", 0.25, 3000, 500,
                                       cache_read_tok=9000)
        rows = self.store.session_cost_rows("s1")
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["cost_usd"], 0.25)
        self.assertEqual(rows[0]["cache_read_tok"], 9000)

    def test_session_cost_isolated(self):
        self.store.upsert_session_cost("s1", "Opus", 1.0)
        self.assertEqual(self.store.session_cost_rows("other"), [])
        self.assertEqual(len(self.store.session_cost_rows()), 1)


if __name__ == "__main__":
    unittest.main()
