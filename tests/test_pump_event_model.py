"""Local-only model parity, durable history, API and event regressions."""
from contextlib import closing
import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import json
from pathlib import Path
import tempfile
import sqlite3
import threading
import unittest
from unittest import mock

from motor_diagnosis import data, operations
from motor_diagnosis.periodic_snapshots import PeriodicSnapshotStore
from motor_diagnosis.pump_event_model import PumpEventModel, configured_model, read_artifact, score_vector, ENV_KEYS
from motor_diagnosis.pump_summary import FEATURES, PROFILE_ID, POLICY_ID, feature_digest
from motor_diagnosis.server import create_server
from tests.test_measured_rpm import RpmSetup

ARTIFACT = Path(__file__).resolve().parents[1] / "models/pump-event-verifier/event-verifier-model.json"
CHECKSUM = "0c2d24015a44744fb5f96554652dcbd3d2a82d30949cf36f729a620084429135"
STREAM = "freshwater_supply_motor2"
SCOPE = {"deviceId": "DEV-01-MOT-02", "siteId": "SITE-01", "assetId": "SITE-01-MOT-02", "sensorId": "SENSOR-02"}
DEVICE = SCOPE["deviceId"]


class VerifierArtifactTest(unittest.TestCase):
    def test_actual_file_and_two_streams_are_pinned_without_retraining(self):
        model = read_artifact(ARTIFACT, CHECKSUM)
        self.assertEqual(len(model["streams"]), 2)
        for stream in model["streams"]:
            adapter = PumpEventModel(ARTIFACT, CHECKSUM, stream, SCOPE)
            self.assertEqual(adapter.metadata()["threshold"], 1)
            self.assertEqual(adapter.metadata()["modelVersion"], "sha256:"+CHECKSUM)
            self.assertFalse(adapter.matches({**SCOPE, "profileId": "adxl345-800hz-xyz-counts-v1"}))
        with self.assertRaises(ValueError):
            PumpEventModel(ARTIFACT, CHECKSUM, "wrong-stream", SCOPE)

    def test_numpy_reference_golden_scores_for_both_real_streams(self):
        # Independent numpy verifier_vector/predict equations from PR45 be6ec32.
        # These are synthetic inputs for numerical parity, not field accuracy.
        history = [[1.04+.001*(i%4),1.1+.002*(i%3),1.01,.01*(i%5-2),-.01,.005*(i%3),3+.02*(i%4),3-.01*(i%3),2.99] for i in range(24)]
        expected = {STREAM: [(4.904703728515021,9.705788478240821,3.858358297694623),
                             (57.015196189612276,242.21926731030402,11.306871959054778)],
                    "freshwater_supply_motor3": [(7.898898031838985,63.2879286943134,3.58403003577577),
                                                  (22.738618688992204,182.18744847641497,9.409466843034934)]}
        for stream, golden in expected.items():
            for factor, values in zip((1, 2), golden):
                actual = score_vector(read_artifact(ARTIFACT, CHECKSUM)["streams"][stream], history,
                                      [v*factor for v in history[-1]])
                for a, b in zip(actual, values):
                    self.assertAlmostEqual(a, b, places=9)

    def test_missing_settings_and_bad_artifacts_fail_closed(self):
        self.assertIsNone(configured_model({}))
        for key in ENV_KEYS:
            with self.assertRaises(ValueError):
                configured_model({key: "x"})
        with self.assertRaises(ValueError):
            read_artifact(ARTIFACT, "0"*64)
        original = json.loads(ARTIFACT.read_text())
        mutations = [lambda m:m.update(historyRows=23), lambda m:m.update(intervalSec=True),
                     lambda m:m.update(features=list(reversed(m["features"]))), lambda m:m.update(extra=1),
                     lambda m:m["streams"][STREAM].update(active=[True]),
                     lambda m:m["streams"][STREAM].update(active=[1, 1]),
                     lambda m:m["streams"][STREAM]["scale"].__setitem__(1, 0),
                     lambda m:m["streams"][STREAM].update(thresholds=[0, 1]),
                     lambda m:m["streams"][STREAM]["pcaAxes"][0].__setitem__(0, 100)]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/"model.json"
            for mutate in mutations:
                m = copy.deepcopy(original); mutate(m)
                raw = json.dumps(m).encode(); path.write_bytes(raw)
                with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                    read_artifact(path, hashlib.sha256(raw).hexdigest())
            for raw in (b'{"schemaVersion":1,"schemaVersion":1}', b'{"x":NaN}'):
                path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    read_artifact(path, hashlib.sha256(raw).hexdigest())

    def test_explicit_complete_configuration_and_wrong_sensor_binding(self):
        values = [str(ARTIFACT), CHECKSUM, STREAM, SCOPE["deviceId"], SCOPE["siteId"], SCOPE["assetId"], SCOPE["sensorId"]]
        adapter = configured_model(dict(zip(ENV_KEYS, values)))
        self.assertTrue(adapter.matches({**SCOPE, "profileId":PROFILE_ID}))
        self.assertFalse(adapter.matches({**SCOPE, "profileId":PROFILE_ID, "sensorId":"SENSOR-03"}))
        for key in ENV_KEYS:
            env = dict(zip(ENV_KEYS, values)); del env[key]
            with self.assertRaises(ValueError):
                configured_model(env)


class VerifierHistoryTest(RpmSetup):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.started = (datetime.now(timezone.utc)-timedelta(seconds=601)).replace(microsecond=0)
        self.model_path = self.path/"test-only-model.json"
        model = json.loads(ARTIFACT.read_text())
        # Exact reference boundary fixture; never used by app defaults.
        stream = model["streams"][STREAM]
        stream.update(center=[0.]*45, scale=[1.]*45, active=list(range(45)), pcaMean=[0.]*45,
                      pcaAxes=[[float(i == j) for j in range(45)] for i in range(10)], thresholds=[1., 1.])
        self.model_path.write_text(json.dumps(model))
        self.checksum = hashlib.sha256(self.model_path.read_bytes()).hexdigest()
        self.adapter = PumpEventModel(self.model_path, self.checksum, STREAM, SCOPE)
        self.store = PeriodicSnapshotStore(self.path/"inbox.sqlite3", model=self.adapter)
        self.addCleanup(lambda: self.store.close())

    def payload(self, seconds, *, sequence=None, reason="history_periodic", quality="valid", value=0., boot="a"*32):
        features = dict.fromkeys(FEATURES, value) if quality == "valid" else None
        state, mode, interval, a, n = {"history_periodic": ("NORMAL","periodic",25,0,0),
            "anomaly_enter": ("ANOMALY_ACTIVE","immediate",0,3,0),
            "anomaly_periodic": ("ANOMALY_ACTIVE","periodic",10,6,0),
            "normal_recovered": ("NORMAL","immediate",0,0,5)}[reason]
        payload = {"window": {**SCOPE, "schemaVersion": 1, "bootId": boot, "windowIndex": int(seconds*2),
            "timestamp": (self.started+timedelta(seconds=seconds)).isoformat(), "startUptimeUs": int(seconds*1e6),
            "profileId": PROFILE_ID, "quality": quality, "features": features, "historySequence": sequence,
            "integrity": {"algorithm": "sha256", "digest": feature_digest(features)}},
            "transmission": {"policyId": POLICY_ID,"reason":reason,"state":state,"mode":mode,
                             "intervalSec":interval,"anomalyCount":a,"normalCount":n}}
        if reason == "anomaly_enter":
            raw = bytes(512*6)
            payload["window"]["rawWindow"] = {"sampleRateHz":800,"sampleCount":512,"axes":["X","Y","Z"],
                "encoding":"base64-int16le-xyz","gPerCount":.0039,"samples":base64.b64encode(raw).decode(),
                "integrity":{"algorithm":"sha256","digest":hashlib.sha256(raw).hexdigest()}}
        return payload

    def send(self, payload):
        return self.store.ingest(self.principal, DEVICE, payload)

    def process(self):
        while self.store.tick():
            pass
        while self.store.inference.tick():
            pass

    def result(self):
        return self.store.list_device(self.admin, DEVICE)["items"][0]["analysis"]

    def history(self, *, omit=None, bad=None, start=0):
        for i in range(24):
            if i == omit:
                continue
            self.send(self.payload(start+i*25, sequence=i,
                                   quality="fifo_overrun" if i == bad else "valid"))

    def test_24_history_plus_current_boundary_and_persisted_evidence(self):
        self.history()
        self.process()
        self.assertEqual(self.result()["reason"], "VERIFIER_REQUIRES_24_PRIOR_RECORDS")
        ack, code = self.send(self.payload(600, sequence=24, value=1.))
        self.assertEqual((code, ack["accepted"]), (202,1))
        self.process()
        result = self.result()
        self.assertEqual((result["status"],result["score"],result["verdict"]), ("completed",1.,False))
        self.assertEqual(result["evidence"]["decision"], "not_confirmed_by_baseline")
        self.assertEqual(result["evidence"]["historyOrdinals"], list(range(1,25)))
        self.assertNotIn("confidence", result["evidence"])
        self.assertEqual(result["evidence"]["historySpanSec"], 575)
        view = self.store.list_device(self.admin, DEVICE)
        self.assertTrue(view["inferenceEnabled"])
        self.assertEqual(view["preferredPolicyId"], "edge-feature-snapshot-v1")

    def test_extra_event_reports_do_not_advance_or_pollute_history(self):
        self.history()
        self.send(self.payload(580, reason="anomaly_enter", value=9.))
        self.send(self.payload(590, reason="anomaly_periodic", value=10.))
        self.send(self.payload(600, sequence=24))
        self.process()
        self.assertFalse(self.result()["verdict"])
        self.assertEqual(self.result()["evidence"]["historyOrdinals"], list(range(1,25)))

    def test_bad_quality_is_not_filtered_out_of_history(self):
        self.history(bad=12)
        self.send(self.payload(600, reason="anomaly_enter", value=10))
        self.process()
        self.assertEqual(self.result()["reason"], "VERIFIER_HISTORY_QUALITY_OR_SCOPE")
        self.assertIsNone(self.result()["verdict"])

    def test_missing_and_non25_second_history_cannot_be_filled(self):
        self.history(omit=12)
        self.send(self.payload(600, sequence=24))
        self.process()
        self.assertEqual(self.result()["reason"], "VERIFIER_REQUIRES_24_PRIOR_RECORDS")
        self.send(self.payload(625, sequence=25))
        self.process()
        self.assertEqual(self.result()["reason"], "VERIFIER_HISTORY_DISCONTINUITY")

    def test_model_delay_limit_is_50_seconds_not_receipt_delay(self):
        self.history()
        self.send(self.payload(625, reason="anomaly_enter"))
        self.process()
        self.assertEqual(self.result()["status"], "completed")
        self.send(self.payload(626, reason="anomaly_periodic"))
        self.process()
        self.assertEqual(self.result()["reason"], "VERIFIER_EVENT_HISTORY_TOO_OLD")

    def test_reboot_resets_history_and_does_not_choose_another_sensor(self):
        self.history()
        self.send(self.payload(600, sequence=0, boot="b"*32))
        self.process()
        self.assertEqual(self.result()["reason"], "VERIFIER_REQUIRES_24_PRIOR_RECORDS")
        self.assertFalse(self.adapter.matches({**SCOPE, "deviceId": "DEV-01-GEN-01", "profileId": PROFILE_ID}))

    def test_out_of_order_backfill_does_not_rewrite_event_context(self):
        self.history(omit=12)
        self.send(self.payload(600, reason="anomaly_enter"))
        self.send(self.payload(300, sequence=12))
        self.process()
        self.assertEqual(self.result()["reason"], "VERIFIER_REQUIRES_24_PRIOR_RECORDS")
        self.send(self.payload(601, reason="anomaly_periodic"))
        self.process()
        self.assertEqual(self.result()["status"], "completed")

    def test_retry_restart_and_model_change_preserve_original_results(self):
        self.history()
        original = self.payload(600, reason="anomaly_enter", value=10)
        first, _ = self.send(original)
        self.process(); before = self.result()
        self.store.close()
        self.store = PeriodicSnapshotStore(self.path/"inbox.sqlite3", model=self.adapter)
        second, status = self.send(original)
        self.assertEqual((status, second["accepted"]), (200,0))
        self.assertEqual(first["acknowledged"], second["acknowledged"])
        self.assertEqual(self.result(), before)
        self.assertFalse(self.store.inference.tick())
        self.send(self.payload(610, reason="anomaly_periodic", value=10))
        self.process()
        self.assertEqual(self.result()["status"], "completed")

    def test_missing_model_queues_no_inference_and_preserves_features(self):
        with closing(PeriodicSnapshotStore()) as store:
            payload = self.payload(0, sequence=0)
            store.ingest(self.principal, DEVICE, payload); store.tick()
            item = store.list_device(self.admin, DEVICE)["items"][0]
            self.assertEqual(item["window"], payload["window"])
            self.assertEqual(item["analysis"]["status"], "waiting_model")
            self.assertEqual(item["analysis"]["preparedInput"]["shape"], [9])

    def test_foreign_quality_missing_hash_and_sequence_rules_rejected_before_ack(self):
        payload = self.payload(0, sequence=0)
        mutations = [lambda p:p["window"].update(features={}), lambda p:p["window"].update(historySequence=True),
                     lambda p:p["window"]["features"].__setitem__(FEATURES[0], "0"),
                     lambda p:p["window"].update(extra=1), lambda p:p["window"].update(quality=[]),
                     lambda p:p["window"]["integrity"].update(digest="0"*64),
                     lambda p:p["transmission"].update(policyId="edge-state-snapshot-v1"),
                     lambda p:p["transmission"].update(intervalSec=300),
                     lambda p:p["window"].update(historySequence=None)]
        for mutate in mutations:
            bad = copy.deepcopy(payload); mutate(bad)
            with self.subTest(mutate=mutate), self.assertRaises(data.ApiError):
                self.send(bad)
        with self.assertRaises(data.ApiError):
            self.store.ingest({"permissions":["device-health:write"]}, DEVICE, payload)
        self.assertEqual(self.store.list_device(self.admin, DEVICE)["items"], [])

    def test_reused_history_sequence_and_clock_drift_rejected(self):
        self.send(self.payload(0, sequence=0))
        with self.assertRaises(data.ApiError):
            self.send(self.payload(25, sequence=0))
        wrong = self.payload(25, sequence=1); wrong["window"]["startUptimeUs"] += 2000000
        with self.assertRaises(data.ApiError):
            self.send(wrong)

    def test_tampered_history_never_generates_candidate(self):
        self.history(); self.send(self.payload(600, reason="anomaly_enter", value=10))
        self.process()
        self.send(self.payload(610, reason="anomaly_periodic", value=10))
        with self.store.db:
            self.store.db.execute("UPDATE periodic_snapshots SET digest=? WHERE ordinal=1", ("0"*64,))
        self.process()
        self.assertEqual(self.result()["status"], "unavailable")
        self.assertIsNone(self.result()["verdict"])

    def test_events_open_and_reference_clear_but_missing_history_does_not_close(self):
        self.history()
        self.send(self.payload(600, reason="anomaly_enter", value=10))
        self.process(); self.store.events.tick()
        event = json.loads(self.store.db.execute("SELECT payload FROM snapshot_incidents").fetchone()[0])
        self.assertEqual(event["status"], "open")
        self.assertIn("고장 확률이 아니", event["note"])
        self.assertEqual(len(event["snapshotEvidence"]["modelEvidence"]["historyDigests"]), 24)
        self.send(self.payload(601, reason="normal_recovered")); self.process(); self.store.events.tick()
        event = json.loads(self.store.db.execute("SELECT payload FROM snapshot_incidents").fetchone()[0])
        self.assertEqual((event["status"],event["endReason"]), ("closed","novelty_reference_not_exceeded"))
        self.assertFalse(self.store.events.notification_allowed(event))

    def test_http_summary_receipt_uses_existing_auth_and_new_profile(self):
        server = create_server("127.0.0.1",0,snapshot_model=self.adapter,auto_alerts=False)
        thread = threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        def post(token):
            conn = http.client.HTTPConnection(*server.server_address, timeout=10)
            try:
                conn.request("POST",f"/api/devices/{DEVICE}/periodic-snapshots",json.dumps(self.payload(0,sequence=0)),
                             {"Content-Type":"application/json","Authorization":"Bearer "+token})
                response=conn.getresponse(); return response.status,json.load(response)
            finally:
                conn.close()
        try:
            self.assertEqual(post("wrong")[0],401)
            self.assertEqual(post("demo-telemetry-ingest-token")[0],202)
            self.assertEqual(post("demo-telemetry-ingest-token")[0],200)
            self.assertFalse(server.raw_vibration.processing_enabled)
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_inspect_uses_25_second_policy_and_keeps_inference_unavailability_explicit(self):
        self.history(); self.process()
        # Inspect reads this database through explicit environment override.
        report = operations.inspect(self.path, {"PERIODIC_SNAPSHOT_DB_PATH": str(self.path/"inbox.sqlite3")}, DEVICE)
        component = report["databases"]["PERIODIC_SNAPSHOT_DB_PATH"]
        self.assertEqual(component["latest"]["expectedReportIntervalSec"],25)
        self.assertEqual(component["latest"]["reason"],"VERIFIER_REQUIRES_24_PRIOR_RECORDS")

    def test_two_sensors_same_boot_index_and_time_never_mix_histories(self):
        for i in range(24):
            left = self.payload(i*25, sequence=i)
            right = copy.deepcopy(left); right["window"]["sensorId"] = "SENSOR-03"
            # Both sensors use exactly the same boot/index/sequence. Only 02 is bound.
            if i != 10:
                self.send(left)
            ack, code = self.send(right)
            self.assertEqual(code, 202)
            self.assertEqual(ack["acknowledged"][0]["sensorId"], "SENSOR-03")
        self.send(self.payload(600,sequence=24)); self.process()
        self.assertEqual(self.result()["reason"], "VERIFIER_REQUIRES_24_PRIOR_RECORDS")
        self.assertEqual(self.result()["evidence"]["historicalRecordsUsed"], 23)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM periodic_snapshots WHERE sensor='SENSOR-03' AND status='waiting_model'").fetchone()[0],24)

    def test_unbound_sensor_cannot_supersede_or_close_bound_sensor_incident(self):
        self.history(); self.send(self.payload(600, reason="anomaly_enter", value=10))
        self.process(); self.store.events.tick()
        foreign = self.payload(601, reason="normal_recovered")
        foreign["window"]["sensorId"] = "SENSOR-03"
        self.send(foreign); self.process(); self.store.events.tick()
        event = json.loads(self.store.db.execute("SELECT payload FROM snapshot_incidents").fetchone()[0])
        self.assertEqual((event["status"],event["snapshotObservation"]),("open","observing"))
        self.assertEqual((event["sensorId"],event["verificationLabel"]),("SENSOR-02","unknown"))
        self.send(self.payload(602,reason="normal_recovered",boot="b"*32))
        self.process(); self.store.events.tick()
        event = json.loads(self.store.db.execute("SELECT payload FROM snapshot_incidents").fetchone()[0])
        self.assertEqual((event["status"],event["snapshotObservation"]),("open","unknown"))

    def test_raw_evidence_is_required_for_entry_verified_and_not_used_as_features(self):
        p = self.payload(600,reason="anomaly_enter",value=5)
        self.history(); self.send(p); self.process()
        item = self.store.list_device(self.admin, DEVICE)["items"][0]
        self.assertEqual(item["window"]["rawWindow"],p["window"]["rawWindow"])
        self.assertEqual(item["analysis"]["preparedInput"]["values"],[5]*9)
        for mutate in (lambda w:w.pop("rawWindow"),
                       lambda w:w["rawWindow"]["integrity"].update(digest="0"*64),
                       lambda w:w["rawWindow"].update(sampleCount=511),
                       lambda w:w["rawWindow"].update(gPerCount=1),
                       lambda w:w.pop("sensorId")):
            bad = self.payload(601,reason="anomaly_enter"); mutate(bad["window"])
            with self.subTest(mutate=mutate), self.assertRaises(data.ApiError):
                self.send(bad)

    def test_real_schema3_migration_preserves_old_raw_original_and_ack(self):
        from motor_diagnosis.periodic_snapshots import canonical, normalize_window
        from tests import test_periodic_snapshots as old
        payload = old.SnapshotTest.payload(self)
        w = payload["window"]; body = canonical(payload)
        captured = normalize_window(w); digest = hashlib.sha256(body.encode()).hexdigest()
        path = self.path/"schema3.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            db.executescript("""CREATE TABLE periodic_snapshot_schema(version INTEGER PRIMARY KEY);
                INSERT INTO periodic_snapshot_schema VALUES(3);
                CREATE TABLE snapshot_streams(device TEXT,boot TEXT,context TEXT,anchor_uptime INTEGER,anchor_captured REAL,PRIMARY KEY(device,boot));
                CREATE TABLE periodic_snapshots(ordinal INTEGER PRIMARY KEY AUTOINCREMENT,device TEXT,site TEXT,asset TEXT,boot TEXT,idx INTEGER,
                    uptime INTEGER,captured REAL,received REAL,digest TEXT,body TEXT,late INTEGER,quality TEXT,status TEXT,result TEXT,UNIQUE(device,boot,idx));""")
            db.execute("INSERT INTO snapshot_streams VALUES(?,?,?,?,?)",(w["deviceId"],w["bootId"],json.dumps([w[k] for k in ("siteId","assetId","profileId")]),w["startUptimeUs"],captured))
            db.execute("INSERT INTO periodic_snapshots VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (41,w["deviceId"],w["siteId"],w["assetId"],w["bootId"],w["windowIndex"],w["startUptimeUs"],captured,captured+1,digest,body,0,"valid","waiting_model",'{"status":"waiting_model"}'))
            db.commit()
        with closing(PeriodicSnapshotStore(path,model=self.adapter)) as store:
            row=store.db.execute("SELECT * FROM periodic_snapshots").fetchone()
            self.assertEqual((row["ordinal"],row["body"],row["digest"],row["sensor"]),(41,body,digest,""))
            ack, code=store.ingest(self.principal,w["deviceId"],payload)
            self.assertEqual((code,ack["accepted"]),(200,0))
            self.assertFalse(store.inference.tick())
            store.ingest(self.principal,DEVICE,self.payload(0,sequence=0))
            self.assertEqual(store.db.execute("SELECT max(ordinal) FROM periodic_snapshots").fetchone()[0],42)
            self.assertEqual(store.db.execute("PRAGMA integrity_check").fetchone()[0],"ok")


if __name__ == "__main__":
    unittest.main()
