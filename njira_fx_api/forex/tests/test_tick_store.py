"""Tests for the bounded SQLite tick store."""
from __future__ import annotations

import os
import tempfile
import unittest


class TickStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        from forex.analysis.tick_store import TickStore

        self.TickStore = TickStore
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "ticks.sqlite3")

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    @staticmethod
    def _row(symbol: str, ts_ms: int) -> tuple:
        return (symbol, 2000.0, 2000.5, 0.5, 2, ts_ms)

    def test_from_env_disabled_when_unset(self) -> None:
        os.environ.pop("NJIRA_TICK_STORE", None)
        self.assertIsNone(self.TickStore.from_env())

    def test_from_env_reads_path_and_cap(self) -> None:
        os.environ["NJIRA_TICK_STORE"] = self.path
        os.environ["NJIRA_TICK_MAX_ROWS"] = "5000"
        try:
            store = self.TickStore.from_env()
            self.assertIsNotNone(store)
            self.assertEqual(store.path, self.path)
            self.assertEqual(store.max_rows, 5000)
        finally:
            os.environ.pop("NJIRA_TICK_STORE", None)
            os.environ.pop("NJIRA_TICK_MAX_ROWS", None)

    def test_write_then_counts_survive_reopen(self) -> None:
        store = self.TickStore(self.path)
        store.write([self._row("XAUUSD", 1), self._row("XAUUSD", 2)])
        store.write([self._row("XAUUSD", 3)])
        store.close()

        reopened = self.TickStore(self.path)
        reopened.write([self._row("XAUUSD", 4)])
        self.assertEqual(reopened.stats()["rows"], 4)
        reopened.close()

    def test_cap_trims_oldest_rows(self) -> None:
        store = self.TickStore(self.path, max_rows=1000)
        store.write([self._row("XAUUSD", ts) for ts in range(1500)])

        stats = store.stats()
        self.assertLessEqual(stats["rows"], stats["max_rows"])
        self.assertGreater(stats["rows"], 0)

        oldest = store._connect().execute("SELECT MIN(ts_ms) FROM ticks").fetchone()[0]
        self.assertGreater(oldest, 0, "oldest rows must have been trimmed")
        store.close()

    def test_write_swallows_storage_errors(self) -> None:
        store = self.TickStore(os.path.join(self.tmpdir, "no", "such", "dir", "t.sqlite3"))
        store.write([object()])
        self.assertEqual(store.stats()["rows"], 0)


if __name__ == "__main__":
    unittest.main()
