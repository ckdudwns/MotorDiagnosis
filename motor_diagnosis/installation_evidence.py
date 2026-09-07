"""Reconstruct installation metadata only from recorded, effective history."""

from . import data


def snapshot_for(event):
    at = data.parse_rfc3339("occurredAt", event["occurredAt"])
    points = []
    for row in data.INSTALL_POINTS:
        if row["siteId"] != event["siteId"] or row["assetId"] != event["assetId"]:
            continue
        created = row.get("createdAt")
        if not created or data.parse_rfc3339("createdAt", created) > at:
            continue
        state = data.install_point_snapshot(row)
        version_at = created
        # Reverse every later edit, retaining a deep snapshot independent of CRUD.
        for edit in reversed(row.get("changeHistory", [])):
            if data.parse_rfc3339("changedAt", edit["changedAt"]) > at:
                state = data.copy_payload(edit["before"])
            else:
                version_at = edit["changedAt"]
                break
        if state.get("active"):
            points.append({"id": row["id"], "versionAt": version_at, **state})
    return {
        "status": "recorded" if points else "unavailable",
        "effectiveAt": event["occurredAt"],
        "capturedAt": data.now_iso(),
        "source": "installation_change_history",
        "points": points,
    }
