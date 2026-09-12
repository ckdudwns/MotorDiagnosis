"""Feature-only wire/receipt regressions. All measurements and predictors are synthetic."""
from contextlib import closing
import copy
from datetime import datetime, timezone
import http.client
import json
import re
from pathlib import Path
import tempfile
import threading
import time
from unittest import mock

from motor_diagnosis import data, operations
from motor_diagnosis import edge_feature_snapshots as contract
from motor_diagnosis.periodic_snapshots import PeriodicSnapshotStore
from motor_diagnosis.pump_event_model import PumpEventModel
from motor_diagnosis.server import create_server
from motor_diagnosis.snapshot_model import SnapshotModelAdapter
from motor_diagnosis.transmission_policy import snapshot_policy_metadata
from tests.test_measured_rpm import RpmSetup
from tests.test_pump_event_model import ARTIFACT, CHECKSUM, STREAM, SCOPE, DEVICE


def model_metadata():
    return {"contractId": contract.CONTRACT_ID, "modelId": "test-only-feature-model",
            "modelVersion": "test-v1", "preprocessingVersion": "test-v1",
            "inputContract": copy.deepcopy(contract.INPUT_CONTRACT), "scope": SCOPE,
            "scoreType": "test_score", "threshold": .5, "comparison": ">"}


class EdgeFeatureSnapshotTest(RpmSetup):
    def setUp(self):
        super().setUp()
        self.base = int(time.time()-120)//60*60
        self.store = PeriodicSnapshotStore()
        self.addCleanup(self.store.close)

    def payload(self, offset=25, event="periodic", *, quality="valid", reason="fifo_overrun", slot=None):
        slot = self.base+offset if event == "periodic" else slot
        captured = slot-1 if slot is not None and quality == "valid" else slot if slot is not None else self.base+offset
        a, n = {"periodic": (0, 0), "anomaly_start": (3, 0),
                "anomaly_active": (4, 0), "recovery": (0, 5)}[event]
        features = dict(zip(contract.FEATURES, [2.,2.,2.,0.,0.,0.,3.,3.,3.])) if quality == "valid" else None
        w = {**SCOPE, "schemaVersion": 2, "bootId": "a"*32, "windowIndex": int(offset*10),
             "timestamp": datetime.fromtimestamp(captured, timezone.utc).isoformat(),
             "startUptimeUs": int((captured-self.base+100)*1e6),
             "sampleRateHz": 800, "sampleCount": 512 if quality == "valid" else 0,
             "profileId": contract.PROFILE_ID, "axes": ["X", "Y", "Z"], "unit": "dimensionless",
             "quality": quality, "reason": None if quality == "valid" else reason,
             "features": features, "periodicSlotEpoch": slot,
             "integrity": {"algorithm": "sha256", "digest": contract.feature_digest(features)}}
        return {"window": w, "transmission": {"policyId": contract.POLICY_ID, "eventType": event,
                "state": "NORMAL" if event in ("periodic", "recovery") else "ANOMALY_ACTIVE",
                "anomalyCount": a if quality == "valid" else 0, "normalCount": n if quality == "valid" else 0}}

    def send(self, payload, store=None):
        return (store or self.store).ingest(self.principal, DEVICE, payload)

    def process(self, store=None):
        store = store or self.store
        while store.tick():
            pass
        while store.inference.tick():
            pass
        store.events.tick()

    def view(self, store=None):
        return (store or self.store).list_device(self.admin, DEVICE)

    def test_utc_00_25_50_cycle_is_not_a_uniform_25_second_history(self):
        for offset in (0,25,50,60,85,110,120):
            ack, code = self.send(self.payload(offset))
            self.assertEqual((code, ack["accepted"]), (202,1))
        self.process()
        view = self.view()
        self.assertEqual(view["statuses"], {"waiting_model":7})
        self.assertEqual(view["preferredPolicyId"], contract.POLICY_ID)
        meta = next(p for p in snapshot_policy_metadata() if p["policyId"] == contract.POLICY_ID)
        self.assertEqual(meta["normalIntervalsSec"], [25,25,10])
        self.assertFalse(meta["periodicDuringAnomaly"])
        for item in view["items"]:
            prepared = item["analysis"]["preparedInput"]
            self.assertEqual(len(prepared["values"]),9)
            self.assertEqual(prepared["momentConvention"], "population-pearson")
            self.assertNotIn("historyRows",prepared)
            self.assertNotIn("samples",item["window"])

    def test_documented_request_is_valid_with_matching_binary_digest(self):
        document=(Path(__file__).resolve().parents[1]/"docs/edge-feature-snapshots.md").read_text(encoding="utf-8")
        example=json.loads(re.search(r"```json\n(.*?)\n```",document,re.S).group(1))
        contract.validate_delivery(example["transmission"],example["window"])
        captured=contract.normalize(example["window"],check_time_bounds=False)
        self.assertEqual(example["window"]["periodicSlotEpoch"]-captured,1)

    def test_boundary_adjacent_windows_with_rfc3339_offsets_use_one_slot_identity(self):
        p=self.payload(0)
        p["window"]["timestamp"]=datetime.fromtimestamp(self.base-10,timezone.utc).isoformat()
        self.send(p)
        p=self.payload(25)
        from datetime import timedelta
        p["window"]["timestamp"]=datetime.fromtimestamp(self.base+24,timezone(timedelta(hours=9))).isoformat()
        # Keep the acquisition clock anchored to the same elapsed time.
        p["window"]["startUptimeUs"]+=9000000
        self.send(p)
        self.assertEqual(len(self.view()["items"]),2)

    def test_raw_is_not_required_for_entry_and_not_stored_for_any_report(self):
        for offset, event in ((1,"anomaly_start"),(11,"anomaly_active"),(21,"anomaly_active"),(23,"recovery"),(25,"periodic")):
            self.send(self.payload(offset,event))
        self.process()
        self.assertEqual(self.view()["statuses"], {"waiting_model":5})
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM snapshot_incidents").fetchone()[0],0)
        for event in ("periodic","anomaly_start","anomaly_active","recovery"):
            for key in ("rawWindow","samples","encoding"):
                payload=self.payload(50,event); payload["window"][key]="not-allowed"
                with self.subTest(event=event,key=key), self.assertRaises(data.ApiError):
                    self.send(payload)

    def test_invalid_has_no_features_no_transition_and_exact_reason(self):
        for offset, reason in zip((0,25,50,60,85,110,120), sorted(contract.INVALID_REASONS)):
            payload=self.payload(offset,quality="invalid",reason=reason)
            self.send(payload)
        self.process()
        self.assertEqual(self.view()["statuses"], {"unavailable":7})
        for item in self.view()["items"]:
            self.assertIsNone(item["window"]["features"])
            self.assertEqual(item["analysis"]["reason"],item["window"]["reason"])
            self.assertIsNone(item["analysis"]["verdict"])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM snapshot_inference_jobs").fetchone()[0],0)
        for event in ("anomaly_start","recovery"):
            with self.assertRaises(data.ApiError):
                self.send(self.payload(125,event,quality="invalid"))

    def test_malformed_contracts_reject_before_any_write(self):
        changes = [lambda p:p["window"].update(schemaVersion=1), lambda p:p["window"].update(sampleRateHz=True),
            lambda p:p["window"].update(sampleCount=511), lambda p:p["window"].update(axes=["Z","Y","X"]),
            lambda p:p["window"].update(unit="g"), lambda p:p["window"].update(features=None),
            lambda p:p["window"].update(reason="fifo_overrun"), lambda p:p["window"].update(quality=[]),
            lambda p:p["window"].update(historySequence=1), lambda p:p["window"].update(sensorId="sensor2"),
            lambda p:p["window"]["features"].update(cf_a_1=float("nan")),
            lambda p:p["window"]["features"].update(cf_a_1=float("inf")),
            lambda p:p["window"]["features"].update(cf_a_1=True),
            lambda p:p["window"]["integrity"].update(digest="0"*64),
            lambda p:p["transmission"].update(state="ANOMALY_ACTIVE"),
            lambda p:p["transmission"].update(anomalyCount=3),
            lambda p:p["transmission"].update(eventType="anomaly_enter"),
            lambda p:p["transmission"].update(intervalSec=25),
            lambda p:p["transmission"].update(policyId="pump-verifier-history-v1")]
        for mutate in changes:
            payload=self.payload();mutate(payload)
            with self.subTest(mutate=mutate),self.assertRaises(data.ApiError):
                self.send(payload)
        self.assertEqual(self.view()["items"],[])

    def test_invalid_reports_reject_stale_feature_reuse_and_counters(self):
        for event in ("periodic","anomaly_active"):
            for change in ("features","counter","reason"):
                p=self.payload(event=event,quality="invalid")
                if change=="features":
                    p["window"]["features"]=self.payload()["window"]["features"]
                    p["window"]["integrity"]["digest"]=contract.feature_digest(p["window"]["features"])
                elif change=="counter":
                    p["transmission"]["anomalyCount"]=1
                else:
                    p["window"]["reason"]=None
                with self.subTest(event=event,change=change),self.assertRaises(data.ApiError):
                    self.send(p)

    def test_slot_boundary_and_full_window_coverage_validation(self):
        for slot in (None,True,self.base+26,self.base+25.0):
            p=self.payload();p["window"]["periodicSlotEpoch"]=slot
            with self.assertRaises(data.ApiError):self.send(p)
        for offset, shift in ((0,-11),(25,-26),(50,-26),(25,-.3),(25,1)):
            p=self.payload(offset);slot=p["window"]["periodicSlotEpoch"]
            p["window"]["timestamp"]=datetime.fromtimestamp(slot+shift,timezone.utc).isoformat()
            with self.assertRaises(data.ApiError):self.send(p)
        p=self.payload(25,quality="invalid")
        p["window"]["timestamp"]=datetime.fromtimestamp(self.base+24,timezone.utc).isoformat()
        with self.assertRaises(data.ApiError):self.send(p)

    def test_transition_wins_slot_and_conflicting_second_report_rejected(self):
        p=self.payload(25,"anomaly_start",slot=self.base+25)
        ack,code=self.send(p)
        self.assertEqual(code,202)
        self.assertEqual(ack["acknowledged"][0]["eventType"],"anomaly_start")
        periodic=self.payload(25);periodic["window"]["windowIndex"]+=1
        with self.assertRaises(data.ApiError) as caught:self.send(periodic)
        self.assertEqual(caught.exception.code,"SNAPSHOT_SLOT_CONFLICT")
        self.assertEqual(self.send(p)[1],200)

    def test_ack_duplicate_after_restart_and_old_retry_preserves_original(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"snapshots.sqlite3";p=self.payload()
            with closing(PeriodicSnapshotStore(path)) as store:
                first,code=self.send(p,store)
                self.assertEqual(code,202)
                a=first["acknowledged"][0]
                self.assertTrue(a["durablyStored"])
                self.assertEqual(a["featureDigest"],p["window"]["integrity"]["digest"])
                self.assertEqual((a["sensorId"],a["bootId"],a["windowIndex"]),
                                 (SCOPE["sensorId"],p["window"]["bootId"],p["window"]["windowIndex"]))
            with closing(PeriodicSnapshotStore(path)) as store:
                with mock.patch("motor_diagnosis.edge_feature_snapshots.time.time",return_value=time.time()+3*86400):
                    replay,code=self.send(p,store)
                self.assertEqual((code,replay["accepted"],replay["duplicate"]),(200,0,True))
                self.assertEqual(replay["acknowledged"],first["acknowledged"])
                p["transmission"]["normalCount"]=1
                with self.assertRaises(data.ApiError) as caught:self.send(p,store)
                self.assertEqual(caught.exception.code,"SNAPSHOT_CONFLICT")

    def test_sensor_and_boot_identity_are_separate_and_clock_cannot_drift(self):
        self.send(self.payload())
        other=self.payload();other["window"]["sensorId"]="SENSOR-03";self.send(other)
        boot=self.payload();boot["window"]["bootId"]="b"*32;self.send(boot)
        drift=self.payload(50);drift["window"]["startUptimeUs"]+=2000000
        with self.assertRaises(data.ApiError) as caught:self.send(drift)
        self.assertEqual(caught.exception.code,"TIMESTAMP_UPTIME_MISMATCH")
        self.assertEqual(len(self.view()["items"]),3)

    def test_source_history_model_is_incompatible_not_relabelled_or_automatically_run(self):
        self.store.inference.model=PumpEventModel(ARTIFACT,CHECKSUM,STREAM,SCOPE)
        self.send(self.payload())
        self.process()
        view=self.view();a=view["items"][0]["analysis"]
        self.assertEqual((a["status"],a["reason"]),("unavailable","MODEL_INPUT_CONTRACT_MISMATCH"))
        self.assertEqual(a["inputPreparation"]["status"],"ready")
        self.assertEqual(a["preparedInput"]["shape"],[9])
        self.assertFalse(view["inferenceEnabled"])
        self.assertEqual(view["modelStatus"],"input_contract_mismatch")
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM snapshot_inference_jobs").fetchone()[0],0)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM snapshot_incidents").fetchone()[0],0)

    def test_receipt_binding_is_not_changed_by_later_model_assignment(self):
        predict=mock.Mock(return_value={"score":1.})
        self.send(self.payload())
        self.store.inference.model=SnapshotModelAdapter(model_metadata(),predict)
        self.process()
        self.assertEqual(self.view()["items"][0]["analysis"]["status"],"waiting_model")
        predict.assert_not_called()
        self.send(self.payload(50));self.process()
        self.assertEqual(self.view()["items"][0]["analysis"]["status"],"completed")
        self.assertEqual(predict.call_count,1)

    def test_corrupt_receipt_cannot_stall_all_preparation(self):
        self.send(self.payload())
        with self.store.db:
            self.store.db.execute("UPDATE periodic_snapshots SET result='not-json' WHERE ordinal=1")
        self.send(self.payload(50));self.process()
        self.assertEqual(self.view()["statuses"],{"unavailable":1,"waiting_model":1})
        self.assertEqual(self.view()["items"][1]["analysis"]["reason"],"INPUT_PREPARATION_FAILED")

    def test_future_compatible_adapter_events_and_invalid_does_not_clear_incident(self):
        predict=mock.Mock(return_value={"score":.8})
        self.store.inference.model=SnapshotModelAdapter(model_metadata(),predict)
        def step(offset,event,**kwargs):
            p=self.payload(offset,event,**kwargs)
            self.store.events.clock=lambda: data.parse_rfc3339("timestamp",p["window"]["timestamp"]).timestamp()+1
            self.send(p);self.process()
        step(1,"anomaly_start")
        self.assertEqual(self.store.db.execute("SELECT status FROM snapshot_incidents").fetchone()[0],"open")
        step(11,"anomaly_active",quality="invalid",reason="timeout")
        self.assertEqual(self.store.db.execute("SELECT status FROM snapshot_incidents").fetchone()[0],"open")
        predict.return_value={"score":.1}
        step(21,"recovery")
        self.assertEqual(self.store.db.execute("SELECT status FROM snapshot_incidents").fetchone()[0],"closed")
        self.assertEqual(predict.call_count,2)
        context=predict.call_args.args[1]
        self.assertEqual(context["sensorId"],SCOPE["sensorId"])
        self.assertEqual(context["eventType"],"recovery")

    def test_foreign_sensor_never_runs_bound_feature_model(self):
        predict=mock.Mock(return_value={"score":1.})
        self.store.inference.model=SnapshotModelAdapter(model_metadata(),predict)
        p=self.payload();p["window"]["sensorId"]="SENSOR-03"
        self.send(p);self.process()
        self.assertEqual(self.view()["items"][0]["analysis"]["reason"],"MODEL_SCOPE_MISMATCH")
        predict.assert_not_called()

    def test_http_uses_existing_ingest_permission_and_durable_ack(self):
        server=create_server("127.0.0.1",0,auto_alerts=False)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def post(token,p):
            with closing(http.client.HTTPConnection(*server.server_address,timeout=10)) as conn:
                conn.request("POST",f"/api/devices/{DEVICE}/periodic-snapshots",json.dumps(p),
                             {"Content-Type":"application/json","Authorization":"Bearer "+token})
                response=conn.getresponse();return response.status,json.load(response)
        try:
            p=self.payload()
            self.assertEqual(post("wrong",p)[0],401)
            code,ack=post("demo-telemetry-ingest-token",p)
            self.assertEqual(code,202);self.assertTrue(ack["acknowledged"][0]["durablyStored"])
            self.assertEqual(post("demo-telemetry-ingest-token",p)[0],200)
            p=self.payload(50);p["window"]["samples"]="forbidden"
            self.assertEqual(post("demo-telemetry-ingest-token",p)[0],400)
            self.assertFalse(server.raw_vibration.processing_enabled)
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_inspect_reports_model_contract_mismatch_not_sensor_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"snapshots.sqlite3"
            model=PumpEventModel(ARTIFACT,CHECKSUM,STREAM,SCOPE)
            with closing(PeriodicSnapshotStore(path,model=model)) as store:
                self.send(self.payload(),store);self.process(store)
                report=operations.inspect(Path(folder),{"PERIODIC_SNAPSHOT_DB_PATH":str(path)},DEVICE)
            codes={issue["code"] for issue in report["issues"]}
            self.assertIn("PERIODIC_SNAPSHOT_MODEL_INPUT_CONTRACT_MISMATCH",codes)
            self.assertNotIn("PERIODIC_SNAPSHOT_INPUT_UNAVAILABLE",codes)
            latest=report["databases"]["PERIODIC_SNAPSHOT_DB_PATH"]["latest"]
            self.assertEqual((latest["eventType"],latest["qualityReason"]),("periodic",None))
