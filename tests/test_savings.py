"""
test_savings.py — stdlib unittest tests for savings persistence in scripts/store.py

Covers the contract from the nexum plan step:
- session_savings returns 0 when no rows exist
- record_saving writes rows that are accumulated by session_savings
- session_savings is isolated per session_id
"""

import os
import sys
import tempfile
import unittest

_SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


class TestSavings(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        os.environ["CLAUDE_PLUGIN_DATA"] = self._tmp
        # Re-import store so it picks up the fresh CLAUDE_PLUGIN_DATA env var
        import importlib
        import store as _store
        importlib.reload(_store)
        import store
        self.store = store

    def test_initial_savings_zero(self):
        self.assertEqual(self.store.session_savings("s1"), 0)

    def test_record_and_sum(self):
        self.store.record_saving("s1", "dedup", 100)
        self.store.record_saving("s1", "truncate", 50)
        self.assertEqual(self.store.session_savings("s1"), 150)

    def test_savings_isolated_by_session(self):
        self.store.record_saving("s1", "dedup", 100)
        self.store.record_saving("s1", "truncate", 50)
        self.assertEqual(self.store.session_savings("other"), 0)


if __name__ == "__main__":
    unittest.main()
