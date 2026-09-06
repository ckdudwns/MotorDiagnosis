"""AI1 handoff -> durable backend -> explicit human review integration."""

import copy
import hashlib
import http.client
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from ai.ai1 import result_handoff as handoff
from ai.ai1.week4.ai1.tests.test_dataset_version import _draft_manifest
from ai.ai1.week4.ai1.dataset_versions.dataset_version import freeze_dataset_version
from motor_diagnosis import ai_results, data, model_registry
from motor_diagnosis.server import create_server

TOKEN = "ai1-result-test-" + "A" * 40


class AIResultTest(unittest.TestCase):
    def setUp(self):
        data.close_runtime_state()
        data.reset_runtime_state()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_path = str(self.root / "state.sqlite3")
        self.environment = patch.dict(
            os.environ,
            {
                "APP_ENV": "production",
                "AI1_RESULT_PRODUCERS_JSON": json.dumps(
                    {"AI1-TEST": {"token": TOKEN, "allowedSiteIds": ["SITE-01"]}}
                ),
                "AI1_RESULT_TOKEN": TOKEN,
                "AI1_RESULT_ALLOW_LOCAL_HTTP": "true",
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.start_server()
        self.addCleanup(self.finish)
        self.users, self.tokens = {}, {}
        for name in ("system", "admin", "operator"):
            login = data.authenticate({"username": name, "password": name + "123"})
            self.tokens[name] = login["session"]["token"]
            self.users[name] = data.current_user_for_token(self.tokens[name])
        self.frozen = freeze_dataset_version(_draft_manifest())
        self.dataset = data.create_dataset_version(
            self.users["system"],
            {
                "name": "Handoff fixture dataset",
                "source": {
                    "type": "external",
                    "uri": "https://example.org/fixture",
                    "license": "test-only",
                    "checksum": self.frozen["source"]["checksum"],
                },
                "compatibility": {
                    "signalType": ["vibration"],
                    "samplingRateHz": 12000,
                    "units": {"vibration": "g"},
                    "operatingConditions": {"rpm": 1800},
                },
                "sourceFilters": {"siteId": "SITE-01"},
                "labelTaxonomyVersion": "ACOUSTIC-V1",
                "labelMapping": {"normal": "NORMAL"},
                "split": {"train": 0.7, "validation": 0.2, "test": 0.1},
                "reason": "Test fixture, not a trained model",
            },
        )
        self.baseline = model_registry.create_baseline_version(
            self.users["system"],
            {
                "datasetId": self.dataset["id"],
                "siteId": "SITE-01",
                "assetId": "SITE-01-MOT-02",
                "features": {"rms": 0.08},
            },
        )
        self.artifact = self.root / "model.pt"
        self.artifact.write_bytes(b"Inert fixture bytes; never loaded as a model")
        self.report = {
            "id": "TJ-FIXTURE-1",
            "datasetId": self.frozen["id"],
            "datasetSnapshotDigest": self.frozen["snapshotDigest"],
            "status": "completed",
            "metrics": {
                "bestCandidate": "dense_autoencoder",
                "selectionCriterion": "validation_f1",
                "independentHoldout": False,
                "evaluation": "Fixture only, no field performance evidence",
            },
            "candidates": [
                {
                    "name": "dense_autoencoder",
                    "artifactUri": self.artifact.as_uri(),
                    "artifactChecksum": "sha256:"
                    + hashlib.sha256(self.artifact.read_bytes()).hexdigest(),
                    "validationMetrics": {"f1": 0.7},
                    "metrics": {"f1": 0.6},
                }
            ],
            "domainGap": {"note": "fixture vs field"},
            "fieldCalibrationPlan": ["현장 검증 필요"],
            "errorCases": {
                "dense_autoencoder": {
                    "false_positives": [{"sample": "fixture-A"}],
                    "false_negatives": [],
                }
            },
        }
        self.target = {
            "producerId": "AI1-TEST",
            "datasetId": self.dataset["id"],
            "datasetFingerprint": self.dataset["versionFingerprint"],
            "baselineVersion": self.baseline["version"],
        }
        self.payload = handoff.build_submission(self.report, self.frozen, self.target)
        self.pending = self.root / "outbox.json"

    def start_server(self):
        self.server = create_server(
            "127.0.0.1", 0, auto_alerts=False, state_database=self.state_path
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        os.environ["AI1_RESULT_URL"] = (
            f"http://127.0.0.1:{self.server.server_address[1]}/api/ai1/results"
        )

    def finish(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        data.reset_runtime_state()

    def request(self, path, body=None, token=TOKEN, method=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(
                method or ("POST" if body is not None else "GET"),
                path,
                body=json.dumps(body) if body is not None else None,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def submit(self, payload=None):
        return self.request("/api/ai1/results", payload or self.payload)

    def review_payload(self, model, decision="approve", request_id="review-1"):
        return {
            "decision": decision,
            "reason": "검토 근거와 한계를 확인함",
            "expectedRevision": 0,
            "registrationDigest": model["registrationDigest"],
            "requestId": request_id,
        }

    def review(self, model, payload=None, actor="admin"):
        return self.request(
            f"/api/model-versions/{model['version']}/reviews",
            payload or self.review_payload(model),
            self.tokens[actor],
        )

    def test_automatic_registration_is_pending_idempotent_and_not_deployment(self):
        status, response = self.submit()
        self.assertEqual(status, 201, response)
        model = response["model"]
        self.assertEqual(model["approvalStatus"], "pending")
        self.assertEqual(model["deploymentStatus"], "not_deployed")
        self.assertFalse(model["artifactVerified"])
        self.assertEqual(model["createdBy"], "ai1:AI1-TEST")
        self.assertEqual(model["datasetSnapshot"], self.dataset)
        before = copy.deepcopy(data.MODEL_VERSIONS), copy.deepcopy(data.AUDIT_LOGS)
        self.assertEqual(self.submit()[0], 200)
        self.assertEqual((data.MODEL_VERSIONS, data.AUDIT_LOGS), before)
        self.assertEqual(handoff.digest(self.payload), response["submissionDigest"])

    def test_producer_token_cannot_review_and_user_tokens_cannot_auto_submit(self):
        for name in self.tokens:
            self.assertEqual(
                self.request("/api/ai1/results", self.payload, self.tokens[name])[0],
                401,
            )
        _, response = self.submit()
        model = response["model"]
        self.assertEqual(self.review(model, actor="operator")[0], 403)
        self.assertEqual(
            self.request(
                f"/api/model-versions/{model['version']}/reviews",
                self.review_payload(model),
            )[0],
            401,
        )
        self.assertNotIn("model:review", data.role_policy("AI1")["permissions"])

    def test_auth_configuration_and_site_scopes_fail_closed(self):
        for config in (
            "[]",
            '{"AI1-TEST":{},"AI1-TEST":{}}',
            json.dumps({"AI1-TEST": {"token": TOKEN, "allowedSiteIds": ["*"]}}),
        ):
            with patch.dict(os.environ, {"AI1_RESULT_PRODUCERS_JSON": config}):
                status, response = self.submit()
                self.assertEqual(status, 503, response)
                self.assertNotIn(TOKEN, json.dumps(response))
        with patch.dict(
            os.environ,
            {
                "AI1_RESULT_PRODUCERS_JSON": json.dumps(
                    {"AI1-TEST": {"token": TOKEN, "allowedSiteIds": ["SITE-02"]}}
                )
            },
        ):
            self.assertEqual(self.submit()[0], 403)
        self.assertEqual(self.submit({**self.payload, "producerId": "OTHER"})[0], 403)
        self.assertEqual(data.MODEL_VERSIONS, [])

    def test_invalid_registration_and_wrong_dataset_have_no_partial_rows(self):
        for field, value, expected in (
            ("approvalStatus", "approved", 400),
            ("artifactChecksum", "bad", 400),
            ("metrics", {"f1": float("nan")}, 400),
            ("independentHoldout", 1, 400),
            ("datasetFingerprint", "sha256:" + "a" * 64, 409),
            ("datasetSourceChecksum", "sha256:" + "b" * 64, 409),
            ("artifactUri", "https://bad:99999/model", 400),
            ("errorCases", [None], 400),
        ):
            with self.subTest(field=field):
                before = copy.deepcopy(data.AUDIT_LOGS)
                status, response = self.submit({**self.payload, field: value})
                self.assertEqual(status, expected, response)
                self.assertEqual(data.MODEL_VERSIONS, [])
                self.assertEqual(data.AUDIT_LOGS, before)

    def test_same_job_content_conflict_and_version_collision(self):
        self.assertEqual(self.submit()[0], 201)
        self.assertEqual(self.submit({**self.payload, "evaluation": "changed"})[0], 409)
        self.assertEqual(self.submit({**self.payload, "jobId": "another-job"})[0], 409)
        self.assertEqual(len(data.MODEL_VERSIONS), 1)

    def test_human_approval_is_audited_once_and_survives_registration_retry(self):
        _, response = self.submit()
        model = response["model"]
        status, approved = self.review(model)
        self.assertEqual(status, 200, approved)
        self.assertEqual(approved["approvalStatus"], "approved")
        self.assertEqual(approved["reviewedBy"], self.users["admin"]["id"])
        self.assertEqual(
            approved["reviewHistory"][0]["metricSnapshot"], model["metrics"]
        )
        self.assertEqual(approved["deploymentStatus"], "not_deployed")
        self.assertFalse(approved["artifactVerified"])
        self.assertEqual(self.review(model)[1], approved)
        self.assertEqual(self.submit()[1]["model"], approved)
        self.assertEqual(
            sum(row["action"] == "model.approve" for row in data.AUDIT_LOGS), 1
        )

    def test_rejection_is_final_and_status_filters_are_real(self):
        model = self.submit()[1]["model"]
        request = self.review_payload(model, "reject")
        status, rejected = self.review(model, request)
        self.assertEqual(status, 200, rejected)
        self.assertEqual(rejected["status"], "rejected")
        for status, count in (("draft", 0), ("approved", 0), ("rejected", 1)):
            self.assertEqual(
                model_registry.versions_for(
                    self.users["operator"], "model", status=status
                )["total"],
                count,
            )
        request.update(expectedRevision=1, decision="approve", requestId="second")
        self.assertEqual(self.review(model, request)[0], 409)

    def test_review_requires_reason_exact_digest_revision_and_scope(self):
        model = self.submit()[1]["model"]
        for field, value, status in (
            ("reason", " ", 400),
            ("expectedRevision", True, 400),
            ("expectedRevision", 2, 409),
            ("registrationDigest", "sha256:" + "c" * 64, 409),
            ("decision", "deploy", 400),
        ):
            body = {**self.review_payload(model), field: value}
            self.assertEqual(self.review(model, body)[0], status)
        denied = {**self.users["admin"], "allowedSiteIds": ["SITE-02"]}
        with self.assertRaises(data.ApiError) as error:
            ai_results.review_model(
                denied, model["version"], self.review_payload(model)
            )
        self.assertEqual(error.exception.status, 403)
        self.assertEqual(data.MODEL_VERSIONS[0]["reviewHistory"], [])

    def test_changed_stored_evidence_is_not_approved(self):
        model = self.submit()[1]["model"]
        data.MODEL_VERSIONS[0]["metrics"]["test"]["f1"] = 1.0
        status, response = self.review(model)
        self.assertEqual(status, 409, response)
        self.assertEqual(response["error"]["code"], "MODEL_EVIDENCE_CHANGED")

    def test_concurrent_registration_and_opposing_reviews(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            statuses = list(executor.map(lambda _: self.submit()[0], range(4)))
        self.assertCountEqual(statuses, [201, 200, 200, 200])
        model = copy.deepcopy(data.MODEL_VERSIONS[0])
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    self.review, model, self.review_payload(model, decision, decision)
                )
                for decision in ("approve", "reject")
            ]
            self.assertCountEqual(
                [future.result()[0] for future in futures], [200, 409]
            )
        self.assertEqual(len(data.MODEL_VERSIONS[0]["reviewHistory"]), 1)

    def test_checkpoint_failure_rolls_back_registration_and_review(self):
        with patch.object(
            data._RUNTIME_STATE_STORE,
            "save",
            side_effect=sqlite3.OperationalError("injected"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                ai_results.submit_result(TOKEN, self.payload)
        self.assertEqual(data.MODEL_VERSIONS, [])
        self.assertFalse(
            any(row["action"].startswith("model.") for row in data.AUDIT_LOGS)
        )
        model = self.submit()[1]["model"]
        with patch.object(
            data._RUNTIME_STATE_STORE,
            "save",
            side_effect=sqlite3.OperationalError("injected"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                ai_results.review_model(
                    self.users["admin"], model["version"], self.review_payload(model)
                )
        self.assertEqual(data.MODEL_VERSIONS[0], model)
        self.assertEqual(self.review(model)[0], 200)

    def test_restart_retains_approval_job_identity_and_audit(self):
        model = self.submit()[1]["model"]
        approved = self.review(model)[1]
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)
        self.start_server()
        self.assertEqual(self.submit()[1]["model"], approved)
        self.assertEqual(len(data.MODEL_VERSIONS[0]["reviewHistory"]), 1)

    def test_client_outbox_real_http_and_receipt_never_contain_token(self):
        handoff.prepare_handoff(self.report, self.frozen, self.target, self.pending)
        first = self.pending.read_bytes()
        self.assertNotIn(TOKEN, first.decode())
        receipt = handoff.send_pending(self.pending, attempts=1)
        self.assertEqual(handoff.send_pending(self.pending, attempts=1), receipt)
        self.assertEqual(self.pending.read_bytes(), first)
        self.assertTrue(Path(str(self.pending) + ".receipt.json").is_file())
        self.assertEqual(len(data.MODEL_VERSIONS), 1)

    def test_client_rejects_artifact_or_manifest_tampering_and_intent_overwrite(self):
        handoff.prepare_handoff(self.report, self.frozen, self.target, self.pending)
        changed = copy.deepcopy(self.report)
        changed["metrics"]["evaluation"] = "changed"
        with self.assertRaises(ValueError):
            handoff.prepare_handoff(changed, self.frozen, self.target, self.pending)
        broken = copy.deepcopy(self.frozen)
        broken["rows"][0]["common_label"] = "ANOMALY"
        with self.assertRaises(ValueError):
            handoff.build_submission(self.report, broken, self.target)
        self.artifact.write_bytes(b"different bytes")
        with self.assertRaises(ValueError):
            handoff.build_submission(self.report, self.frozen, self.target)

    def test_client_retries_lost_ack_without_reregistering_or_retraining(self):
        handoff.prepare_handoff(self.report, self.frozen, self.target, self.pending)
        self.assertEqual(
            self.submit()[0], 201
        )  # Simulate the server commit with a lost ACK.
        receipt = handoff.send_pending(self.pending, attempts=1)
        self.assertEqual(receipt["version"], self.payload["version"])
        self.assertEqual(len(data.MODEL_VERSIONS), 1)

    def test_client_rejects_wrong_ack_and_retains_outbox(self):
        handoff.prepare_handoff(self.report, self.frozen, self.target, self.pending)
        response = Mock(status=201)
        response.read.return_value = b'{"accepted":true}'
        opened = Mock()
        opened.open.return_value.__enter__ = Mock(return_value=response)
        opened.open.return_value.__exit__ = Mock(return_value=False)
        with (
            patch.object(handoff, "build_opener", return_value=opened),
            self.assertRaises(ValueError),
        ):
            handoff.send_pending(self.pending, attempts=1)
        self.assertTrue(self.pending.is_file())
        self.assertFalse(Path(str(self.pending) + ".receipt.json").exists())

    def test_client_transient_retry_is_bounded_and_keeps_identical_intent(self):
        handoff.prepare_handoff(self.report, self.frozen, self.target, self.pending)
        original = self.pending.read_bytes()
        real = handoff.build_opener(handoff.NoRedirect())
        attempts, pauses = [], []

        def open_request(request, timeout):
            attempts.append(request.data)
            if len(attempts) == 1:
                raise HTTPError(request.full_url, 503, "temporary", {}, None)
            if len(attempts) == 2:
                raise URLError("temporary connection failure")
            return real.open(request, timeout=timeout)

        with patch.object(
            handoff, "build_opener", return_value=Mock(open=open_request)
        ):
            handoff.send_pending(self.pending, attempts=3, sleep=pauses.append)
        self.assertEqual(pauses, [1, 2])
        self.assertEqual(len(set(attempts)), 1)
        self.assertEqual(self.pending.read_bytes(), original)
        self.assertEqual(len(data.MODEL_VERSIONS), 1)

    def test_client_redirect_auth_failure_and_exhaustion_never_create_receipt(self):
        handoff.prepare_handoff(self.report, self.frozen, self.target, self.pending)
        for status, expected_attempts in (
            (302, 1),
            (401, 1),
            (409, 1),
            (429, 3),
            (503, 3),
        ):
            opened, pauses = Mock(), []
            opened.open.side_effect = lambda *args, **kwargs: self.raise_http(status)
            with (
                self.subTest(status=status),
                patch.object(handoff, "build_opener", return_value=opened),
            ):
                with self.assertRaises(RuntimeError) as caught:
                    handoff.send_pending(self.pending, attempts=3, sleep=pauses.append)
                self.assertEqual(opened.open.call_count, expected_attempts)
                self.assertEqual(len(pauses), expected_attempts - 1)
                self.assertNotIn(TOKEN, str(caught.exception))
                self.assertTrue(self.pending.is_file())
                self.assertFalse(Path(str(self.pending) + ".receipt.json").exists())
        self.assertIsNone(
            handoff.NoRedirect().redirect_request(
                None, None, 302, "", {}, "https://elsewhere.invalid"
            )
        )

    @staticmethod
    def raise_http(status):
        raise HTTPError(
            "https://example.invalid/api/ai1/results", status, "failure", {}, None
        )


if __name__ == "__main__":
    unittest.main()
