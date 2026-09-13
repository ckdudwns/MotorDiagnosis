"""Synthetic inputs, real pinned model files; no device/server or field claims."""
from contextlib import closing
import copy
from datetime import datetime, timezone
import hashlib
import http.client
import json
import math
from pathlib import Path
import tempfile
import time
import threading
import unittest
import shutil
import subprocess
import sys
from unittest.mock import patch

from motor_diagnosis import data, operations
from motor_diagnosis import edge_feature_snapshots as contract
from motor_diagnosis.periodic_snapshots import PeriodicSnapshotStore
from motor_diagnosis.pump_models import (PumpDualModels, read_forecast, predict_features,
                                         configured_model, ENV_KEYS, INPUT_MODE)
from tests.test_measured_rpm import RpmSetup
from tests.test_pump_event_model import ARTIFACT, CHECKSUM, STREAM, SCOPE, DEVICE

FORECAST = ARTIFACT.parents[1]/"pump-forecast/model.json"
FORECAST_HASH = "f91bd1b999f213e7f24b07b51ba28970b13e0dbbec7bb6afaea5100cfb04d9a6"


def environment():
    return dict(zip(ENV_KEYS, [str(ARTIFACT), CHECKSUM, str(FORECAST), FORECAST_HASH,
        STREAM, SCOPE["deviceId"], SCOPE["siteId"], SCOPE["assetId"], SCOPE["sensorId"], INPUT_MODE]))


class ForecastArtifactTest(unittest.TestCase):
    def test_documented_envelope_and_read_only_preflight(self):
        from motor_diagnosis.transmission_policy import validate_snapshot
        root = Path(__file__).resolve().parents[1]
        document = (root / "docs/pump-dual-model-serving.md").read_text(encoding="utf-8")
        payload = json.loads(document.split("```json\n", 1)[1].split("```", 1)[0])
        window = validate_snapshot(payload)
        contract.normalize(window, check_time_bounds=False)
        args = [sys.executable, "-m", "motor_diagnosis.pump_models",
                "--event-artifact", str(ARTIFACT), "--event-checksum", CHECKSUM,
                "--forecast-artifact", str(FORECAST), "--forecast-checksum", FORECAST_HASH,
                "--stream", STREAM, "--input-mode", INPUT_MODE]
        for field in ("device", "site", "asset", "sensor"):
            args.extend(["--" + field, SCOPE[field + "Id"]])
        completed = subprocess.run(args, cwd=root, capture_output=True, text=True, timeout=20)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertTrue(report["artifactsVerified"])
        self.assertFalse(report["serviceModified"])
        self.assertEqual(report["bindingId"], configured_model(environment()).binding_id)

    def test_pinned_bytes_and_explicit_complete_config(self):
        self.assertEqual(hashlib.sha256(FORECAST.read_bytes()).hexdigest(), FORECAST_HASH)
        model = configured_model(environment())
        self.assertTrue(model.matches({**SCOPE,"profileId":contract.HISTORY_PROFILE_ID}))
        self.assertFalse(model.matches({**SCOPE,"profileId":contract.PROFILE_ID}))
        self.assertFalse(model.matches({**SCOPE,"sensorId":"SENSOR-03","profileId":contract.HISTORY_PROFILE_ID}))
        for key in ENV_KEYS:
            env = environment(); env.pop(key)
            with self.subTest(key=key), self.assertRaises(ValueError):
                configured_model(env)
        for key, value in (("PUMP_DUAL_STREAM","unknown"), ("PUMP_DUAL_INPUT_MODE","approved"),
                           ("PUMP_DUAL_EVENT_CHECKSUM","0"*64), ("PUMP_DUAL_FORECAST_CHECKSUM","0"*64),
                           ("PUMP_EVENT_VERIFIER_STREAM",STREAM)):
            env=environment(); env[key]=value
            with self.subTest(key=key), self.assertRaises(ValueError):
                configured_model(env)

    def test_forecast_matches_independent_training_equation_for_both_streams(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy only needed for independent reference test")
        model = read_forecast(FORECAST, FORECAST_HASH)
        history = np.array([[2+j*.05+i*.001 for j in range(9)] for i in range(13)])
        vector = np.concatenate((history[-1],history.mean(axis=0),history.std(axis=0),history[-1]-history[0]))
        for stream in model["streams"].values():
            f=stream["forecast"]
            expected = ((vector-np.array(f["inputMean"]))/np.array(f["inputStd"])) @ np.array(f["weights"])
            expected = expected*np.array(f["targetStd"])+np.array(f["targetMean"])
            np.testing.assert_allclose(predict_features(f,history.tolist()),expected,rtol=1e-10,atol=1e-10)

    def test_invalid_dimensions_nan_duplicate_keys_and_reordered_features_rejected(self):
        original=read_forecast(FORECAST,FORECAST_HASH)
        mutations=[lambda m:m.update(intervalSec=10),lambda m:m.update(windowRows=True),
            lambda m:m.update(features=list(reversed(m["features"]))),
            lambda m:m.update(derivedFeatures=list(reversed(m["derivedFeatures"]))),
            lambda m:m["streams"][STREAM]["forecast"].update(weights=[[0]*9]*35),
            lambda m:m["streams"][STREAM]["forecast"]["inputStd"].__setitem__(0,0),
            lambda m:m["streams"][STREAM]["forecast"].update(operationallyApproved=True)]
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"bad.json"
            for change in mutations:
                model=copy.deepcopy(original); change(model); raw=json.dumps(model).encode(); path.write_bytes(raw)
                with self.subTest(change=change), self.assertRaises(ValueError):
                    read_forecast(path,hashlib.sha256(raw).hexdigest())
            for raw in (b'{"x":NaN}',b'{"schemaVersion":2,"schemaVersion":2}'):
                path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    read_forecast(path,hashlib.sha256(raw).hexdigest())


class DualServingTest(RpmSetup):
    def setUp(self):
        super().setUp()
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/"snapshots.sqlite3"
        self.base=int(time.time()-700)//25*25
        self.model=configured_model(environment())
        # Serving/lifecycle fixtures use a deterministic in-range predictor.
        # Real artifact ridge arithmetic and guard failures are tested separately.
        predictor = patch("motor_diagnosis.pump_models.predict_features",
                          return_value=list(self.model._forecast["targetMean"]))
        predictor.start(); self.addCleanup(predictor.stop)
        self.store=PeriodicSnapshotStore(self.path,model=self.model)
        self.addCleanup(lambda:self.store.close())

    def payload(self, sequence, *, event="periodic", quality="valid", scheduled=True, state=None, offset=-1., boot="a"*32):
        slot=self.base+sequence*25
        at=slot+offset if quality=="valid" else slot
        means=[1.045,1.1,1.01,0.,-.01,0.,3.,3.,3.]
        values=[m+math.sqrt(2)*self.model._forecast_stream["center"][18+j]
                *math.sin(2*math.pi*sequence/13+j) for j,m in enumerate(means)]
        features=dict(zip(contract.FEATURES,values)) if quality=="valid" else None
        a,n={"periodic":(0,0),"anomaly_start":(3,0),"anomaly_active":(4,0),"recovery":(0,5)}[event]
        return {"window":{**SCOPE,"schemaVersion":3,"bootId":boot,"windowIndex":int((at-self.base+100)*100),
            "timestamp":datetime.fromtimestamp(at,timezone.utc).isoformat(),"startUptimeUs":int((at-self.base+100)*1e6),
            "sampleRateHz":800,"sampleCount":512 if quality=="valid" else 0,"axes":["X","Y","Z"],
            "unit":"dimensionless","profileId":contract.HISTORY_PROFILE_ID,"quality":quality,
            "reason":None if quality=="valid" else "fifo_overrun","features":features,
            "periodicSlotEpoch":slot if scheduled else None,"historySequence":sequence if scheduled else None,
            "integrity":{"algorithm":"sha256","digest":contract.feature_digest(features)}},
            "transmission":{"policyId":contract.HISTORY_POLICY_ID,"eventType":event,
                "state":state or ("NORMAL" if event in ("periodic","recovery") else "ANOMALY_ACTIVE"),
                "anomalyCount":a if quality=="valid" else 0,"normalCount":n if quality=="valid" else 0}}

    def send(self, payload):
        return self.store.ingest(self.principal,DEVICE,payload)

    def process(self):
        while self.store.tick(): pass
        while self.store.inference.tick(): pass

    def latest(self):
        return self.store.list_device(self.admin,DEVICE)["items"][0]

    def history(self,count,**kwargs):
        for seq in range(count): self.send(self.payload(seq,**kwargs))
        self.process()

    def test_13_records_produce_forecast_even_while_event_history_is_insufficient(self):
        self.history(12)
        self.assertEqual(self.latest()["analysis"]["forecast"]["reason"],"FORECAST_HISTORY_INSUFFICIENT")
        self.send(self.payload(12)); self.process()
        row=self.latest(); result=row["analysis"]; f=result["forecast"]
        self.assertEqual(result["reason"],"VERIFIER_HISTORY_INSUFFICIENT")
        self.assertEqual(f["status"],"completed")
        self.assertEqual(set(f["features"]),set(contract.FEATURES))
        self.assertEqual(f["inputOrdinals"],list(range(1,14)))
        self.assertEqual(f["inputDigests"][-1],row["digest"])
        self.assertEqual(f["targetSlotEpoch"],self.base+12*25+300)
        self.assertFalse(f["affectsAlerts"])

    def test_24_prior_records_produce_both_results_and_coalesced_transition_has_no_raw(self):
        self.history(24,state="ANOMALY_ACTIVE")
        p=self.payload(24,event="anomaly_start")
        ack,code=self.send(p); self.process(); result=self.latest()["analysis"]
        self.assertEqual((code,ack["accepted"],ack["acknowledged"][0]["durablyStored"]),(202,1,True))
        self.assertEqual((result["status"],result["forecast"]["status"]),("completed","completed"))
        self.assertEqual(result["evidence"]["historyOrdinals"],list(range(1,25)))
        self.assertFalse(result["evidence"]["sourceFeatureEquivalenceVerified"])
        self.assertNotIn("samples",self.latest()["window"])
        ack2,code=self.send(p)
        self.assertEqual((code,ack2["accepted"]),(200,0)); self.assertEqual(ack2["acknowledged"],ack["acknowledged"])

    def test_immediate_event_does_not_pollute_periodic_history_or_run_forecast(self):
        self.history(24)
        self.send(self.payload(23,event="anomaly_start",scheduled=False,offset=5))
        self.process()
        self.assertEqual(self.latest()["analysis"]["status"],"completed")
        self.assertEqual(self.latest()["analysis"]["forecast"]["status"],"not_applicable")
        self.send(self.payload(24)); self.process()
        self.assertEqual(self.latest()["analysis"]["evidence"]["historyOrdinals"],list(range(1,25)))

    def test_bad_quality_missing_slot_and_reboot_break_history(self):
        for seq in range(24): self.send(self.payload(seq,quality="invalid" if seq==18 else "valid"))
        self.send(self.payload(24)); self.process()
        result=self.latest()["analysis"]
        self.assertEqual(result["reason"],"VERIFIER_HISTORY_QUALITY_OR_SCOPE")
        self.assertEqual(result["forecast"]["reason"],"FORECAST_HISTORY_QUALITY_OR_SCOPE")
        self.send(self.payload(25,boot="b"*32)); self.process()
        self.assertEqual(self.latest()["analysis"]["forecast"]["reason"],"FORECAST_HISTORY_INSUFFICIENT")

    def test_late_backfill_never_rewrites_receipt_history(self):
        for seq in range(13):
            if seq!=8:self.send(self.payload(seq))
        self.send(self.payload(8)); self.process()
        self.assertEqual(self.latest()["analysis"]["forecast"]["reason"],"FORECAST_HISTORY_INSUFFICIENT")
        self.send(self.payload(13)); self.process()
        self.assertEqual(self.latest()["analysis"]["forecast"]["status"],"completed")

    def test_skipped_slot_is_not_filled_with_repeated_or_interpolated_values(self):
        self.history(13)
        self.send(self.payload(14)); self.process()
        self.assertEqual(self.latest()["analysis"]["forecast"]["reason"],"FORECAST_HISTORY_DISCONTINUITY")

    def test_latest_window_jitter_within_one_second_is_valid_without_rewriting_timestamps(self):
        for seq in range(25): self.send(self.payload(seq,offset=-.65 if seq%2 else -1.63))
        self.process(); row=self.latest()
        self.assertEqual((row["analysis"]["status"],row["analysis"]["forecast"]["status"]),("completed","completed"))
        self.assertEqual(row["window"]["timestamp"],self.payload(24,offset=-1.63)["window"]["timestamp"])

    def test_old_cadence_stale_selection_slot_reuse_and_bad_sequences_rejected(self):
        for change in (lambda p:p["window"].update(periodicSlotEpoch=self.base+10),
                       lambda p:p["window"].update(historySequence=True),
                       lambda p:p["window"].update(historySequence=None),
                       lambda p:p["transmission"].update(policyId=contract.POLICY_ID)):
            p=self.payload(0); change(p)
            with self.subTest(change=change),self.assertRaises(data.ApiError):self.send(p)
        with self.assertRaises(data.ApiError):self.send(self.payload(0,offset=-2))
        self.send(self.payload(0))
        with self.assertRaises(data.ApiError):self.send(self.payload(0,offset=-.8))
        p=self.payload(1);p["window"]["historySequence"]=0
        with self.assertRaises(data.ApiError):self.send(p)

    def test_foreign_sensor_cannot_supply_missing_history(self):
        for seq in range(25):
            p=self.payload(seq)
            if seq==10:p["window"]["sensorId"]="SENSOR-03"
            self.send(p)
        self.process()
        self.assertEqual(self.latest()["analysis"]["reason"],"VERIFIER_HISTORY_INSUFFICIENT")

    def test_restart_retains_completed_forecast_and_model_change_fences_queued_jobs(self):
        self.history(13);before=self.latest()
        self.store.close();self.store=PeriodicSnapshotStore(self.path,model=self.model)
        self.assertEqual(self.latest(),before)
        self.send(self.payload(13));self.store.tick();self.store.close()
        other=copy.deepcopy(self.model);other.binding_id="f"*64
        self.store=PeriodicSnapshotStore(self.path,model=other)
        self.assertFalse(self.store.inference.tick())
        self.assertIsNone(self.latest()["analysis"].get("forecast"))

    def test_inspect_exposes_separate_forecast_without_raw_samples(self):
        self.history(13)
        report=operations.inspect(Path(self.temp.name),{"PERIODIC_SNAPSHOT_DB_PATH":str(self.path)},DEVICE)
        latest=report["databases"]["PERIODIC_SNAPSHOT_DB_PATH"]["latest"]
        self.assertEqual(latest["profileId"],contract.HISTORY_PROFILE_ID)
        self.assertEqual(latest["forecast"]["status"],"completed")

    def test_older_invalid_can_block_verifier_without_blocking_13_record_forecast(self):
        for seq in range(25):
            self.send(self.payload(seq, quality="invalid" if seq == 5 else "valid"))
        self.process()
        result = self.latest()["analysis"]
        self.assertEqual(result["reason"], "VERIFIER_HISTORY_QUALITY_OR_SCOPE")
        self.assertEqual(result["forecast"]["status"], "completed")

    def test_one_model_calculation_failure_does_not_block_the_other(self):
        self.history(24)
        self.send(self.payload(24))
        with patch("motor_diagnosis.pump_models.score_vector", side_effect=OverflowError):
            self.process()
        result = self.latest()["analysis"]
        self.assertEqual(result["reason"], "VERIFIER_OUTPUT_INVALID")
        self.assertIsNone(result["verdict"])
        self.assertEqual(result["forecast"]["status"], "completed")
        self.send(self.payload(25))
        with patch("motor_diagnosis.pump_models.predict_features", side_effect=ValueError):
            self.process()
        result = self.latest()["analysis"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["forecast"]["reason"], "FORECAST_OUTPUT_INVALID")
        self.assertIsNone(result["forecast"]["features"])

    def test_tampered_stored_history_cannot_produce_forecast(self):
        self.history(12)
        with self.store.db:
            self.store.db.execute("UPDATE periodic_snapshots SET digest=? WHERE ordinal=5", ("0"*64,))
        self.send(self.payload(12)); self.process()
        result = self.latest()["analysis"]
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result.get("forecast"))

    def test_new_history_http_route_preserves_auth_and_durable_retry_ack(self):
        from motor_diagnosis.server import create_server
        server = create_server("127.0.0.1", 0, snapshot_model=self.model, auto_alerts=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        payload = self.payload(0, state="ANOMALY_ACTIVE")

        def post(token):
            with closing(http.client.HTTPConnection(*server.server_address, timeout=10)) as conn:
                conn.request("POST", f"/api/devices/{DEVICE}/periodic-snapshots", json.dumps(payload),
                    {"Content-Type": "application/json", "Authorization": "Bearer " + token})
                response = conn.getresponse()
                return response.status, json.load(response)

        try:
            self.assertEqual(post("wrong")[0], 401)
            status, ack = post("demo-telemetry-ingest-token")
            self.assertEqual((status, ack["accepted"]), (202, 1))
            self.assertTrue(ack["acknowledged"][0]["durablyStored"])
            status, repeated = post("demo-telemetry-ingest-token")
            self.assertEqual((status, repeated["accepted"]), (200, 0))
            self.assertEqual(ack["acknowledged"], repeated["acknowledged"])
            response = server.periodic_snapshots.list_device(self.admin, DEVICE)
            self.assertEqual(response["modelCompatibility"]["status"], "ready")
            self.assertEqual(response["configuredModel"]["forecastModel"]["modelVersion"], "sha256:" + FORECAST_HASH)
            self.assertFalse(server.raw_vibration.processing_enabled)
        finally:
            server.shutdown(); server.server_close(); thread.join()

    @unittest.skipUnless(shutil.which("node"),"Node required for shipped UI")
    def test_real_guard_rejection_remains_visible_after_unscheduled_report(self):
        self.history(25)
        self.assertEqual(self.latest()["analysis"]["forecast"]["status"], "completed")
        blocked = self.payload(25)
        blocked["window"]["features"]["cf_a_2"] = 1000.
        blocked["window"]["integrity"]["digest"] = contract.feature_digest(blocked["window"]["features"])
        self.assertEqual(self.send(blocked)[1], 202)
        self.process()
        rejected = self.latest()
        self.assertEqual(rejected["analysis"]["forecast"]["reason"], "FORECAST_INPUT_OUT_OF_DISTRIBUTION")
        cases = [self.store.list_device(self.admin, DEVICE)]
        self.assertEqual(self.send(self.payload(26, scheduled=False, event="anomaly_active", offset=-20.))[1], 202)
        self.process()
        self.assertEqual(self.latest()["analysis"]["forecast"]["reason"], "FORECAST_REQUIRES_SCHEDULED_RECORD")
        after = self.store.list_device(self.admin, DEVICE)
        self.assertEqual(next(r for r in after["items"] if r["ordinal"] == rejected["ordinal"]), rejected)
        cases.append(after)
        self.assert_dashboard({"snapshots": [], "blockedForecasts": cases})

    @unittest.skipUnless(shutil.which("node"),"Node required for shipped UI")
    def test_backfilled_history_forecast_is_visible_using_real_api_response(self):
        for seq in range(13):
            if seq != 8:
                self.send(self.payload(seq))
        self.send(self.payload(8)); self.process()
        before = self.store.list_device(self.admin, DEVICE)
        self.assertEqual(before["items"][0]["analysis"]["forecast"]["reason"], "FORECAST_HISTORY_INSUFFICIENT")
        self.send(self.payload(13)); self.process()
        after = self.store.list_device(self.admin, DEVICE)
        forecast = after["items"][0]["analysis"]["forecast"]
        self.assertEqual(forecast["status"], "completed")
        self.assertEqual(forecast["inputOrdinals"], [2,3,4,5,6,7,8,13,9,10,11,12,14])
        # The earlier receipt stays unchanged; only subsequent inference can use backfill.
        saved = next(r for r in after["items"] if r["ordinal"] == before["items"][0]["ordinal"])
        self.assertEqual(saved["analysis"], before["items"][0]["analysis"])
        self.assert_dashboard({"snapshots": [before, after]})

    @unittest.skipUnless(shutil.which("node"),"Node required for shipped UI")
    def test_dual_model_open_and_reference_clear_notifications_use_actual_event_metadata(self):
        self.store.events.mode = "alerts"
        self.history(24)
        notifications = []
        # Control the decision only: test the actual persistence/event/rendering chain.
        for seq, score, state in ((24, 2., "open"), (25, 0., "closed")):
            self.store.events.clock = lambda seq=seq: self.base + seq*25
            self.send(self.payload(seq))
            with patch("motor_diagnosis.pump_models.score_vector", return_value=(score, score, score)):
                self.process()
            self.store.events.tick()
            event = json.loads(self.store.db.execute("SELECT payload FROM snapshot_incidents").fetchone()[0])
            self.assertEqual(event["snapshotTransition"], state)
            self.assertTrue(self.store.events.notification_allowed(event))
            self.assertEqual(event["snapshotEvidence"]["inference"]["model"], self.model.metadata())
            if state == "closed":
                self.assertEqual(event["endReason"], "novelty_reference_not_exceeded")
            notifications.append({"event": event, "deliveredAt": self.payload(seq)["window"]["timestamp"]})
        self.assert_dashboard({"snapshots": [], "notifications": notifications})

    @unittest.skipUnless(shutil.which("node"),"Node required for shipped UI")
    def test_real_api_results_render_forecast_independently_of_event_warmup(self):
        cases=[]
        self.history(12)
        cases.append(self.store.list_device(self.admin,DEVICE))
        self.send(self.payload(12));self.process()
        cases.append(self.store.list_device(self.admin,DEVICE))
        for seq in range(13,25):self.send(self.payload(seq))
        self.process()
        for mode in ("shadow","events","alerts"):
            self.store.events.mode=mode
            cases.append(self.store.list_device(self.admin,DEVICE))
        self.assert_dashboard(cases)

    def assert_dashboard(self, cases):
        result=subprocess.run([shutil.which("node"),"tests/pump_dual_dashboard_fixture.mjs"],
            cwd=Path(__file__).resolve().parents[1],input=json.dumps(cases),
            capture_output=True,text=True,encoding="utf-8",timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__=="__main__":unittest.main()
