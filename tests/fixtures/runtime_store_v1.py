"""Frozen pre-PR25 writer from 79b2722, for rollback compatibility tests.

RuntimeStateStore below is unchanged from that revision (DurableStateLock
omitted). Do not update its validation or SQL to match the current writer.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any


class RuntimeStateStore:
    """Versioned, atomic JSON checkpoints stored in SQLite."""

    SCHEMA_VERSION = 1

    def __init__(self, database: str) -> None:
        self.database = database
        if database != ":memory:":
            Path(database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(database, timeout=5.0, check_same_thread=False)
        self._db.execute("PRAGMA busy_timeout=5000")
        if database != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                schema_version INTEGER NOT NULL,
                revision INTEGER NOT NULL,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        self._db.commit()

    def load(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute(
                "SELECT schema_version, payload FROM runtime_state WHERE id=1"
            ).fetchone()
        if row is None:
            return None
        if int(row[0]) != self.SCHEMA_VERSION:
            raise ValueError(
                f"Unsupported runtime state schema {row[0]}; expected {self.SCHEMA_VERSION}."
            )
        payload = json.loads(row[1])
        if not isinstance(payload, dict):
            raise ValueError("Runtime state payload must be an object.")
        return payload

    def save(self, payload: dict[str, Any], updated_at: str) -> int:
        encoded = json.dumps(
            payload,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._lock, self._db:
            self._db.execute(
                """
                INSERT INTO runtime_state(id, schema_version, revision, payload, updated_at)
                VALUES (1, ?, 1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    schema_version=excluded.schema_version,
                    revision=runtime_state.revision + 1,
                    payload=excluded.payload,
                    updated_at=excluded.updated_at
                """,
                (self.SCHEMA_VERSION, encoded, updated_at),
            )
            row = self._db.execute(
                "SELECT revision FROM runtime_state WHERE id=1"
            ).fetchone()
            revision = int(row[0])
        return revision

    def close(self) -> None:
        with self._lock:
            self._db.close()
