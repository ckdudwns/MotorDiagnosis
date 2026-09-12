"""Versioned delivery metadata; never changes measurement identity or RF score."""
from .vibration_windows import reject

POLICY_ID = "edge-trigger-batch-v1"
SNAPSHOT_POLICY_ID = "periodic-single-v1"
SNAPSHOT_INTERVAL_SECONDS = 300
REASONS = {"none", "rms_low", "rms_high", "severe", "quality",
           "baseline_missing", "storage_pressure", "recovery_hold"}


def validate_batch(payload):
    if isinstance(payload, dict) and set(payload) == {"windows"}:
        return None  # Existing firmware remains strictly ordered.
    if not isinstance(payload, dict) or set(payload) != {"windows", "transmission"}:
        reject("Expected windows and optional versioned transmission metadata")
    info = payload["transmission"]
    keys = {"policyId", "mode", "reason", "baselineId", "droppedWindows"}
    if not isinstance(info, dict) or set(info) != keys:
        reject("Invalid transmission metadata")
    if (info["policyId"] != POLICY_ID or info["mode"] not in ("periodic", "priority", "replay")
            or not isinstance(info["reason"], str) or info["reason"] not in REASONS):
        reject("Unknown transmission policy, mode or reason")
    if not isinstance(info["baselineId"], str) or not 1 <= len(info["baselineId"]) <= 64:
        reject("baselineId must identify the fixed normal baseline, or unconfigured")
    if type(info["droppedWindows"]) is not int or not 0 <= info["droppedWindows"] <= 2**53-1:
        reject("Invalid droppedWindows")
    return dict(info)


def validate_snapshot(payload):
    """A selected current window, never a batch or an urgent transmission."""
    if not isinstance(payload, dict) or set(payload) != {"window", "transmission"}:
        reject("Expected one window and periodic transmission metadata",
               code="INVALID_PERIODIC_SNAPSHOT")
    info = payload["transmission"]
    if (not isinstance(info, dict)
            or set(info) != {"policyId", "mode", "intervalSec"}
            or info["policyId"] != SNAPSHOT_POLICY_ID
            or info["mode"] != "periodic"
            or type(info["intervalSec"]) is not int
            or info["intervalSec"] != SNAPSHOT_INTERVAL_SECONDS):
        reject("Use periodic-single-v1, periodic mode and intervalSec=300; no priority or replay mode",
               code="INVALID_PERIODIC_SNAPSHOT")
    if not isinstance(payload["window"], dict):
        reject("window must be one complete measurement object",
               code="INVALID_PERIODIC_SNAPSHOT")
    return payload["window"]
