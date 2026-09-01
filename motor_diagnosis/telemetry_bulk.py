"""Bounded partial-success replay using the same authorization/idempotency as ingest."""

from . import data

MAX_BULK_ITEMS = 100


def ingest_telemetry_bulk(principal, payload):
    items = payload.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_BULK_ITEMS:
        raise data.ApiError(
            400, "INVALID_BULK_PAYLOAD", "items must contain 1 to 100 records."
        )
    if any(not isinstance(item, dict) for item in items):
        raise data.ApiError(400, "INVALID_BULK_PAYLOAD", "Each item must be an object.")
    # Check the whole batch's device authorization before any writes.
    for item in items:
        device_id = item.get("deviceId")
        data.principal_can_ingest(
            principal, device_id.strip().upper() if isinstance(device_id, str) else ""
        )

    def ordering(pair):
        index, item = pair
        sequence = item.get("sequence")
        return (
            str(item.get("deviceId", "")).strip().upper(),
            sequence if type(sequence) is int else -1,
            index,
        )

    results = []
    for index, item in sorted(enumerate(items), key=ordering):
        try:
            result, status = data.ingest_telemetry(principal, item)
        except data.ApiError as exc:
            result, status = {
                "accepted": False,
                "error": {"code": exc.code, "message": exc.message},
            }, exc.status
        results.append({"index": index, "status": status, **result})
    results.sort(key=lambda row: row["index"])
    return {
        "items": results,
        "accepted": sum(
            row["accepted"] and not row.get("duplicate", False) for row in results
        ),
        "duplicates": sum(row.get("duplicate", False) for row in results),
        "rejected": sum(not row["accepted"] for row in results),
    }
