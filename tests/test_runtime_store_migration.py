"""Schema migration must protect configuration generations from old writers."""

import json
import runpy
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from motor_diagnosis.runtime_store import RuntimeStateStore

LegacyStore = runpy.run_path(
    str(Path(__file__).parent / "fixtures" / "runtime_store_v1.py")
)["RuntimeStateStore"]


class RuntimeStoreMigrationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = str(Path(temporary.name) / "runtime.sqlite3")

    def open_store(self, store_type=RuntimeStateStore):
        store = store_type(self.database)
        self.addCleanup(store.close)
        return store

    def row(self):
        connection = sqlite3.connect(self.database)
        try:
            return connection.execute("SELECT * FROM runtime_state").fetchone()
        finally:
            connection.close()

    def test_v1_upgrade_adds_empty_configs_without_losing_unknown_state(self):
        legacy = self.open_store(LegacyStore)
        payload = {"assets": [{"id": "A"}], "unknownExtension": {"keep": [1, 2]}}
        legacy.save(payload, "2026-09-06T00:00:00Z")
        current = self.open_store()
        self.assertEqual(current.load(), {**payload, "deviceConfigs": {}})
        row = self.row()
        self.assertEqual(row[1:3], (2, 2))
        self.assertEqual(row[4], "2026-09-06T00:00:00Z")
        self.open_store().load()
        self.assertEqual(self.row(), row)  # Reopening is not another migration.

    def test_existing_pr25_v1_configurations_survive_migration_and_old_writer(self):
        legacy = self.open_store(LegacyStore)
        payload = {"deviceConfigs": {"DEV-1": {"version": 9}}, "auditLogs": ["keep"]}
        legacy.save(payload, "before")
        stale_snapshot = legacy.load()
        current = self.open_store()
        self.assertEqual(current.load(), payload)
        before = self.row()
        with self.assertRaisesRegex(ValueError, "Unsupported runtime state schema 2"):
            self.open_store(LegacyStore).load()
        stale_snapshot.pop("deviceConfigs")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "schema downgrade"):
            legacy.save(stale_snapshot, "old process still running")
        self.assertEqual(self.row(), before)
        self.assertEqual(self.open_store().load(), payload)

    def test_new_empty_database_also_blocks_old_writer(self):
        current = self.open_store()
        self.assertIsNone(current.load())
        with self.assertRaisesRegex(sqlite3.IntegrityError, "schema downgrade"):
            self.open_store(LegacyStore).save({}, "old")
        self.assertIsNone(current.load())
        current.save({"deviceConfigs": {}}, "new")

    def test_future_schema_is_not_accepted_or_rewritten(self):
        legacy = self.open_store(LegacyStore)
        legacy.save({"future": "keep"}, "before")
        with legacy._db:
            legacy._db.execute("UPDATE runtime_state SET schema_version=3")
        before = self.row()
        with self.assertRaisesRegex(ValueError, "Unsupported runtime state schema 3"):
            RuntimeStateStore(self.database)
        self.assertEqual(self.row(), before)

    def test_live_writer_cannot_downgrade_a_subsequently_upgraded_schema(self):
        current = self.open_store()
        current.save({"deviceConfigs": {}}, "before")
        with current._db:
            current._db.execute("UPDATE runtime_state SET schema_version=3")
        before = self.row()
        with self.assertRaises((ValueError, sqlite3.IntegrityError)):
            current.save({"deviceConfigs": {}}, "stale")
        self.assertEqual(self.row(), before)

    def test_invalid_v1_payload_does_not_partially_migrate_or_add_guards(self):
        legacy = self.open_store(LegacyStore)
        for invalid in ("not-json", "[]", json.dumps({"deviceConfigs": None})):
            with self.subTest(invalid=invalid):
                legacy.save({}, "before")
                with legacy._db:
                    legacy._db.execute("UPDATE runtime_state SET payload=?", (invalid,))
                before = self.row()
                with self.assertRaises(ValueError):
                    RuntimeStateStore(self.database)
                self.assertEqual(self.row(), before)
                self.assertEqual(
                    legacy._db.execute(
                        "SELECT count(*) FROM sqlite_master WHERE type='trigger'"
                    ).fetchone()[0],
                    0,
                )

    def test_v2_missing_configs_is_corrupt_not_a_silent_version_reset(self):
        legacy = self.open_store(LegacyStore)
        legacy.save({}, "before")
        with legacy._db:
            legacy._db.execute("UPDATE runtime_state SET schema_version=2")
        before = self.row()
        with self.assertRaisesRegex(ValueError, "deviceConfigs"):
            RuntimeStateStore(self.database)
        self.assertEqual(self.row(), before)

    def test_migration_write_failure_rolls_back_schema_payload_and_guards(self):
        legacy = self.open_store(LegacyStore)
        legacy.save({}, "before")
        with legacy._db:
            legacy._db.execute(
                """CREATE TRIGGER fail_migration BEFORE UPDATE ON runtime_state
                BEGIN SELECT RAISE(ABORT, 'fixture migration failure'); END"""
            )
        before = self.row()
        with self.assertRaisesRegex(
            sqlite3.IntegrityError, "fixture migration failure"
        ):
            RuntimeStateStore(self.database)
        self.assertEqual(self.row(), before)
        with legacy._db:
            legacy._db.execute("DROP TRIGGER fail_migration")
        self.assertEqual(self.open_store().load(), {"deviceConfigs": {}})

    def test_two_initializers_migrate_once(self):
        legacy = self.open_store(LegacyStore)
        legacy.save({"keep": 1}, "before")

        def migrate(_):
            store = RuntimeStateStore(self.database)
            try:
                return store.load()
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(migrate, range(2)))
        self.assertEqual(results, [{"keep": 1, "deviceConfigs": {}}] * 2)
        self.assertEqual(self.row()[1:3], (2, 2))


if __name__ == "__main__":
    unittest.main()
