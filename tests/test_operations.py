"""Maintenance failure/recovery contracts using disposable SQLite stores."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

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
        with closing(sqlite3.connect(self.project / ops.DB_DEFAULTS["PERIODIC_SNAPSHOT_DB_PATH"])) as db:
            db.execute("CREATE TABLE periodic_snapshots(device TEXT,captured REAL,received REAL,boot TEXT,idx INT,quality TEXT,status TEXT,ordinal INTEGER,late INTEGER,body TEXT,result TEXT)")
        self.capacity = mock.patch.object(ops.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(100*ops.GiB, 10*ops.GiB, 90*ops.GiB))
        self.capacity.start()
        self.addCleanup(self.capacity.stop)
        self.git = mock.patch.object(ops, "run", return_value="a"*40)
        self.git.start()
        self.addCleanup(self.git.stop)

    def backup(self, **options):
        return ops.make_backup(self.project, self.env, {"User": "ubuntu"}, self.root,
                               stopped=options.get("stopped", lambda: True))

    def rf66_package(self, *, content=b"model bytes; never deserialize in maintenance",
                     corrupt=None, model_version=None):
        """Real handoff layout, without requiring executable pickle or ML packages."""
        path = self.project / "model.zip"
        checksum = "sha256:" + hashlib.sha256(content).hexdigest()
        files = {
            "model/candidate.joblib": content,
            "input-contract.json": b'{"modelInputShape":[1,66]}',
            "decision-rule.json": b'{"comparison":">","threshold":0.7}',
            "environment.json": b'{"packages":{}}',
        }
        manifest = {
            "modelVersion": model_version or checksum,
            "files": {name: hashlib.sha256(body).hexdigest()
                      for name, body in files.items()},
        }
        if corrupt:
            files[corrupt] += b"changed after manifest was generated"
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, body in files.items():
                archive.writestr(name, body)
            archive.writestr("MANIFEST.json", json.dumps(manifest))
        self.env.update(RF66_MODEL_ARTIFACT=str(path), RF66_MODEL_CHECKSUM=checksum)
        return path, checksum

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
        model, checksum = self.rf66_package()
        archive_hash = ops.digest(model)
        self.assertNotEqual(checksum, "sha256:" + archive_hash)
        folder = self.backup()
        manifest = ops.verify_backup(folder)
        self.assertIn("SHADOW_MODEL_DB_PATH", manifest["absentOptional"])
        item = next(i for i in manifest["files"] if i["key"] == "RF66_MODEL_ARTIFACT")
        self.assertEqual(item["sha256"], archive_hash)
        self.assertEqual(manifest["modelChecksum"], checksum)
        restored = Path(self.temp.name) / "model-restore"
        self.assertTrue(ops.restore_drill(folder, restored)["verified"])
        self.assertEqual((restored / item["file"]).read_bytes(), model.read_bytes())
        self.assertEqual(ops.verify_backup(restored)["modelChecksum"], checksum)

    def test_rf66_backup_rejects_wrong_or_container_checksum(self):
        model, _ = self.rf66_package()
        for checksum in (None, "", "sha256:wrong", "sha256:" + "0" * 64,
                         "sha256:" + ops.digest(model)):
            with self.subTest(checksum=checksum):
                self.env["RF66_MODEL_CHECKSUM"] = checksum
                with self.assertRaises(ValueError):
                    self.backup()
                self.assertFalse(list(self.root.glob("*/manifest.json")))

    def test_rf66_backup_rejects_corrupt_members_and_untrusted_model(self):
        for member in ("model/candidate.joblib", "input-contract.json"):
            with self.subTest(member=member):
                self.rf66_package(corrupt=member)
                with self.assertRaisesRegex(ValueError, "manifest checksum"):
                    self.backup()
                self.assertFalse(list(self.root.glob("*/manifest.json")))
        self.rf66_package(model_version="sha256:" + "0" * 64)
        with self.assertRaisesRegex(ValueError, "trusted model checksum"):
            self.backup()
        _, trusted = self.rf66_package()
        self.rf66_package(content=b"replacement model", model_version=trusted)
        self.env["RF66_MODEL_CHECKSUM"] = trusted
        with self.assertRaisesRegex(ValueError, "trusted model checksum"):
            self.backup()
        self.assertFalse(list(self.root.glob("*/manifest.json")))

    def test_rf66_checksum_without_artifact_rejected_before_stopping(self):
        self.env["RF66_MODEL_CHECKSUM"] = "sha256:" + "0" * 64
        with mock.patch.object(ops, "discover", return_value=(self.project, self.env, {})), \
             mock.patch.object(ops, "run") as run:
            with self.assertRaisesRegex(ValueError, "requires a model artifact"):
                ops.backup_service("motordiagnosis", self.root)
            run.assert_not_called()
        self.assertFalse(self.root.exists())

    def test_rf66_backup_and_restore_need_no_ml_or_application_imports(self):
        model, checksum = self.rf66_package()
        code = """
from pathlib import Path
import sys
from tests.test_operations import OperationsTest
from motor_diagnosis import operations

case = OperationsTest()
case.setUp()
try:
    case.env.update(RF66_MODEL_ARTIFACT=sys.argv[1], RF66_MODEL_CHECKSUM=sys.argv[2])
    folder = case.backup()
    assert operations.restore_drill(folder, Path(case.temp.name) / 'restore')['verified']
    forbidden = {'numpy', 'scipy', 'sklearn', 'joblib', 'torch',
                 'motor_diagnosis.data', 'motor_diagnosis.server', 'motor_diagnosis.rf66'}
    assert not forbidden.intersection(sys.modules)
finally:
    case.doCleanups()
"""
        result = subprocess.run(
            [sys.executable, "-S", "-c", code, str(model), checksum],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rf66_backup_rejects_non_zip_missing_and_duplicate_model(self):
        model, _ = self.rf66_package()
        for kind in ("non_zip", "missing", "duplicate"):
            with self.subTest(kind=kind):
                if kind == "non_zip":
                    model.write_bytes(b"not a ZIP")
                elif kind == "missing":
                    with zipfile.ZipFile(model, "w") as archive:
                        archive.writestr("MANIFEST.json", json.dumps({
                            "files": {}, "modelVersion": self.env["RF66_MODEL_CHECKSUM"]
                        }))
                else:
                    self.rf66_package()
                    with zipfile.ZipFile(model, "a") as archive:
                        with self.assertWarns(UserWarning):
                            archive.writestr("model/candidate.joblib", b"duplicate")
                with self.assertRaises(ValueError):
                    self.backup()
                self.assertFalse(list(self.root.glob("*/manifest.json")))

    def test_rf66_failure_still_restarts_service_without_complete_backup(self):
        self.rf66_package(corrupt="model/candidate.joblib")
        with mock.patch.object(ops, "discover", return_value=(self.project, self.env, {})), \
             mock.patch.object(ops, "service_info", return_value={"ActiveState": "inactive", "MainPID": "0"}), \
             mock.patch.object(ops, "run") as run:
            with self.assertRaises(ValueError):
                ops.backup_service("motordiagnosis", self.root)
            self.assertEqual(run.call_args_list[-1], mock.call("systemctl", "start", "motordiagnosis"))
        self.assertFalse(list(self.root.glob("*/manifest.json")))
        self.assertFalse((self.project / ".operations-backup-resume.json").exists())

    def test_rf66_restore_rejects_container_tamper_before_creating_destination(self):
        self.rf66_package()
        folder = self.backup()
        model = folder / "RF66_MODEL_ARTIFACT.bin"
        with model.open("ab") as handle:
            handle.write(b"archive changed")
        destination = Path(self.temp.name) / "tampered-restore"
        with self.assertRaisesRegex(ValueError, "hash/size mismatch"):
            ops.restore_drill(folder, destination)
        self.assertFalse(destination.exists())

    def test_rf66_restore_rechecks_inner_model_even_with_updated_archive_hash(self):
        self.rf66_package()
        folder = self.backup()
        manifest_path = folder / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        replacement, _ = self.rf66_package(content=b"different model")
        model = folder / "RF66_MODEL_ARTIFACT.bin"
        shutil.copyfile(replacement, model)
        item = next(i for i in manifest["files"] if i["key"] == "RF66_MODEL_ARTIFACT")
        item.update(sha256=ops.digest(model), bytes=model.stat().st_size)
        manifest_path.write_text(json.dumps(manifest))
        destination = Path(self.temp.name) / "wrong-model-restore"
        with self.assertRaisesRegex(ValueError, "trusted model checksum"):
            ops.restore_drill(folder, destination)
        self.assertFalse(destination.exists())

    def test_rf66_restore_requires_model_entry_and_trusted_checksum(self):
        self.rf66_package()
        folder = self.backup()
        manifest_path = folder / "manifest.json"
        original = json.loads(manifest_path.read_text())
        for kind in ("model_missing", "checksum_missing", "checksum_wrong"):
            manifest = json.loads(json.dumps(original))
            if kind == "model_missing":
                manifest["files"] = [i for i in manifest["files"] if i["key"] != "RF66_MODEL_ARTIFACT"]
            elif kind == "checksum_missing":
                del manifest["modelChecksum"]
            else:
                manifest["modelChecksum"] = "sha256:" + "0" * 64
            manifest_path.write_text(json.dumps(manifest))
            destination = Path(self.temp.name) / kind
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                ops.restore_drill(folder, destination)
            self.assertFalse(destination.exists())

    def test_inspection_absent_backlog_and_disk_no_secret_or_creation(self):
        missing = self.project / ops.DB_DEFAULTS["SHADOW_MODEL_DB_PATH"]
        report = ops.inspect(self.project, self.env, "DEV-01-MOT-02", now=100)
        self.assertEqual(report["status"], "warning")
        self.assertIn("PERIODIC_SNAPSHOT_INPUT_ABSENT", [i["code"] for i in report["issues"]])
        self.assertFalse(report["legacyRf66ProcessingEnabled"])
        self.assertNotIn("not-for-report", json.dumps(report))
        self.assertFalse(missing.exists())
        path = self.project / ops.DB_DEFAULTS["RAW_VIBRATION_WINDOW_DB_PATH"]
        with closing(sqlite3.connect(path)) as db:
            db.execute("INSERT INTO vibration_windows(device,captured,boot,idx,body,status) VALUES(?,?,?,?,?,?)",
                       ("DEV-01-MOT-02", 1, "abc", 0, '{"startUptimeUs":0,"quality":"valid"}', "queued"))
            db.commit()
        report = ops.inspect(self.project, self.env, "DEV-01-MOT-02", now=100)
        codes = [i["code"] for i in report["issues"]]
        self.assertNotIn("RAW_INPUT_STALE_OR_FUTURE", codes)
        self.assertNotIn("RAW_VIBRATION_WINDOW_DB_PATH:PROCESSING_BACKLOG", codes)
        self.assertTrue(report["databases"]["RAW_VIBRATION_WINDOW_DB_PATH"]["historicalOnly"])
        with mock.patch.object(ops.shutil, "disk_usage", return_value=shutil._ntuple_diskusage(10*ops.GiB, 9*ops.GiB, ops.GiB)):
            self.assertEqual(ops.inspect(self.project, self.env, "D")["status"], "critical")

    def test_frozen_time_is_not_successful_observation(self):
        def report(ordinal):
            return {"status": "ok", "issues": [], "databases": {"PERIODIC_SNAPSHOT_DB_PATH": {"latest": {"ordinal": ordinal, "captured": 1}}}}
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
