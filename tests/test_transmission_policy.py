"""Adaptive batch/priority integration without any live device or server writes."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import http.client
import threading

from motor_diagnosis import data
from motor_diagnosis.raw_vibration import RawVibrationStore
from motor_diagnosis.transmission_policy import POLICY_ID, validate_batch
from motor_diagnosis.server import create_server
from motor_diagnosis import operations
from tests.test_measured_rpm import RpmSetup
from tests.test_raw_vibration import RawWindowTest, DEVICE
from tests.test_rf66 import FakeRF


class TransmissionTest(RpmSetup):
    window = RawWindowTest.window

    def setUp(self):
        super().setUp()
        self.started = datetime.now(timezone.utc)-timedelta(seconds=15)
        self.model = FakeRF()
        self.store = RawVibrationStore(model=self.model, event_mode="events")
        self.addCleanup(self.store.close)

    def batch(self, indices, mode="periodic", **metadata):
        return {"windows": [self.window(i) for i in indices], "transmission": {
            "policyId": POLICY_ID, "mode": mode, "reason": "rms_high" if mode=="priority" else "none",
            "baselineId": "motor-normal-v1", "droppedWindows": 0, **metadata}}

    def send(self, indices, mode="periodic", **metadata):
        return self.store.ingest(self.principal, DEVICE, self.batch(indices, mode, **metadata))

    def drain(self):
        while self.store.tick():
            pass

    def result(self, index):
        return json.loads(self.store.db.execute("SELECT result FROM vibration_windows WHERE idx=?", (index,)).fetchone()[0])

    def test_priority_then_archival_backfill_keeps_highwater_and_all_raw(self):
        self.send([10,11,12], "priority"); self.drain()
        before=self.store.db.execute("SELECT payload FROM rf66_event_state").fetchone()[0]
        self.send([0,1,2,3]); self.drain()
        self.assertEqual(self.store.db.execute("SELECT idx FROM window_streams").fetchone()[0],12)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM vibration_windows").fetchone()[0],7)
        self.assertEqual(self.result(2)["confirmation"]["decision"],1)
        self.assertEqual(self.result(2)["eventLifecycle"]["reason"],"HISTORICAL_DELIVERY")
        self.assertEqual(before,self.store.db.execute("SELECT payload FROM rf66_event_state").fetchone()[0])
        self.assertEqual(self.store.list_device(self.admin,DEVICE)["items"][0]["window"]["windowIndex"],12)

    def test_backlog_does_not_block_priority_inference(self):
        self.send([0,1,2,3]); self.send([10,11,12],"priority")
        self.assertTrue(self.store.tick())
        self.assertEqual(self.result(10)["status"],"completed")
        self.assertIsNone(self.store.db.execute("SELECT result FROM vibration_windows WHERE idx=0").fetchone()[0])
        self.drain()
        self.assertEqual(self.result(12)["confirmation"]["decision"],1)

    def test_archival_between_live_windows_does_not_reset_live_confirmation(self):
        self.send([10],"priority"); self.drain()
        self.send([0,1]); self.drain()
        self.send([11,12],"priority"); self.drain()
        self.assertEqual(self.result(12)["confirmation"]["decision"],1)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM rf66_incidents").fetchone()[0],1)

    def test_legacy_out_of_order_and_mutated_duplicate_still_rejected(self):
        self.send([10],"priority")
        with self.assertRaises(data.ApiError):
            self.store.ingest(self.principal,DEVICE,{"windows":[self.window(0)]})
        payload=self.batch([10]); payload["windows"][0]["quality"]="fifo_overrun"
        with self.assertRaises(data.ApiError): self.store.ingest(self.principal,DEVICE,payload)
        ack,status=self.send([10],"replay")
        self.assertEqual((ack["accepted"],status),(0,200))
        self.assertEqual(ack["acknowledged"][0]["windowIndex"],10)

    def test_stale_priority_is_history_and_cannot_close_live_incident(self):
        self.send([10,11,12],"priority"); self.drain()
        before=self.store.db.execute("SELECT payload FROM rf66_event_state").fetchone()[0]
        self.model.score=0
        old=self.batch([0,1,2],"priority")
        for w in old["windows"]:
            w["bootId"]="a"*32
            w["timestamp"]=(datetime.now(timezone.utc)-timedelta(minutes=5)+timedelta(seconds=w["windowIndex"]*.64)).isoformat()
        self.store.ingest(self.principal,DEVICE,old); self.drain()
        self.assertEqual(before,self.store.db.execute("SELECT payload FROM rf66_event_state").fetchone()[0])

    def test_strict_metadata_and_atomic_failure(self):
        for change in ({"mode":"unknown"},{"reason":[]},{"droppedWindows":True},
                       {"droppedWindows":-1},{"baselineId":""},{"policyId":"v2"}):
            with self.subTest(change=change), self.assertRaises(data.ApiError):
                validate_batch(self.batch([0],**change))
        self.send([10],"priority")
        payload=self.batch([0,1]); payload["windows"][1]["startUptimeUs"]=100000000
        with self.assertRaises(data.ApiError): self.store.ingest(self.principal,DEVICE,payload)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM raw_delivery").fetchone()[0],1)

    def test_archival_cannot_falsify_neighbor_chronology(self):
        self.send([10],"priority"); self.send([2])
        payload=self.batch([1]); payload["windows"][0]["startUptimeUs"]=1300000
        with self.assertRaises(data.ApiError): self.store.ingest(self.principal,DEVICE,payload)

    def test_older_archive_still_builds_its_own_three_window_chain(self):
        self.send([10,11,12]); self.drain()
        self.send([0,1]); self.drain(); self.send([2,3]); self.drain()
        self.assertEqual(self.result(2)["confirmation"]["decision"],1)
        self.assertEqual(self.result(3)["confirmation"]["validWindows"],3)
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM rf66_incidents").fetchone()[0],0)

    def test_restart_retains_delivery_metadata_idempotency_and_lane_heads(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/"raw.db"
            self.store.close()
            self.store=RawVibrationStore(db,model=self.model,event_mode="events")
            self.send([10,11],"priority"); self.drain(); self.store.close()
            self.store=RawVibrationStore(db,model=self.model,event_mode="events")
            self.addCleanup(self.store.close)
            self.send([0,1]); self.drain()
            self.send([12],"priority"); self.drain()
            self.assertEqual(self.result(12)["confirmation"]["decision"],1)
            self.assertEqual(self.send([10],"replay")[1],200)
            self.store.close()

    def test_http_priority_and_backfill_return_real_storage_ack(self):
        server=create_server("127.0.0.1",0,demo_enabled=False,auto_alerts=False)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            for indices,mode,expected in (([10,11,12,13],"priority",202),([0,1,2,3],"periodic",202),([0,1,2,3],"replay",200)):
                conn=http.client.HTTPConnection(*server.server_address,timeout=10)
                try:
                    conn.request("POST",f"/api/devices/{DEVICE}/raw-vibration-windows",json.dumps(self.batch(indices,mode)),
                        {"Authorization":"Bearer demo-telemetry-ingest-token","Content-Type":"application/json"})
                    response=conn.getresponse(); ack=json.load(response)
                    self.assertEqual(response.status,expected,ack)
                    self.assertEqual([i["windowIndex"] for i in ack["acknowledged"]],indices)
                    self.assertEqual(ack["deviceId"],DEVICE)
                    self.assertTrue(all(len(i["digest"])==64 for i in ack["acknowledged"]))
                finally: conn.close()
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_inspection_uses_periodic_cadence_and_received_processing_age(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"raw.db"
            store=RawVibrationStore(path,model=self.model)
            try:
                self.started=datetime.now(timezone.utc)-timedelta(seconds=250)
                store.ingest(self.principal,DEVICE,self.batch([0]))
                env={"RAW_VIBRATION_WINDOW_DB_PATH":str(path)}
                now=datetime.now(timezone.utc).timestamp()
                report=operations.inspect(Path(tmp),env,DEVICE,now=now)
                codes=[i["code"] for i in report["issues"]]
                self.assertNotIn("RAW_INPUT_STALE_OR_FUTURE",codes)
                self.assertNotIn("RAW_VIBRATION_WINDOW_DB_PATH:PROCESSING_BACKLOG",codes)
                self.assertEqual(report["databases"]["RAW_VIBRATION_WINDOW_DB_PATH"]["latest"]["maxExpectedAgeSec"],360)
                report=operations.inspect(Path(tmp),env,DEVICE,now=now+120)
                self.assertIn("RAW_INPUT_STALE_OR_FUTURE",[i["code"] for i in report["issues"]])
                self.assertIn("RAW_VIBRATION_WINDOW_DB_PATH:PROCESSING_BACKLOG",[i["code"] for i in report["issues"]])
            finally: store.close()
