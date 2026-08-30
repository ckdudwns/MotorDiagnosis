"""
데이터셋 매니페스트 CSV/XLSX 내보내기 (AI-1, 3주차 DATA_EXPORT_01)

register_dataset.build_manifest()가 만든 매니페스트를 받아:
1. `dataset_manifest.json` — GET /api/datasets/{id} 응답 형태(rows 제외 + artifactRefs)
2. `dataset_rows.csv`      — 학습용 정규화 행 데이터
3. `dataset_export.xlsx`   — manifest 시트 + rows 시트

를 출력한다. week1 `build_ai1_handoff_dataset.py`처럼 CSV(csv 모듈)를 그대로 쓰고,
XLSX는 요청대로 별도 무거운 도구 없이 openpyxl만 사용한다.
"""

import os
import csv
import json
import shutil
import tempfile
import argparse

from openpyxl import Workbook, load_workbook

from register_dataset import (  # noqa: E402
    build_manifest,
    _DEFAULT_DATA_DIR,
    DEFAULT_SPLIT_RATIOS,
)


def _manifest_without_rows(manifest: dict, artifact_refs: dict) -> dict:
    """GET /api/datasets/{id} 응답 형태 (rows 제외, artifactRefs 포함)."""
    result = {k: v for k, v in manifest.items() if k != "rows"}
    result["artifactRefs"] = artifact_refs
    return result


def export_dataset(manifest: dict, output_dir: str) -> dict:
    """manifest를 CSV/XLSX/JSON으로 내보낸다.

    세 산출물을 output_dir에 바로 순차 기록하면, 뒤쪽 파일(XLSX/manifest)
    생성이 실패했을 때 앞서 이미 덮어쓴 파일(CSV)만 새 버전이고 나머지는
    이전 버전으로 남아 서로 다른 버전이 섞인다. 그래서 임시 디렉터리에서
    세 파일을 모두 만들고 검증한 뒤에만 output_dir로 교체한다 — 실패하면
    output_dir은 이전 상태 그대로 남는다.
    """
    os.makedirs(output_dir, exist_ok=True)

    rows = manifest["rows"]
    if not rows:
        raise ValueError("내보낼 행이 없습니다 (manifest['rows']가 비어 있음).")

    abs_output_dir = os.path.abspath(output_dir)
    staging_dir = tempfile.mkdtemp(
        prefix=".dataset_export_staging-", dir=os.path.dirname(abs_output_dir) or "."
    )
    try:
        csv_path = os.path.join(staging_dir, "dataset_rows.csv")
        fieldnames = list(rows[0].keys())
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

        xlsx_path = os.path.join(staging_dir, "dataset_export.xlsx")
        wb = Workbook()

        manifest_sheet = wb.active
        manifest_sheet.title = "manifest"
        manifest_sheet.append(["field", "value"])
        manifest_sheet.append(["id", manifest["id"]])
        manifest_sheet.append(["name", manifest["name"]])
        manifest_sheet.append(["source.type", manifest["source"]["type"]])
        manifest_sheet.append(["source.uri", manifest["source"]["uri"]])
        manifest_sheet.append(["source.license", manifest["source"]["license"]])
        manifest_sheet.append(["source.checksum", manifest["source"]["checksum"]])
        for filename, info in manifest["source"]["files"].items():
            manifest_sheet.append([f"source.files.{filename}.sha256", info["sha256"]])
            manifest_sheet.append([f"source.files.{filename}.label", info["label"]])
        manifest_sheet.append(
            ["compatibility.signalType", ",".join(manifest["compatibility"]["signalType"])]
        )
        manifest_sheet.append(
            ["compatibility.samplingRateHz", manifest["compatibility"]["samplingRateHz"]]
        )
        manifest_sheet.append(
            ["compatibility.units.vibration", manifest["compatibility"]["units"]["vibration"]]
        )
        rpm_range = manifest["compatibility"]["operatingConditions"]["rpmRange"]
        manifest_sheet.append(["compatibility.operatingConditions.rpmRange", str(rpm_range)])
        manifest_sheet.append(["labelTaxonomyVersion", manifest["labelTaxonomyVersion"]])
        for src_label, common_label in manifest["labelMapping"].items():
            manifest_sheet.append([f"labelMapping.{src_label}", common_label])
        manifest_sheet.append(["split.train", manifest["split"]["train"]])
        manifest_sheet.append(["split.validation", manifest["split"]["validation"]])
        manifest_sheet.append(["split.test", manifest["split"]["test"]])
        manifest_sheet.append(["splitStrategy", manifest["splitStrategy"]])
        manifest_sheet.append(["status", manifest["status"]])
        manifest_sheet.append(["reason", manifest["reason"]])
        manifest_sheet.append(["createdAt", manifest["createdAt"]])
        manifest_sheet.append(["rowCount", manifest["rowCount"]])
        for split_name, count in manifest["splitCounts"].items():
            manifest_sheet.append([f"splitCounts.{split_name}", count])

        rows_sheet = wb.create_sheet("rows")
        rows_sheet.append(fieldnames)
        for row in rows:
            rows_sheet.append([row.get(name) for name in fieldnames])

        wb.save(xlsx_path)

        final_csv_path = os.path.join(output_dir, "dataset_rows.csv")
        final_xlsx_path = os.path.join(output_dir, "dataset_export.xlsx")
        final_manifest_path = os.path.join(output_dir, "dataset_manifest.json")

        artifact_refs = {"csv": final_csv_path, "xlsx": final_xlsx_path}
        manifest_path = os.path.join(staging_dir, "dataset_manifest.json")
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(
                _manifest_without_rows(manifest, artifact_refs),
                f,
                ensure_ascii=False,
                indent=2,
            )

        # 세 파일 모두 온전히 만들어졌는지 확인한 뒤에만 교체한다 — 이 중
        # 하나라도 손상돼 있으면 output_dir의 이전 버전은 건드리지 않는다.
        if os.path.getsize(csv_path) == 0:
            raise RuntimeError("생성된 CSV가 비어 있습니다.")
        if os.path.getsize(xlsx_path) == 0:
            raise RuntimeError("생성된 XLSX가 비어 있습니다.")
        wb_check = load_workbook(xlsx_path)
        if "manifest" not in wb_check.sheetnames or "rows" not in wb_check.sheetnames:
            raise RuntimeError("생성된 XLSX에 manifest/rows 시트가 없습니다.")
        with open(manifest_path, encoding="utf-8") as f:
            json.load(f)  # 파싱 가능한 JSON인지 확인

        # os.replace()는 파일 단위로 원자적이다 (같은 파일시스템 내 rename).
        os.replace(csv_path, final_csv_path)
        os.replace(xlsx_path, final_xlsx_path)
        os.replace(manifest_path, final_manifest_path)
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

    return {
        "manifest_path": final_manifest_path,
        "csv_path": final_csv_path,
        "xlsx_path": final_xlsx_path,
        "row_count": len(rows),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="CWRU 데이터셋 매니페스트를 CSV/XLSX로 내보내기"
    )
    parser.add_argument("--data-dir", default=_DEFAULT_DATA_DIR)
    parser.add_argument("--train-ratio", type=float, default=DEFAULT_SPLIT_RATIOS["train"])
    parser.add_argument(
        "--validation-ratio", type=float, default=DEFAULT_SPLIT_RATIOS["validation"]
    )
    parser.add_argument("--test-ratio", type=float, default=DEFAULT_SPLIT_RATIOS["test"])
    parser.add_argument(
        "--output-dir",
        default=os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "data", "handoff")
        ),
    )
    args = parser.parse_args()

    # 기본 비율은 라벨당 자산이 여러 개일 때를 전제로 한다 — CWRU처럼 자산이
    # 1개뿐이면 build_manifest가 InsufficientAssetGroupsError를 낸다 (의도된 동작).
    manifest = build_manifest(
        data_dir=args.data_dir,
        split_ratios={
            "train": args.train_ratio,
            "validation": args.validation_ratio,
            "test": args.test_ratio,
        },
    )
    result = export_dataset(manifest, args.output_dir)

    print(f"{result['row_count']}행 내보내기 완료")
    print(f"  매니페스트: {result['manifest_path']}")
    print(f"  CSV: {result['csv_path']}")
    print(f"  XLSX: {result['xlsx_path']}")
