"""Versioned delivery metadata; never changes measurement identity or RF score."""
from .window_envelope import reject

POLICY_ID = "edge-trigger-batch-v1"
SNAPSHOT_POLICY_ID = "periodic-single-v1"
SNAPSHOT_INTERVAL_SECONDS = 300
EDGE_SNAPSHOT_POLICY_ID = "edge-state-snapshot-v1"
EDGE_ANOMALY_INTERVAL_SECONDS = 10
EDGE_ENTER_WINDOWS = 3
EDGE_RECOVERY_WINDOWS = 5
EDGE_SNAPSHOT_REASONS = {
    "normal_periodic": ("NORMAL", "periodic", SNAPSHOT_INTERVAL_SECONDS),
    "anomaly_enter": ("ANOMALY_ACTIVE", "immediate", 0),
    "anomaly_periodic": ("ANOMALY_ACTIVE", "periodic", EDGE_ANOMALY_INTERVAL_SECONDS),
    "normal_recovered": ("NORMAL", "immediate", 0),
}
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
    """Selected single windows; board state is NOT a server model verdict."""
    if not isinstance(payload, dict) or set(payload) != {"window", "transmission"}:
        reject("Expected one window and versioned transmission metadata",
               code="INVALID_PERIODIC_SNAPSHOT")
    info = payload["transmission"]
    window = payload["window"]
    if not isinstance(window, dict):
        reject("window must be one complete measurement object",
               code="INVALID_PERIODIC_SNAPSHOT")
    if not isinstance(info, dict):
        reject("Invalid transmission metadata", code="INVALID_PERIODIC_SNAPSHOT")
    from .pump_summary import POLICY_ID as HISTORY_POLICY, PROFILE_ID as SUMMARY_PROFILE, validate_delivery
    from . import edge_feature_snapshots as features
    if info.get("policyId") == features.POLICY_ID:
        features.validate_delivery(info, window)
    elif window.get("profileId") == features.PROFILE_ID:
        reject("ADXL features require edge-feature-snapshot-v1", code="INVALID_PERIODIC_SNAPSHOT")
    elif info.get("policyId") == HISTORY_POLICY:
        validate_delivery(info, window)
    elif window.get("profileId") == SUMMARY_PROFILE:
        reject("Source summaries require pump-verifier-history-v1", code="INVALID_PERIODIC_SNAPSHOT")
    elif info.get("policyId") == EDGE_SNAPSHOT_POLICY_ID:
        _validate_edge_snapshot(info, window)
    elif (set(info) != {"policyId", "mode", "intervalSec"}
          or info["policyId"] != SNAPSHOT_POLICY_ID
          or info["mode"] != "periodic"
          or type(info["intervalSec"]) is not int
          or info["intervalSec"] != SNAPSHOT_INTERVAL_SECONDS):
        reject("Use periodic-single-v1, periodic mode and intervalSec=300; no priority or replay mode",
               code="INVALID_PERIODIC_SNAPSHOT")
    return window


def _validate_edge_snapshot(info, window):
    keys = {"policyId", "mode", "intervalSec", "reason", "state", "anomalyCount", "normalCount"}
    if set(info) != keys:
        reject("Use the exact edge-state-snapshot-v1 transmission fields",
               code="INVALID_PERIODIC_SNAPSHOT")
    reason = info["reason"]
    if not isinstance(reason, str) or reason not in EDGE_SNAPSHOT_REASONS:
        reject("Unknown edge snapshot reason", code="INVALID_PERIODIC_SNAPSHOT")
    expected = EDGE_SNAPSHOT_REASONS[reason]
    if (type(info["intervalSec"]) is not int
            or (info["state"], info["mode"], info["intervalSec"]) != expected):
        reject("Edge state, reason, mode and interval must agree", code="INVALID_PERIODIC_SNAPSHOT")
    for field in ("anomalyCount", "normalCount"):
        if type(info[field]) is not int or not 0 <= info[field] <= 2**31-1:
            reject("Invalid consecutive counter: " + field, code="INVALID_PERIODIC_SNAPSHOT")
    anomaly, normal = info["anomalyCount"], info["normalCount"]
    if anomaly and normal:
        reject("Consecutive anomaly and normal counters cannot both be positive",
               code="INVALID_PERIODIC_SNAPSHOT")
    if ((reason == "anomaly_enter" and (anomaly, normal) != (EDGE_ENTER_WINDOWS, 0))
            or (reason == "normal_recovered" and (anomaly, normal) != (0, EDGE_RECOVERY_WINDOWS))
            or (reason == "normal_periodic" and anomaly >= EDGE_ENTER_WINDOWS)
            or (reason == "anomaly_periodic" and normal >= EDGE_RECOVERY_WINDOWS)):
        reject("Consecutive counters do not match the reported transition/state",
               code="INVALID_PERIODIC_SNAPSHOT")
    # Quality failures cannot confirm either anomaly entry or normal recovery.
    # Periodic failure evidence is retained with the latched board state and zero counters.
    if window.get("quality") != "valid" and (info["mode"] == "immediate" or anomaly or normal):
        reject("Unavailable windows cannot confirm a transition or contribute consecutive hits",
               code="INVALID_PERIODIC_SNAPSHOT")


def snapshot_interval_seconds(info):
    """Expected next periodic report, not delivery latency or event delay (0)."""
    from .pump_summary import POLICY_ID as HISTORY_POLICY
    from .edge_feature_snapshots import POLICY_ID as FEATURE_POLICY
    if info.get("policyId") == FEATURE_POLICY:
        # Maximum scheduled normal gap; the complete 25/25/10 cycle is metadata.
        return 10 if info.get("state") == "ANOMALY_ACTIVE" else 25
    if info.get("policyId") == HISTORY_POLICY:
        return 10 if info.get("state") == "ANOMALY_ACTIVE" else 25
    if info.get("policyId") == EDGE_SNAPSHOT_POLICY_ID and info.get("state") == "ANOMALY_ACTIVE":
        return EDGE_ANOMALY_INTERVAL_SECONDS
    return SNAPSHOT_INTERVAL_SECONDS


def snapshot_policy_metadata():
    from .edge_feature_snapshots import policy_metadata
    return [
        policy_metadata(),
        {"policyId": "pump-verifier-history-v1", "historyIntervalSec": 25,
         "normalIntervalSec": 25, "anomalyIntervalSec": 10, "historyRows": 24,
         "historySchedule": "fixed_acquisition_grid", "immediateReportsAdvanceHistory": False,
         "enterConsecutiveWindows": 3, "recoveryConsecutiveWindows": 5},
        {"policyId": SNAPSHOT_POLICY_ID, "normalIntervalSec": SNAPSHOT_INTERVAL_SECONDS},
        {"policyId": EDGE_SNAPSHOT_POLICY_ID,
         "normalIntervalSec": SNAPSHOT_INTERVAL_SECONDS,
         "anomalyIntervalSec": EDGE_ANOMALY_INTERVAL_SECONDS,
         "enterConsecutiveWindows": EDGE_ENTER_WINDOWS,
         "recoveryConsecutiveWindows": EDGE_RECOVERY_WINDOWS,
         "normalSchedule": "wall_clock", "stateSource": "device_report",
         "serverVerified": False},
    ]
