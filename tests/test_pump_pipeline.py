"""Pump experiment leakage, gap and input-boundary regressions."""

from datetime import datetime, timedelta, timezone
import unittest
from pathlib import Path
import tempfile
from zipfile import ZipFile

import numpy as np

from ai.ai2.pump_pipeline import (
    audit,
    experiment,
    fit_model,
    predict,
    temporal_windows,
    timestamp,
    read_workbook,
    replay_events,
)


class PumpPipelineTest(unittest.TestCase):
    def test_xlsx_sparse_cells_and_formula_rejection(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "fixture.xlsx"

            def write(formula=False):
                with ZipFile(path, "w") as archive:
                    archive.writestr(
                        "xl/workbook.xml",
                        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="sensor" r:id="rId1"/></sheets></workbook>',
                    )
                    archive.writestr(
                        "xl/_rels/workbook.xml.rels",
                        '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
                    )
                    archive.writestr(
                        "xl/worksheets/sheet1.xml",
                        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row><c r="A1" t="inlineStr"><is><t>_document_id</t></is></c><c r="C1" t="inlineStr"><is><t>이벤트</t></is></c></row><row><c r="A2" t="inlineStr"><is><t>id1</t></is></c>'
                        + ('<c r="C2"><f>1+1</f><v>2</v></c>' if formula else "")
                        + "</row></sheetData></worksheet>",
                    )

            write()
            self.assertEqual(
                read_workbook(path), {"sensor": [{"_document_id": "id1", "이벤트": ""}]}
            )
            write(True)
            with self.assertRaises(ValueError):
                read_workbook(path)

    def test_replay_gap_breaks_duration(self):
        start = datetime(2026, 9, 1, tzinfo=timezone.utc)

        def points(seconds):
            return [
                {
                    "sensorId": "S1",
                    "timestamp": (start + timedelta(seconds=s)).isoformat(),
                    "anomalyScore": 95,
                }
                for s in seconds
            ]

        continuous = replay_events(points(range(0, 40, 5)))
        self.assertTrue(
            any(item["kind"] == "asset_event_started" for item in continuous)
        )
        broken = replay_events(points([0, 5, 10, 15, 60, 65, 70, 75]))
        self.assertFalse(any(item["kind"] == "asset_event_started" for item in broken))

    def records(self, count=300):
        start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        return [
            {
                "_document_id": str(i),
                "createdAt": (start + timedelta(seconds=5 * i)).isoformat(),
                "receivedAt": (start + timedelta(seconds=5 * i + 1)).isoformat(),
                "rms_a_1": str(5 + np.sin(i / 7)),
                "mqtt_topic": "sensor/1",
                "이벤트": "",
            }
            for i in range(count)
        ]

    def test_naive_time_rejected(self):
        with self.assertRaises(ValueError):
            timestamp("2026-09-01T12:00:00")

    def test_missing_labels_remain_unlabeled(self):
        result = audit({"sensor": self.records()})["sheets"]["sensor"]
        self.assertEqual(result["labels"]["이벤트"], {"": 300})
        self.assertEqual(result["medianIntervalSec"], 5)

    def test_constants_not_given_fabricated_scale(self):
        with self.assertRaises(ValueError):
            fit_model(np.ones((40, 2)))

    def test_batch_and_single_prediction_agree(self):
        values = np.random.default_rng(4).normal(size=(100, 3))
        model = fit_model(values[:60])
        batch = predict(model, values[60:])
        single = predict(model, values[60:61])
        self.assertAlmostEqual(batch[0][0], single[0][0])
        self.assertAlmostEqual(batch[1][0], single[1][0])
        with self.assertRaises(ValueError):
            predict(model, np.array([[np.nan] * 3]))

    def test_test_period_does_not_change_model(self):
        rows = self.records()
        model1, _, _ = experiment({"sensor": rows}, ["rms_a_1"])
        changed = [dict(row) for row in rows]
        for row in changed[250:]:
            row["rms_a_1"] = "1000"
        model2, _, _ = experiment({"sensor": changed}, ["rms_a_1"])
        self.assertEqual(model1, model2)

    def test_duplicate_identity_and_time_rejected(self):
        for field in ("_document_id", "createdAt"):
            with self.subTest(field=field):
                rows = self.records()
                rows[1][field] = rows[0][field]
                with self.assertRaises(ValueError):
                    experiment({"sensor": rows}, ["rms_a_1"])

    def test_labels_not_permitted_as_features(self):
        with self.assertRaises(ValueError):
            experiment({"sensor": self.records()}, ["이벤트"])

    def test_gap_resets_window(self):
        rows = self.records(24)
        records = [
            (timestamp(row["createdAt"]), [float(row["rms_a_1"])], row["_document_id"])
            for row in rows
        ]
        records[12:] = [
            (at + timedelta(minutes=5), values, key) for at, values, key in records[12:]
        ]
        retained, matrix = temporal_windows(records)
        self.assertEqual([row[2] for row in retained], ["11", "23"])
        self.assertEqual(matrix.shape, (2, 4))

    def test_short_sensor_is_not_randomly_split(self):
        _, report, _ = experiment(
            {"long": self.records(), "short": self.records(90)}, ["rms_a_1"]
        )
        self.assertEqual(
            report["streams"]["short"]["status"], "insufficient_temporal_coverage"
        )


if __name__ == "__main__":
    unittest.main()
