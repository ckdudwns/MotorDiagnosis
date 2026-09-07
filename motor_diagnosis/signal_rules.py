"""Explicit, audited binding of registered baseline statistics to live inputs."""

import math

from . import data

FIELDS = {
    "vibrationRmsRaw": "vibrationUnitNote",
    "acousticRmsRaw": "acousticUnitNote",
    "rpm": None,
}


def bind_baselines(user, asset, requested):
    data.require_permission(user, "baseline:read")
    if not isinstance(requested, dict) or not requested or set(requested) - set(FIELDS):
        raise data.ApiError(
            400,
            "INVALID_SIGNAL_BASELINE",
            "Select one to three supported RMS/RPM inputs.",
        )
    result = {}
    for field, selection in requested.items():
        if not isinstance(selection, dict) or set(selection) != {
            "baselineVersion",
            "feature",
            "unitNote",
            "scale",
        }:
            raise data.ApiError(
                400,
                "INVALID_SIGNAL_BASELINE",
                "baselineVersion, feature, unitNote and scale are required.",
            )
        if any(
            not isinstance(selection[k], str) or not selection[k].strip()
            for k in ("baselineVersion", "feature")
        ):
            raise data.ApiError(
                400,
                "INVALID_SIGNAL_BASELINE",
                "Baseline and feature identifiers must be nonblank strings.",
            )
        baseline = next(
            (
                item
                for item in data.BASELINE_VERSIONS
                if item["version"] == selection["baselineVersion"]
            ),
            None,
        )
        if (
            not baseline
            or baseline["siteId"] != asset["siteId"]
            or baseline["assetId"] != asset["id"]
        ):
            raise data.ApiError(
                409,
                "SIGNAL_BASELINE_SCOPE",
                "A registered baseline for this asset is required.",
            )
        for site in baseline["scopeSiteIds"]:
            data.require_site_access(user, site)
        stats = baseline["features"].get(selection["feature"])
        bounds = stats.get("normal_range") if isinstance(stats, dict) else None
        scale = selection["scale"]
        unit = selection["unitNote"]
        if (
            not isinstance(bounds, list)
            or len(bounds) != 2
            or any(
                type(n) not in (int, float)
                or not -1e100 <= n <= 1e100
                or not math.isfinite(n)
                for n in bounds
            )
            or bounds[0] > bounds[1]
            or type(scale) not in (int, float)
            or not 0 < scale <= 1e100
            or not math.isfinite(scale)
            or not isinstance(unit, str)
            or not unit.strip()
            or len(unit) > 200
            or (field == "rpm" and unit != "rpm")
        ):
            raise data.ApiError(
                400,
                "INVALID_SIGNAL_BASELINE",
                "Finite ordered normal_range, positive scale and explicit input unit are required.",
            )
        result[field] = {
            "baselineVersion": baseline["version"],
            "datasetId": baseline["datasetId"],
            "feature": selection["feature"],
            "normalRange": data.copy_payload(bounds),
            "scale": scale,
            "unitNote": unit,
            "unitField": FIELDS[field],
        }
    return result
