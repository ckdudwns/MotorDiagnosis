"""Offline field archive tests use synthetic fixtures, never real normal labels."""

from datetime import datetime, timezone
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ai.ai1.mcc5_training import field_collection as f


class FieldCollectionTest(unittest.TestCase):
    def meta(self, name="a"):
        return {
            "schemaVersion": 1,
            "sessionId": name,
            "collectionRunId": "run-" + name,
            "conditionId": "steady",
            "deviceId": "DEV-1",
            "siteId": "SITE.1",
            "assetId": "MOT.1",
            "start": "2026-09-07T00:00:00Z",
            "end": "2026-09-07T00:00:02.560Z",
            "operatingCondition": "synthetic fixture, not hardware",
            "firmwareVersion": "test",
            "mountingDescription": "synthetic",
            "axisMapping": "XYZ",
            "gConversionEvidence": "synthetic test g",
            "normalReview": {
                "status": "confirmed_normal",
                "reviewer": "unit-test",
                "basis": "synthetic fixture only",
                "reviewedAt": "2026-09-07T01:00:00Z",
            },
            "qualityLimits": {
                "minimumValidSeconds": 2.56,
                "maxMissingWindows": 0,
                "maxInvalidWindows": 0,
                "maxTimingErrorUs": 0,
            },
        }

    def window(self, index, boot="a" * 32):
        captured = f.timestamp(self.meta()["start"]) + index * 0.640
        return {
            "schemaVersion": 1,
            "deviceId": "DEV-1",
            "siteId": "SITE.1",
            "assetId": "MOT.1",
            "bootId": boot,
            "windowIndex": index,
            "timestamp": datetime.fromtimestamp(captured, timezone.utc).isoformat(),
            "startUptimeUs": index * 640000,
            "sampleRateHz": 800,
            "sampleCount": 512,
            "profileId": f.PROFILE,
            "axes": ["X", "Y", "Z"],
            "unit": "g",
            "quality": "valid",
            "features": [1, 2, 3, 0.1, 0.2, 0.3, 0.4] * 3,
        }

    def write_windows(self, root, rows):
        path = root / "windows.ndjson"
        path.write_text("".join(json.dumps(w) + "\n" for w in rows), encoding="utf-8")
        return path

    def archive(self, root, name, boot):
        folder = root / name
        folder.mkdir()
        meta = self.meta(name)
        path = self.write_windows(folder, [self.window(i, boot) for i in range(4)])
        f.dump(folder / "session.json", meta)
        audit, _ = f.inspect(meta, path)
        audit.update(
            metadataSha256=f.digest(folder / "session.json"),
            windowsSha256=f.digest(path),
        )
        f.dump(folder / "audit.json", audit)
        return folder

    def test_template_is_pending_and_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "session.json"
            f.template(path)
            meta = f.decode(path.read_text(encoding="utf-8"))
            self.assertEqual(meta["normalReview"]["status"], "pending")
            self.assertIsNone(meta["qualityLimits"]["maxTimingErrorUs"])
            with self.assertRaises(ValueError):
                f.metadata(meta)
            with self.assertRaises(FileExistsError):
                f.template(path)

    def test_strict_timestamp_and_json(self):
        for value in (
            "2026-09-07T24:00:00Z",
            "2026-02-30T00:00:00Z",
            "2026-09-07T00:00:00",
        ):
            with self.assertRaises(ValueError):
                f.timestamp(value)
        for text in ('{"a":1,"a":2}', '{"a":NaN}'):
            with self.assertRaises(ValueError):
                f.decode(text)

    def test_valid_archive_never_claims_spectral66_or_field_accuracy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = self.write_windows(root, [self.window(i) for i in range(4)])
            result, ids = f.inspect(self.meta(), path)
            self.assertTrue(result["passesCollectionChecks"])
            self.assertFalse(result["spectral66Ready"])
            self.assertFalse(result["fieldValidated"])
            self.assertEqual(len(ids), 4)

    def test_review_and_timing_are_explicit_requirements(self):
        with tempfile.TemporaryDirectory() as temp:
            path = self.write_windows(Path(temp), [self.window(i) for i in range(4)])
            meta = self.meta()
            meta["normalReview"]["status"] = "pending"
            meta["qualityLimits"]["maxTimingErrorUs"] = None
            result, _ = f.inspect(meta, path)
            self.assertIn("HUMAN_NORMAL_REVIEW_REQUIRED", result["blockingReasons"])
            self.assertIn("TIMING_TOLERANCE_NOT_AGREED", result["blockingReasons"])

    def test_gaps_and_invalid_windows_preserved_and_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            rows = [self.window(i) for i in (0, 2, 3)]
            rows[-1].update(quality="clipped", features=None)
            result, _ = f.inspect(self.meta(), self.write_windows(Path(temp), rows))
            self.assertEqual(result["windows"], 3)
            self.assertEqual(result["missingWindowsInsideStream"], 1)
            self.assertIn("MISSING_WINDOW_LIMIT", result["blockingReasons"])
            self.assertIn("INVALID_WINDOW_LIMIT", result["blockingReasons"])

    def test_scope_shape_order_and_duplicate_rejection(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for mutation in (
                lambda w: w.update(deviceId="OTHER"),
                lambda w: w.update(features=[1] * 4),
                lambda w: w.update(unit="raw"),
                lambda w: w.update(sampleCount=511),
                lambda w: w.update(authorization="not-an-allowed-field"),
            ):
                rows = [self.window(i) for i in range(4)]
                mutation(rows[0])
                with self.assertRaises(ValueError):
                    f.inspect(self.meta(), self.write_windows(root, rows))
            for indices in ([0, 1, 1, 3], [1, 0, 2, 3]):
                with self.assertRaises(ValueError):
                    f.inspect(
                        self.meta(),
                        self.write_windows(root, [self.window(i) for i in indices]),
                    )

    def test_export_readonly_full_history_and_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            db = root / "source.sqlite3"
            with closing(sqlite3.connect(db)) as connection, connection:
                connection.execute(
                    "CREATE TABLE vibration_windows(ordinal INTEGER PRIMARY KEY,device TEXT,site TEXT,asset TEXT,captured REAL,body TEXT)"
                )
                for i in range(4):
                    w = self.window(i)
                    connection.execute(
                        "INSERT INTO vibration_windows VALUES(?,?,?,?,?,?)",
                        (
                            i,
                            "DEV-1",
                            "SITE.1",
                            "MOT.1",
                            f.timestamp(w["timestamp"]),
                            json.dumps(w),
                        ),
                    )
            before = f.digest(db)
            f.dump(root / "session.json", self.meta())
            report = f.export(db, root / "session.json", root / "export")
            self.assertEqual(report["windows"], 4)
            self.assertEqual(f.digest(db), before)
            self.assertTrue(report["passesCollectionChecks"])
            self.assertTrue(
                f.check_archive(root / "export")[2]["passesCollectionChecks"]
            )
            with self.assertRaises(FileExistsError):
                f.export(db, root / "session.json", root / "export")
            with self.assertRaises(FileNotFoundError):
                f.export(root / "absent.sqlite3", root / "session.json", root / "new")
            self.assertFalse((root / "absent.sqlite3").exists())

    def test_group_split_deterministic_and_independent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            a, b = self.archive(root, "a", "a" * 32), self.archive(root, "b", "b" * 32)
            result = f.build([a, b], root / "plan")
            swapped = f.build([b, a], root / "plan2")
            mapping = lambda r: {
                v["collectionRunId"]: v["split"] for v in r["sessions"]
            }
            self.assertEqual(mapping(result), mapping(swapped))
            self.assertEqual(
                set(mapping(result).values()), {"calibration", "evaluation"}
            )
            self.assertFalse(result["spectral66Ready"])
            with self.assertRaises(ValueError):
                f.build([a], root / "few")
            with self.assertRaises(ValueError):
                f.build([], root / "empty")

    def test_tampering_and_overlapping_archives_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            a, b = self.archive(root, "a", "a" * 32), self.archive(root, "b", "a" * 32)
            with self.assertRaisesRegex(ValueError, "overlapping"):
                f.build([a, b], root / "overlap")
            (a / "session.json").write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash"):
                f.check_archive(a)
            with self.assertRaisesRegex(ValueError, "hash"):
                f.build([a, b], root / "tampered")
