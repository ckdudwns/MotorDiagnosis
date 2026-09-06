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

    # v2 owns deviceConfigs, including monotonically increasing device versions.
    # The wire protocol and firmware NVS schema remain at v1.
    SCHEMA_VERSION = 2

    def __init__(self, database: str) -> None:
        self.database = database
        if database != ":memory:":
            Path(database).expanduser().resolve().parent.mkdir(
                parents=True, exist_ok=True
            )
        self._lock = threading.RLock()
        self._db = sqlite3.connect(database, timeout=5.0, check_same_thread=False)
        self._db.execute("PRAGMA busy_timeout=5000")
        if database != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS runtime_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                schema_version INTEGER NOT NULL,
                revision INTEGER NOT NULL,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """)
        self._db.commit()
        try:
            self._migrate()
        except BaseException:
            self._db.close()
            raise

    @staticmethod
    def _decode(payload: str, schema: int) -> dict[str, Any]:
        if schema not in (1, RuntimeStateStore.SCHEMA_VERSION):
            raise ValueError(
                f"Unsupported runtime state schema {schema}; expected {RuntimeStateStore.SCHEMA_VERSION}."
            )
        decoded = json.loads(payload)
        if not isinstance(decoded, dict):
            raise ValueError("Runtime state payload must be an object.")
        if schema == 1:
            # Also preserve the first PR25 release's configs stored under v1.
            decoded.setdefault("deviceConfigs", {})
        if not isinstance(decoded.get("deviceConfigs"), dict):
            raise ValueError("Persisted deviceConfigs must be an object.")
        return decoded

    def _migrate(self) -> None:
        # Take the writer lock before examining the version: simultaneous starts
        # migrate once, and the version update and old-writer guards are atomic.
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute(
                "SELECT schema_version, payload FROM runtime_state WHERE id=1"
            ).fetchone()
            if row is not None:
                schema = int(row[0])
                payload = self._decode(row[1], schema)
                if schema == 1:
                    self._db.execute(
                        """UPDATE runtime_state SET schema_version=?,
                            revision=revision+1, payload=? WHERE id=1""",
                        (
                            self.SCHEMA_VERSION,
                            json.dumps(
                                payload,
                                ensure_ascii=True,
                                allow_nan=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        ),
                    )
            # Old releases already reject schema 2 at load. These DB-enforced
            # guards also reject their UPSERTs if they loaded a v1 snapshot before
            # migration (and INSERT OR REPLACE attempts), even across connections.
            self._db.execute(
                """CREATE TRIGGER IF NOT EXISTS runtime_state_v2_insert_guard
                BEFORE INSERT ON runtime_state
                WHEN NEW.schema_version < 2 OR NEW.schema_version <
                    COALESCE((SELECT schema_version FROM runtime_state WHERE id=1), 2)
                BEGIN SELECT RAISE(ABORT, 'Runtime state schema downgrade refused'); END"""
            )
            self._db.execute(
                """CREATE TRIGGER IF NOT EXISTS runtime_state_v2_update_guard
                BEFORE UPDATE ON runtime_state
                WHEN NEW.schema_version < 2 OR NEW.schema_version < OLD.schema_version
                BEGIN SELECT RAISE(ABORT, 'Runtime state schema downgrade refused'); END"""
            )

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
        return self._decode(row[1], int(row[0]))

    def save(self, payload: dict[str, Any], updated_at: str) -> int:
        if not isinstance(payload, dict) or not isinstance(
            payload.get("deviceConfigs"), dict
        ):
            raise ValueError("Runtime state v2 requires a deviceConfigs object.")
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
