"""Versioned statistical scoring; no calibration or missing-value imputation."""

import math


def score_signals(point, bindings, rule_version):
    evidence = []
    scores = []
    for field, binding in sorted(bindings.items()):
        value = point.get(field)
        reason = None
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            reason = "missing_or_invalid"
        elif field == "rpm" and point.get("rpmStatus") != "valid":
            reason = "rpm_not_measured"
        elif field != "rpm" and point.get(binding["unitField"]) != binding["unitNote"]:
            reason = "unit_mismatch"
        row = {
            "field": field,
            "baselineVersion": binding["baselineVersion"],
            "feature": binding["feature"],
            "ruleVersion": rule_version,
            "unitNote": binding["unitNote"],
            "normalRange": binding["normalRange"],
            "scale": binding["scale"],
            "status": reason or "evaluated",
            "value": value,
            "score": None,
        }
        if reason is None:
            lo, hi = binding["normalRange"]
            distance = max(lo - value, value - hi, 0.0)
            row["score"] = round(min(100.0, distance / binding["scale"] * 100.0))
            scores.append(row["score"])
        evidence.append(row)
    score = max(scores) if scores else None
    return {
        "anomalyScore": score,
        "anomalyStatus": (
            "unavailable"
            if score is None
            else "critical" if score >= 75 else "warning" if score >= 50 else "normal"
        ),
        "anomalyModel": f"asset-signal-rules-v1:{rule_version}",
        "anomalyEvidence": evidence,
        "anomalyCombination": "max_available_v1",
    }
