"""Bounded SQLite sink for live ticks.

ClickHouse is disabled on this deployment, so ticks drained from the
PersistenceBuffer have nowhere durable to go. This module gives the tick
stream a small on-disk home: one SQLite file, appended in batches, with the
oldest rows trimmed once a row cap is hit so the file cannot grow without
bound on a nearly full disk.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from typing import Optional

logger = logging.getLogger("forex.analysis.tick_store")

DEFAULT_MAX_ROWS = 1_000_000
MIN_MAX_ROWS = 1_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS ticks (
    ts_ms  INTEGER NOT NULL,
    symbol TEXT    NOT NULL,
    bid    REAL,
    ask    REAL,
    spread REAL,
    digits INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ticks_ts ON ticks (ts_ms);
"""


class TickStore:
    """Append-only tick log in SQLite with a rolling row cap."""

    def __init__(self, path: str, max_rows: int = DEFAULT_MAX_ROWS) -> None:
        self.path = path
        self.max_rows = max(MIN_MAX_ROWS, int(max_rows))
        self._conn: Optional[sqlite3.Connection] = None
        self._rows = 0
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> Optional["TickStore"]:
        """Build from the environment, or None when the store is not configured.

        NJIRA_TICK_STORE      path to the SQLite file; unset disables the store
        NJIRA_TICK_MAX_ROWS   row cap, default 1_000_000 (about 80 MB)
        """
        path = os.environ.get("NJIRA_TICK_STORE", "").strip()
        if not path:
            return None
        try:
            max_rows = int(os.environ.get("NJIRA_TICK_MAX_ROWS", DEFAULT_MAX_ROWS))
        except ValueError:
            max_rows = DEFAULT_MAX_ROWS
        return cls(path, max_rows)

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.executescript(_SCHEMA)
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.commit()
            self._rows = conn.execute("SELECT COUNT(*) FROM ticks").fetchone()[0]
            self._conn = conn
            logger.info("Tick store open at %s (%d rows)", self.path, self._rows)
        return self._conn

    def write(self, rows: list) -> None:
        """Persist one flushed batch of (symbol, bid, ask, spread, digits, ts_ms)."""
        if not rows:
            return
        try:
            with self._lock:
                conn = self._connect()
                conn.executemany(
                    "INSERT INTO ticks (symbol, bid, ask, spread, digits, ts_ms) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    rows,
                )
                self._rows += len(rows)
                if self._rows > self.max_rows:
                    self._trim(conn)
                conn.commit()
        except Exception as exc:  # noqa: BLE001 - tick stream must survive
            logger.warning("Tick store write failed: %s", exc)

    def _trim(self, conn: sqlite3.Connection) -> None:
        """Drop the oldest rows.

        Deleted pages are reused by later inserts, so the file plateaus near
        the cap instead of shrinking; that is the bound we want.
        """
        margin = max(1, self.max_rows // 10)
        excess = min(self._rows - self.max_rows + margin, self._rows)
        cursor = conn.execute(
            "DELETE FROM ticks WHERE rowid IN "
            "(SELECT rowid FROM ticks ORDER BY ts_ms, rowid LIMIT ?)",
            (excess,),
        )
        self._rows -= cursor.rowcount
        logger.info("Tick store trimmed to %d rows", self._rows)

    def stats(self) -> dict:
        with self._lock:
            return {"path": self.path, "rows": self._rows, "max_rows": self.max_rows}

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
