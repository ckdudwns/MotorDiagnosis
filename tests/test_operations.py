"""Maintenance failure/recovery contracts using disposable SQLite stores."""
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

from motor_diagnosis import operations as ops


class OperationsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        self.root = Path(self.temp.name) / "backups"
        self.env = {"AUTH_USERS_FILE": str(self.project / "auth.json"), "RF66_EVENT_MODE": "events"}
        (self.project / "auth.json").write_text('{"secret":"not-for-report"}')
        for key, default in ops.DB_DEFAULTS.items():
            if key == "SHADOW_MODEL_DB_PATH":
                continue
            path = self.project / default
            path.parent.mkdir(exist_ok=True)
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE evidence(value TEXT)")
                db.execute("INSERT INTO evidence VALUES('persist me')")
                db.commit()
        # Actual table shapes exercised by inspect, with private bodies absent.
        for key in ("RAW_VIBRATION_WINDOW_DB_PATH", "VIBRATION_WINDOW_DB_PATH"):
            with closing(sqlite3.connect(self.project / ops.DB_DEFAULTS[key])) as db:
                db.execute("CREATE TABLE vibration_windows(ordinal INTEGER PRIMARY KEY AUTOINCREMENT,device TEXT,captured REAL,boot TEXT,idx INT,body TEXT,status TEXT,result TEXT)")
                db.commit()
        with closing(sqlite3.connect(self.project / ops.DB_DEFAULTS["ALERT_DB_PATH"])) as db:
            db.execute("CREATE TABLE alert_deliveries(status TEXT,due_at REAL)")
        self.capacity = mock.patch.object(ops.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(100*ops.GiB, 10*ops.GiB, 90*ops.GiB))
        self.capacity.start()
        self.addCleanup(self.capacity.stop)
        self.git = mock.patch.object(ops, "run", return_value="a"*40)
        self.git.start()
        self.addCleanup(self.git.stop)

    def backup(self, **options):
        return ops.make_backup(self.project, self.env, {"User": "ubuntu"}, self.root,
                               stopped=options.get("stopped", lambda: True))

    def test_wal_backup_restore_and_private_output(self):
        path = self.project / ops.DB_DEFAULTS["STATE_DB_PATH"]
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("INSERT INTO evidence VALUES('committed in WAL')")
            db.commit()
            folder = self.backup()
        restored = Path(self.temp.name) / "drill"
        result = ops.restore_drill(folder, restored)
        self.assertTrue(result["verified"])
        with closing(ops.readonly(restored / "STATE_DB_PATH.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM evidence").fetchone()[0], 2)
        self.assertEqual((restored / "AUTH_USERS_FILE.bin").read_text(), '{"secret":"not-for-report"}')
        with self.assertRaises(FileExistsError):
            ops.restore_drill(folder, restored)
        self.assertNotIn("not-for-report", (folder / "manifest.json").read_text())

    def test_missing_or_alias_database_rejected_before_backup(self):
        self.env["RAW_VIBRATION_WINDOW_DB_PATH"] = ops.DB_DEFAULTS["STATE_DB_PATH"]
        with self.assertRaises(ValueError):
            self.backup()
        self.assertFalse(self.root.exists())
        self.env["RAW_VIBRATION_WINDOW_DB_PATH"] = "missing.db"
        with self.assertRaises(ValueError):
            self.backup()
        self.assertFalse((self.project / "missing.db").exists())

    def test_tamper_traversal_and_incomplete_set_fail_before_restore(self):
        folder = self.backup()
        manifest_path = folder / "manifest.json"
        original = json.loads(manifest_path.read_text())
        for kind in ("missing", "traversal", "wrong_type", "hash", "duplicate"):
            manifest = json.loads(json.dumps(original))
            if kind == "missing": manifest["files"].pop(0)
            if kind == "traversal": manifest["files"][0]["file"] = "../outside.bin"
            if kind == "wrong_type": manifest["files"][0]["kind"] = "private_config"
            if kind == "hash": manifest["files"][0]["sha256"] = "0"*64
            if kind == "duplicate": manifest["files"].append(manifest["files"][0])
            manifest_path.write_text(json.dumps(manifest))
            destination = Path(self.temp.name) / kind
            with self.assertRaises(ValueError): ops.restore_drill(folder, destination)
            self.assertFalse(destination.exists())

    def test_service_restarts_when_stop_or_copy_fails(self):
        with mock.patch.object(ops, "discover", return_value=(self.project, self.env, {})), \
             mock.patch.object(ops, "make_backup", side_effect=OSError("disk")), \
             mock.patch.object(ops, "run") as run:
            with self.assertRaises(OSError): ops.backup_service("motordiagnosis", self.root)
            self.assertEqual(run.call_args_list, [mock.call("systemctl", "stop", "motordiagnosis"), mock.call("systemctl", "start", "motordiagnosis")])
            run.reset_mock()
            run.side_effect = [OSError("stop timed out"), ""]
            with self.assertRaises(OSError): ops.backup_service("motordiagnosis", self.root)
            self.assertEqual(run.call_args_list[-1], mock.call("systemctl", "start", "motordiagnosis"))

    def test_unexpected_restart_and_low_disk_never_complete_backup(self):
        with self.assertRaises(ValueError): self.backup(stopped=lambda: False)
        self.assertFalse(list(self.root.glob("*/manifest.json")))
        with mock.patch.object(ops.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(10, 9, 1)):
            with self.assertRaises(ValueError): self.backup()

    def test_resume_requires_backup_marker_and_uses_nonblocking_start(self):
        with mock.patch.object(ops, "run") as run:
            self.assertFalse(ops.resume_backup(self.project, "motordiagnosis")["restartScheduled"])
            run.assert_not_called()
            ops.write_json(self.project / ".operations-backup-resume.json", {"service":"motordiagnosis"})
            with mock.patch.object(ops, "service_info", return_value={"ActiveState":"inactive"}):
                self.assertTrue(ops.resume_backup(self.project, "motordiagnosis")["restartScheduled"])
            run.assert_called_once_with("systemctl", "--no-block", "start", "motordiagnosis")
            self.assertFalse((self.project / ".operations-backup-resume.json").exists())

    def test_model_checksum_and_optional_history(self):
        model = self.project / "model.zip"
        model.write_bytes(b"trusted fixture model")
        self.env.update(RF66_MODEL_ARTIFACT=str(model), RF66_MODEL_CHECKSUM="sha256:wrong")
        with self.assertRaises(ValueError): self.backup()
        self.env["RF66_MODEL_CHECKSUM"] = "sha256:" + ops.digest(model)
        manifest = ops.verify_backup(self.backup())
        self.assertIn("SHADOW_MODEL_DB_PATH", manifest["absentOptional"])
        self.assertIn("RF66_MODEL_ARTIFACT", [i["key"] for i in manifest["files"]])

    def test_inspection_absent_backlog_and_disk_no_secret_or_creation(self):
        missing = self.project / ops.DB_DEFAULTS["SHADOW_MODEL_DB_PATH"]
        report = ops.inspect(self.project, self.env, "DEV-01-MOT-02", now=100)
        self.assertEqual(report["status"], "warning")
        self.assertIn("RAW_INPUT_ABSENT", [i["code"] for i in report["issues"]])
        self.assertNotIn("not-for-report", json.dumps(report))
        self.assertFalse(missing.exists())
        path = self.project / ops.DB_DEFAULTS["RAW_VIBRATION_WINDOW_DB_PATH"]
        with closing(sqlite3.connect(path)) as db:
            db.execute("INSERT INTO vibration_windows(device,captured,boot,idx,body,status) VALUES(?,?,?,?,?,?)",
                       ("DEV-01-MOT-02", 1, "abc", 0, '{"startUptimeUs":0,"quality":"valid"}', "queued"))
            db.commit()
        report = ops.inspect(self.project, self.env, "DEV-01-MOT-02", now=100)
        codes = [i["code"] for i in report["issues"]]
        self.assertIn("RAW_INPUT_STALE_OR_FUTURE", codes)
        self.assertIn("RAW_VIBRATION_WINDOW_DB_PATH:PROCESSING_BACKLOG", codes)
        with mock.patch.object(ops.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(10*ops.GiB, 9*ops.GiB, ops.GiB)):
            self.assertEqual(ops.inspect(self.project, self.env, "D")["status"], "critical")

    def test_frozen_time_is_not_successful_observation(self):
        def report(ordinal):
            return {"status": "ok", "issues": [], "databases": {"RAW_VIBRATION_WINDOW_DB_PATH": {"latest": {"ordinal": ordinal, "captured": 1}}}}
        with mock.patch.object(ops, "discover", return_value=(self.project, self.env, {"MainPID":"1"})), \
             mock.patch.object(ops, "inspect", side_effect=[report(1), report(2)]), \
             mock.patch.object(ops.time, "monotonic", side_effect=[0, 0, 1]), \
             mock.patch.object(ops.time, "sleep"):
            output = Path(self.temp.name) / "reports" / "probe.jsonl"
            result = ops.observe("motordiagnosis", "D", 1, 1, output)
        self.assertEqual(result["status"], "critical")
        self.assertIn("MEASUREMENT_TIME_NOT_ADVANCING", output.read_text())

    def test_exact_key_preview_removal_recoverable_other_keys_untouched(self):
        path = Path(self.temp.name) / "authorized_keys"
        path.write_bytes(b'# ssh-ed25519 TARGET comment\nssh-ed25519 OTHER keep\nrestrict ssh-ed25519 TARGET rf66-upload\n')
        self.assertEqual(ops.revoke_upload_key(path, "TARGET"), {"matched":1,"changed":False})
        result = ops.revoke_upload_key(path, "TARGET", apply=True)
        self.assertTrue(result["changed"])
        self.assertIn(b"restrict ssh-ed25519 TARGET", Path(result["backup"]).read_bytes())
        self.assertEqual(path.read_bytes(), b'# ssh-ed25519 TARGET comment\nssh-ed25519 OTHER keep\n')
        self.assertFalse(ops.revoke_upload_key(path, "TARGET", apply=True)["changed"])

    def test_backup_age_missing_and_stale(self):
        self.assertEqual(ops.backup_status(self.root)["code"], "NO_COMPLETE_BACKUP")
        folder = self.backup()
        manifest = json.loads((folder / "manifest.json").read_text())
        now = datetime.fromisoformat(manifest["createdAt"]).timestamp()
        self.assertEqual(ops.backup_status(self.root, now=now+3600)["status"], "ok")
        self.assertEqual(ops.backup_status(self.root, now=now+37*3600)["status"], "warning")

    def test_backup_lock_prevents_overlapping_maintenance(self):
        with ops.maintenance_lock(self.project):
            with self.assertRaises(OSError):
                with ops.maintenance_lock(self.project):
                    self.fail("Second backup entered the maintenance boundary")
        with ops.maintenance_lock(self.project):
            pass

    def test_key_in_command_or_comment_is_not_revoked(self):
        path = Path(self.temp.name) / "authorized_keys"
        original = b'command="echo ssh-ed25519 TARGET" ssh-ed25519 OTHER keep\nssh-rsa OTHER ssh-ed25519 TARGET\n'
        path.write_bytes(original)
        self.assertEqual(ops.revoke_upload_key(path, "TARGET", apply=True)["matched"], 0)
        self.assertEqual(path.read_bytes(), original)

    def test_discovery_excludes_secret_values_and_rejects_pid_change(self):
        info = {"MainPID": "123", "ActiveState": "active"}
        def read(path):
            if path.name == "cmdline":
                return b"python\0app.py\0"
            return b"AUTH_USERS_FILE=/private/auth.json\0ALERT_SMTP_PASSWORD=never-expose\0RF66_EVENT_MODE=events\0"
        with mock.patch.object(ops, "service_info", return_value=info), \
             mock.patch.object(ops.os, "readlink", return_value=str(self.project)), \
             mock.patch.object(Path, "read_bytes", read):
            _, env, _ = ops.discover("motordiagnosis")
            self.assertEqual(env, {"AUTH_USERS_FILE":"/private/auth.json", "RF66_EVENT_MODE":"events"})
            with mock.patch.object(ops, "service_info", side_effect=[info, {**info, "MainPID":"124"}]):
                with self.assertRaises(ValueError): ops.discover("motordiagnosis")


if __name__ == "__main__":
    unittest.main()
