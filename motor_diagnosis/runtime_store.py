"""Durable SQLite storage for the backend's general runtime state.

The alert outbox and MQTT retry queue own separate databases.  This module is
only for the application state that historically lived in ``data.py`` module
globals.  A single JSON document keeps cross-collection updates atomic while
the backend remains a compact proof-of-concept service.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable


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


class DurableStateLock:
    """Re-entrant state lock with an optional outer-transaction commit hook."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._local = threading.local()
        self._commit: Callable[[], None] | None = None
        self._snapshot: Callable[[], Any] | None = None
        self._restore: Callable[[Any], None] | None = None
        self._commit_failed: Callable[[Exception], None] | None = None

    def set_transaction_hooks(
        self,
        snapshot: Callable[[], Any] | None = None,
        restore: Callable[[Any], None] | None = None,
        commit_failed: Callable[[Exception], None] | None = None,
    ) -> None:
        with self._lock:
            self._snapshot = snapshot
            self._restore = restore
            self._commit_failed = commit_failed

    def set_commit_hook(self, callback: Callable[[], None] | None) -> None:
        with self._lock:
            self._commit = callback

    def _is_owned(self) -> bool:
        """Expose the CPython RLock probe used by concurrency regression tests."""

        probe = getattr(self._lock, "_is_owned", None)
        return bool(probe and probe())

    def __enter__(self) -> DurableStateLock:
        self._lock.acquire()
        depth = int(getattr(self._local, "depth", 0))
        try:
            if depth == 0:
                self._local.before = self._snapshot() if self._snapshot else None
            self._local.depth = depth + 1
        except BaseException:
            self._lock.release()
            raise
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        depth = int(getattr(self._local, "depth", 1)) - 1
        try:
            if depth == 0:
                before = self._local.before
                if exc_type is not None:
                    if self._restore is not None and before is not None:
                        self._restore(before)
                elif self._commit is not None:
                    try:
                        # Reads do not advance the SQLite revision. Nested state
                        # operations share the outer transaction and its snapshot.
                        if self._snapshot is None or self._snapshot() != before:
                            self._commit()
                    except Exception as error:
                        if self._restore is not None and before is not None:
                            self._restore(before)
                        if self._commit_failed is not None:
                            self._commit_failed(error)
                        raise
        finally:
            self._local.depth = depth
            if depth == 0:
                self._local.before = None
            self._lock.release()
        return False
