"""
dedup_store.py — Persistent deduplication store backed by SQLite.

Design decisions:
- SQLite is chosen for its ACID guarantees, zero external dependencies,
  and file-level portability (survives container restarts when mounted).
- Primary key (topic, event_id) enforces uniqueness at the DB level,
  eliminating TOCTOU race conditions.
- All writes use INSERT OR IGNORE for atomic idempotency — no separate
  SELECT-then-INSERT that could race under concurrent consumers.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS processed_events (
    topic        TEXT    NOT NULL,
    event_id     TEXT    NOT NULL,
    timestamp    TEXT    NOT NULL,
    source       TEXT    NOT NULL,
    payload      TEXT    NOT NULL DEFAULT '{}',
    processed_at REAL    NOT NULL,
    PRIMARY KEY (topic, event_id)
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_topic ON processed_events (topic);
"""

_CREATE_STATS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS system_stats (
    stat_key TEXT PRIMARY KEY,
    stat_value INTEGER NOT NULL DEFAULT 0
);
"""

_INIT_STATS_SQL = """
INSERT OR IGNORE INTO system_stats (stat_key, stat_value) VALUES 
('received', 0),
('duplicate_dropped', 0);
"""


class DedupStore:
    """
    Thread-safe, persistent deduplication store.

    Each SQLite connection is created per-call (connect-on-use) to remain
    compatible with asyncio without requiring an async driver.  SQLite's
    WAL mode is enabled to allow concurrent reads alongside writes.
    """

    def __init__(self, db_path: str = "/app/data/dedup.db") -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        logger.info("DedupStore initialised at %s", db_path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(_CREATE_TABLE_SQL)
            conn.execute(_CREATE_INDEX_SQL)
            conn.execute(_CREATE_STATS_TABLE_SQL)
            conn.executescript(_INIT_STATS_SQL)
            conn.commit()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def mark_processed(self, event) -> bool:  # noqa: ANN001
        """
        Atomically insert the event.

        Returns:
            True  — event was new and has been stored.
            False — event was a duplicate; nothing written.
        """
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO processed_events
                        (topic, event_id, timestamp, source, payload, processed_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.topic,
                        event.event_id,
                        event.timestamp,
                        event.source,
                        json.dumps(event.payload),
                        time.time(),
                    ),
                )
                conn.commit()
                return cursor.rowcount == 1
        except sqlite3.OperationalError as exc:
            logger.error("DedupStore write error: %s", exc)
            raise

    def is_duplicate(self, topic: str, event_id: str) -> bool:
        """Read-only check without writing — useful for unit tests."""
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT 1 FROM processed_events WHERE topic=? AND event_id=?",
                (topic, event_id),
            )
            return cur.fetchone() is not None

    def get_events(self, topic: Optional[str] = None) -> list[dict]:
        """Return all stored (unique) events, optionally filtered by topic."""
        with self._connect() as conn:
            if topic:
                cur = conn.execute(
                    "SELECT topic, event_id, timestamp, source, payload "
                    "FROM processed_events WHERE topic=? ORDER BY processed_at ASC",
                    (topic,),
                )
            else:
                cur = conn.execute(
                    "SELECT topic, event_id, timestamp, source, payload "
                    "FROM processed_events ORDER BY processed_at ASC"
                )
            return [
                {
                    "topic": row[0],
                    "event_id": row[1],
                    "timestamp": row[2],
                    "source": row[3],
                    "payload": json.loads(row[4]),
                }
                for row in cur.fetchall()
            ]

    def get_topics(self) -> list[str]:
        """Return list of distinct topics seen so far."""
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT DISTINCT topic FROM processed_events ORDER BY topic"
            )
            return [row[0] for row in cur.fetchall()]

    def count_unique(self) -> int:
        """Total unique events stored."""
        with self._connect() as conn:
            cur = conn.execute("SELECT COUNT(*) FROM processed_events")
            return cur.fetchone()[0]

    def increment_stat(self, stat_key: str, amount: int = 1) -> None:
        """Increment a system stat counter atomically."""
        with self._connect() as conn:
            conn.execute(
                "UPDATE system_stats SET stat_value = stat_value + ? WHERE stat_key = ?",
                (amount, stat_key),
            )
            conn.commit()

    def get_stat(self, stat_key: str) -> int:
        """Retrieve a system stat counter."""
        with self._connect() as conn:
            cur = conn.execute("SELECT stat_value FROM system_stats WHERE stat_key = ?", (stat_key,))
            row = cur.fetchone()
            return row[0] if row else 0
