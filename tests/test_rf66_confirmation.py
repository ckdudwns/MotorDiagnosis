"""Durable 3-of-3 research confirmation, separate from per-window verdicts."""
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from motor_diagnosis.raw_vibration import RawVibrationStore
from tests.test_measured_rpm import RpmSetup
from tests import test_raw_vibration as raw_tests
from tests.test_rf66 import FakeRF

DEVICE = raw_tests.DEVICE


class ConfirmationTest(RpmSetup):
    window = raw_tests.RawWindowTest.window
    send = raw_tests.RawWindowTest.send

    def setUp(self):
        super().setUp()
        self.model = FakeRF()
        self.store = RawVibrationStore(model=self.model)
        self.addCleanup(self.store.close)

    def step(self, index, *, score=.8, **changes):
        self.model.score = score
        self.send(self.window(index, **changes))
        self.store.tick()
        return self.store.list_device(self.admin, DEVICE)["items"][0]["analysis"]

    def test_three_hits_and_sliding_normal_are_not_latched_alerts(self):
        for i, (score, expected) in enumerate(((.8,-1),(.8,-1),(.8,1),(.7,0),(.8,0),(.8,0),(.8,1))):
            result = self.step(i, score=score)
            self.assertEqual(result["confirmation"]["decision"], expected)
            self.assertEqual(result["verdict"], score > .7)
            self.assertFalse(result["affectsAlerts"])
            self.assertFalse(result["confirmation"]["affectsAlerts"])

    def test_normal_warmup_is_not_confirmed_normal(self):
        for i, expected in enumerate((-1,-1,0)):
            result = self.step(i, score=0)
            self.assertEqual(result["confirmation"]["decision"], expected)
        self.assertEqual(result["confirmation"]["status"], "no_confirmed_anomaly")

    def test_gap_starts_new_streak_at_current_window(self):
        self.step(0); self.step(1)
        result = self.step(3)["confirmation"]
        self.assertEqual((result["decision"], result["validWindows"], result["resetReason"]), (-1,1,"WINDOW_GAP"))
        self.assertEqual(self.step(4)["confirmation"]["decision"], -1)
        self.assertEqual(self.step(5)["confirmation"]["decision"], 1)

    def test_boot_change_and_return_to_old_boot_reset(self):
        self.step(0); self.step(1)
        result = self.step(0, bootId="d"*32)["confirmation"]
        self.assertEqual(result["resetReason"], "STREAM_CHANGED")
        result = self.step(2)["confirmation"]
        self.assertEqual(result["resetReason"], "STREAM_CHANGED")
        self.assertEqual(result["validWindows"], 1)

    def test_quality_and_inference_failure_reset_without_zero_verdict(self):
        self.step(0); self.step(1)
        bad = self.step(2, quality="sensor_unavailable", sampleCount=0, samples="")
        self.assertEqual(bad["confirmation"]["decision"], -1)
        self.assertEqual(bad["confirmation"]["validWindows"], 0)
        self.assertIsNone(bad["verdict"])
        self.assertEqual(self.step(3)["confirmation"]["validWindows"], 1)
        with mock.patch.object(self.model, "predict_window", side_effect=ValueError("failure")):
            self.assertEqual(self.step(4)["confirmation"]["status"], "unavailable")
        self.assertEqual(self.step(5)["confirmation"]["validWindows"], 1)

    def test_model_version_threshold_and_disabled_model_reset(self):
        self.step(0); self.step(1)
        self.model.checksum = "sha256:" + "b"*64
        self.assertEqual(self.step(2)["confirmation"]["resetReason"], "MODEL_CHANGED")
        self.model.threshold = .6
        self.assertEqual(self.step(3)["confirmation"]["validWindows"], 1)
        self.store.model = None
        result = self.step(4)
        self.assertFalse(result["confirmationApplied"])
        self.assertEqual(result["confirmation"]["status"], "unavailable")
        self.store.model = self.model
        self.assertEqual(self.step(5)["confirmation"]["validWindows"], 1)

    def test_uptime_gap_even_when_index_contiguous(self):
        self.step(0); self.step(1)
        result = self.step(2, startUptimeUs=1920000,
                           timestamp=(self.started+timedelta(seconds=1.92)).isoformat())
        self.assertEqual(result["confirmation"]["resetReason"], "TIME_GAP")
        self.assertEqual(result["confirmation"]["validWindows"], 1)

    def test_asymmetric_time_boundaries_and_measured_intervals(self):
        for interval, accepted in ((629999, False), (630000, True),
                                   (650000, True), (655401, True),
                                   (656241, True), (670000, True),
                                   (670001, False), (3000, False)):
            with self.subTest(interval=interval):
                self.store = RawVibrationStore(model=self.model)
                self.addCleanup(self.store.close)
                for i in range(3):
                    result = self.step(i, startUptimeUs=i*interval,
                        timestamp=(self.started+timedelta(microseconds=i*interval)).isoformat())
                    self.assertEqual(result["score"], .8)
                    self.assertTrue(result["verdict"])
                c = result["confirmation"]
                self.assertEqual(c["decision"], 1 if accepted else -1)
                self.assertEqual(c["validWindows"], 3 if accepted else 1)
                self.assertEqual(c["resetReason"], None if accepted else "TIME_GAP")
                self.assertEqual(c["startIntervalRangeUs"], [630000, 670000])

    def test_relaxed_intervals_still_reset_on_quality_index_and_boot(self):
        def at(index, **changes):
            return self.step(index, startUptimeUs=index*655000,
                timestamp=(self.started+timedelta(microseconds=index*655000)).isoformat(), **changes)
        at(0); at(1)
        at(2, quality="fifo_overrun", sampleCount=0, samples="")
        self.assertEqual(at(3)["confirmation"]["validWindows"], 1)
        at(4)
        self.assertEqual(at(6)["confirmation"]["resetReason"], "WINDOW_GAP")
        at(7)
        c = at(0, bootId="d"*32)["confirmation"]
        self.assertEqual((c["resetReason"], c["validWindows"]), ("STREAM_CHANGED", 1))

    def test_old_results_are_not_recalculated_when_range_changes(self):
        def at(index):
            return self.step(index, startUptimeUs=index*655000,
                timestamp=(self.started+timedelta(microseconds=index*655000)).isoformat())
        with mock.patch("motor_diagnosis.rf66_timing.MAX_START_INTERVAL_US", 650000):
            at(0)
            self.assertEqual(at(1)["confirmation"]["validWindows"], 1)
        before = self.store.db.execute("SELECT result FROM vibration_windows ORDER BY ordinal").fetchall()
        self.assertEqual(at(2)["confirmation"]["validWindows"], 2)
        self.assertEqual(at(3)["confirmation"]["decision"], 1)
        after = self.store.db.execute("SELECT result FROM vibration_windows ORDER BY ordinal LIMIT 2").fetchall()
        self.assertEqual([r[0] for r in before], [r[0] for r in after])

    def test_duplicates_and_out_of_order_do_not_increment(self):
        self.step(0); self.step(1)
        self.assertEqual(self.send(self.window(0))[1], 200)
        self.assertFalse(self.store.tick())
        from motor_diagnosis.data import ApiError
        with self.assertRaises(ApiError):
            self.send(self.window(0, samples=None))
        self.assertEqual(self.step(2)["confirmation"]["decision"], 1)
        self.step(4)
        with self.assertRaises(ApiError) as rejected:
            self.send(self.window(3))
        self.assertEqual(rejected.exception.status, 409)
        self.assertFalse(self.store.tick())

    def test_clipped_and_constant_raw_data_reset_even_if_marked_valid(self):
        index = 0
        for counts in ([(4095, 0, 0)]*512, [(1, 2, 3)]*512):
            self.step(index); self.step(index+1)
            result = self.step(index+2, samples=raw_tests.encoded(counts))
            self.assertEqual(result["confirmation"]["status"], "unavailable")
            self.assertEqual(result["confirmation"]["validWindows"], 0)
            self.assertEqual(self.step(index+3)["confirmation"]["validWindows"], 1)
            index += 4

    def test_stream_scope_change_does_not_reuse_previous_result(self):
        self.step(0); self.step(1)
        # Model a previous persisted mapping; ingestion still checks the current
        # authoritative mapping. The confirmation must not select an older run.
        with self.store.db:
            row = self.store.db.execute("SELECT ordinal,body FROM vibration_windows ORDER BY ordinal DESC LIMIT 1").fetchone()
            old = json.loads(row["body"])
            old["assetId"] = "PREVIOUS-ASSET"
            self.store.db.execute("UPDATE vibration_windows SET asset=?,body=? WHERE ordinal=?",
                                  (old["assetId"], json.dumps(old), row["ordinal"]))
        result = self.step(2)["confirmation"]
        self.assertEqual((result["validWindows"], result["resetReason"]), (1,"STREAM_CHANGED"))

    def test_write_failure_retry_is_atomic(self):
        self.step(0); self.step(1)
        self.send(self.window(2))
        self.store.db.execute("""CREATE TEMP TRIGGER fail_confirmation BEFORE UPDATE OF result
            ON vibration_windows BEGIN SELECT RAISE(ABORT,'disk failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.tick()
        row = self.store.list_device(self.admin, DEVICE)["items"][0]
        self.assertEqual(row["analysis"]["status"], "queued")
        self.store.db.execute("DROP TRIGGER fail_confirmation")
        self.store.tick()
        result = self.store.list_device(self.admin, DEVICE)["items"][0]["analysis"]
        self.assertEqual(result["confirmation"]["validWindows"], 3)
        self.assertEqual(result["confirmation"]["decision"], 1)

    def test_server_restart_preserves_committed_streak(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"raw.db"
            first = RawVibrationStore(path, model=self.model)
            try:
                self.send(self.window(0), self.window(1), self.window(2), store=first)
                first.tick(); first.tick()
            finally:
                first.close()
            second = RawVibrationStore(path, model=self.model)
            try:
                second.tick()
                result = second.list_device(self.admin, DEVICE)["items"][0]["analysis"]
                self.assertEqual(result["confirmation"]["decision"], 1)
            finally:
                second.close()

    def test_legacy_result_does_not_count_or_get_rewritten(self):
        result = self.step(0)
        result.pop("confirmation")
        result["confirmationApplied"] = False
        with self.store.db:
            self.store.db.execute("UPDATE vibration_windows SET result=?", (json.dumps(result),))
        self.assertEqual(self.step(1)["confirmation"]["validWindows"], 1)
        rows = self.store.list_device(self.admin, DEVICE)["items"]
        self.assertNotIn("confirmation", rows[-1]["analysis"])

    def test_devices_do_not_share_confirmation_state(self):
        self.step(0); self.step(1)
        other = "DEV-01-MOT-02"
        window = self.window(0, deviceId=other, assetId="SITE-01-MOT-02")
        self.store.ingest(self.principal, other, {"windows": [window]})
        self.store.tick()
        result = self.store.list_device(self.admin, other)["items"][0]["analysis"]
        self.assertEqual(result["confirmation"]["validWindows"], 1)
        self.assertEqual(self.step(2)["confirmation"]["decision"], 1)


if __name__ == "__main__":
    unittest.main()
