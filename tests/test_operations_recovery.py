"""Accelerated persistence/replay exercise. Not a wall-clock field soak."""
from contextlib import closing, ExitStack
from pathlib import Path
import tempfile
from unittest import mock

from motor_diagnosis import data, operations
from motor_diagnosis.raw_vibration import RawVibrationStore
from tests.test_measured_rpm import RpmSetup
from tests.test_rf66 import FakeRF
from tests import test_raw_vibration as raw_tests


class RecoveryExerciseTest(RpmSetup):
    window = raw_tests.RawWindowTest.window

    def test_256_windows_ack_loss_restart_gap_and_restore(self):
        with ExitStack() as stack:
            directory = stack.enter_context(tempfile.TemporaryDirectory())
            root = Path(directory)
            path = root / "raw.sqlite3"
            state_path = root / "runtime.sqlite3"
            data.configure_runtime_state(str(state_path))
            stack.callback(data.close_runtime_state)
            model = FakeRF()
            store = RawVibrationStore(path, model=model, event_mode="events")
            now = [self.started.timestamp()]
            store.events.clock = lambda: now[0]
            stack.callback(lambda: store.close())
            accepted = 0
            for index in range(256):
                if index == 91:  # transport gap, not zero-filled input
                    continue
                now[0] = self.started.timestamp() + index*.64 + .64
                model.score = .8 if index % 32 < 16 else 0
                window = self.window(index)
                if index == 90:
                    window.update(quality="sensor_unavailable", sampleCount=0, samples=None)
                if index >= 128:
                    window.update(bootId="d"*32, windowIndex=index-128, startUptimeUs=(index-128)*640000)
                batch = {"windows": [window]}
                _, status = store.ingest(self.principal, raw_tests.DEVICE, batch)
                self.assertEqual(status, 202)
                accepted += 1
                if index % 9 == 0:  # lost ACK retries are idempotent
                    self.assertEqual(store.ingest(self.principal, raw_tests.DEVICE, batch)[1], 200)
                if index in (64, 128, 192):  # restart after ACK, before processing
                    store.close()
                    store = RawVibrationStore(path, model=model, event_mode="events")
                    store.events.clock = lambda: now[0]
                self.assertTrue(store.tick())
                self.assertFalse(store.tick())
                store.events.dispatch()
            self.assertEqual(store.db.execute("SELECT count(*) FROM vibration_windows").fetchone()[0], accepted)
            self.assertEqual(store.db.execute("SELECT count(*) FROM vibration_windows WHERE status='queued'").fetchone()[0], 0)
            incidents = [e for e in data.EVENTS if e.get("source") == "rf66"]
            self.assertEqual(len(incidents), 8)
            self.assertTrue(all(e["status"] == "closed" for e in incidents))
            self.assertEqual(len({e["id"] for e in incidents}), 8)
            # Service-stopped backup of the actual raw/runtime stores plus the
            # application's remaining independent SQLite files.
            store.close()
            data.close_runtime_state()
            env = {"STATE_DB_PATH": str(state_path), "RAW_VIBRATION_WINDOW_DB_PATH": str(path),
                   "AUTH_USERS_FILE": str(root / "auth.json")}
            (root / "auth.json").write_text('{"fixture":true}')
            import sqlite3
            for key, default in operations.DB_DEFAULTS.items():
                if key in env or key == "SHADOW_MODEL_DB_PATH": continue
                db_path = root / default
                db_path.parent.mkdir(exist_ok=True)
                with closing(sqlite3.connect(db_path)) as db:
                    db.execute("CREATE TABLE fixture(value INT)")
                    db.commit()
            with mock.patch.object(operations, "run", return_value="fixture-commit"):
                folder = operations.make_backup(root, env, {}, root / "backups", stopped=lambda: True)
            restored = root / "restore"
            operations.restore_drill(folder, restored)
            data.configure_runtime_state(str(restored / "STATE_DB_PATH.sqlite3"))
            store = RawVibrationStore(restored / "RAW_VIBRATION_WINDOW_DB_PATH.sqlite3", model=model, event_mode="events")
            self.assertEqual(store.ingest(self.principal, raw_tests.DEVICE, batch)[1], 200)
            self.assertFalse(store.tick())
            store.events.dispatch()
            self.assertEqual(len([e for e in data.EVENTS if e.get("source") == "rf66"]), 8)
