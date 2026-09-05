"""Single-HTTP-process PoC alert outbox. Network delivery is at-least-once.

External adapters receive a stable delivery ID for downstream deduplication.
Never share this outbox between concurrently running HTTP servers.
"""

from __future__ import annotations

import json
import logging
import os
import re
import smtplib
import ssl
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import data

LOGGER = logging.getLogger(__name__)


class DeliveryError(Exception):
    def __init__(self, code: str, retryable: bool = True):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def configured_adapters():
    """Destinations/credentials come from server configuration, never API URLs."""
    adapters = {}
    webhook_url = os.environ.get("ALERT_WEBHOOK_URL", "")
    if webhook_url:
        parsed = urlsplit(webhook_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ValueError(
                "ALERT_WEBHOOK_URL must be an HTTPS URL without credentials."
            )

        def webhook(row):
            request = Request(
                webhook_url,
                data=json.dumps(row, ensure_ascii=True, allow_nan=False).encode(
                    "utf-8"
                ),
                headers={
                    "Content-Type": "application/json",
                    "Idempotency-Key": row["id"],
                },
                method="POST",
            )
            try:
                with build_opener(NoRedirect).open(request, timeout=5) as response:
                    if not 200 <= response.status < 300:
                        raise DeliveryError("WEBHOOK_REJECTED", False)
            except HTTPError as exc:
                raise DeliveryError(
                    f"WEBHOOK_HTTP_{exc.code}",
                    exc.code >= 500 or exc.code in {408, 425, 429},
                ) from exc

        adapters["webhook"] = webhook

    smtp_host = os.environ.get("ALERT_SMTP_HOST", "")
    if smtp_host:
        smtp_port = int(os.environ.get("ALERT_SMTP_PORT", "465"))
        sender = os.environ.get("ALERT_EMAIL_FROM", "")
        username = os.environ.get("ALERT_SMTP_USERNAME", "")
        password = os.environ.get("ALERT_SMTP_PASSWORD", "")

        def email(row):
            for address in (sender, row["recipient"]):
                if not re.fullmatch(r"[^\s@<>]+@[^\s@<>]+", address):
                    raise DeliveryError("INVALID_EMAIL_ADDRESS", False)
            message = EmailMessage()
            message["From"] = sender
            message["To"] = row["recipient"]
            message["Subject"] = (
                f"{'[TEST] ' if row['isTest'] else ''}Bind Edge AI {row['eventId']}"
            )
            message["Message-ID"] = f"<{row['id']}@bind-edge-ai.local>"
            message.set_content(json.dumps(row["event"], ensure_ascii=True, indent=2))
            with smtplib.SMTP_SSL(
                smtp_host, smtp_port, timeout=5, context=ssl.create_default_context()
            ) as smtp:
                if username:
                    smtp.login(username, password)
                smtp.send_message(message)

        adapters["email"] = email
    return adapters


class AlertService:
    MAX_ATTEMPTS = 3

    def __init__(self, database=":memory:", *, adapters=None, clock=time.time):
        self._owner_file = None
        if database != ":memory:":
            Path(database).parent.mkdir(parents=True, exist_ok=True)
            owner = open(str(Path(database).resolve()) + ".owner.lock", "a+b")
            try:
                if owner.seek(0, 2) == 0:
                    owner.write(b"0")
                    owner.flush()
                owner.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(owner.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                owner.close()
                raise ValueError(
                    "This alert outbox is already owned by another HTTP server."
                )
            self._owner_file = owner
        self._lock = threading.RLock()
        self._db = sqlite3.connect(database, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS alert_deliveries (
                id TEXT PRIMARY KEY, dedupe_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL, due_at REAL NOT NULL, payload TEXT NOT NULL,
                channel TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS alert_due ON alert_deliveries(status, due_at);
            CREATE INDEX IF NOT EXISTS alert_channel_due ON alert_deliveries(channel, status, due_at);
            CREATE TABLE IF NOT EXISTS alert_seen_events (event_id TEXT PRIMARY KEY);
            """
        )
        self._db.executemany(
            "INSERT OR IGNORE INTO alert_seen_events VALUES (?)",
            [(event["id"],) for event in data.BASE_EVENTS],
        )
        # An interrupted send is uncertain, not confirmed successful.
        for record in self._db.execute(
            "SELECT payload FROM alert_deliveries WHERE status='sending'"
        ).fetchall():
            row = json.loads(record["payload"])
            row.update(status="pending", nextRetryAt=0)
            self._save(row)
        self._db.commit()
        self._clock = clock
        self._adapters = dict(adapters or {})
        self._pools = {
            channel: ThreadPoolExecutor(
                max_workers=1, thread_name_prefix=f"alert-{channel}"
            )
            for channel in sorted(data.ALERT_CHANNELS)
        }
        self._busy = set()
        self._pending_results = {}
        self._closed = False

    def _save(self, row):
        self._db.execute(
            "UPDATE alert_deliveries SET status=?, due_at=?, payload=? WHERE id=?",
            (
                row["status"],
                row["nextRetryAt"] or 0,
                json.dumps(row, ensure_ascii=True, allow_nan=False),
                row["id"],
            ),
        )

    def send(self, user, payload):
        data.require_permission(user, "alert:send")
        event_id = data.required_text(payload, "eventId")
        is_test = data.boolean_field(payload, "isTest", default=False)
        policy_id = payload.get("policyId")
        if policy_id is not None:
            policy_id = data.required_text(payload, "policyId")
        with data.STORE_LOCK:
            event = data.copy_payload(data.get_event(event_id))
            data.require_site_access(user, event["siteId"])
            policies = data.alert_policies_for(user)
            if policy_id:
                policies = [policy for policy in policies if policy["id"] == policy_id]
                if not policies:
                    raise data.ApiError(
                        404, "ALERT_POLICY_NOT_FOUND", "Alert policy was not found."
                    )
        result = self._queue(event, policies, is_test=is_test)
        audit_result = {
            "eventId": event["id"],
            "deliveryIds": [row["id"] for row in result["deliveries"]],
            "isTest": is_test,
            "suppressed": result["suppressed"],
        }
        with data.STORE_LOCK:
            data.append_audit_log(
                user,
                "alert.send",
                "event",
                event_id,
                None,
                audit_result,
                "Test alert requested" if is_test else "Event alert requested",
                site_id=event["siteId"],
            )
        return result

    def _queue(self, event, policies, *, is_test=False):
        now = self._clock()
        stamp = datetime.fromtimestamp(now, timezone.utc)
        minute = stamp.strftime("%H:%M")
        deliveries, suppressed = [], []
        with self._lock, self._db:
            for policy in policies:
                reason = None
                scope = policy.get("scopeSiteIds") or []
                if not policy["enabled"]:
                    reason = "disabled"
                elif policy["siteIds"] and event["siteId"] not in policy["siteIds"]:
                    reason = "scope_mismatch"
                elif policy["assetIds"] and event["assetId"] not in policy["assetIds"]:
                    reason = "scope_mismatch"
                elif scope and event["siteId"] not in scope:
                    reason = "scope_mismatch"
                elif event["severity"] != policy["severity"]:
                    reason = "severity_mismatch"
                elif event.get("reviewed") and not is_test:
                    reason = "reviewed"
                start, end = policy["workHours"]["start"], policy["workHours"]["end"]
                in_hours = (
                    start <= minute <= end
                    if start <= end
                    else minute >= start or minute <= end
                )
                if not reason and not in_hours and not is_test:
                    reason = "outside_work_hours"
                if reason:
                    suppressed.append({"policyId": policy["id"], "reason": reason})
                    continue
                pending = []
                for channel in sorted(set(policy["channels"])):
                    # One web notification per event/policy; other channels per recipient.
                    recipients = (
                        ["site-operators"]
                        if channel == "web"
                        else sorted(set(policy["recipients"]))
                    )
                    for recipient in recipients:
                        key = json.dumps(
                            [event["id"], policy["id"], channel, recipient, is_test]
                        )
                        previous = self._db.execute(
                            "SELECT payload FROM alert_deliveries WHERE dedupe_key=?",
                            (key,),
                        ).fetchone()
                        if previous:
                            deliveries.append(json.loads(previous[0]))
                        else:
                            pending.append((channel, recipient, key))
                # Cooldown controls new sends, not reads of an existing delivery.
                if not pending:
                    continue
                existing = [
                    json.loads(item[0])
                    for item in self._db.execute(
                        "SELECT payload FROM alert_deliveries"
                    ).fetchall()
                ]
                if not is_test and any(
                    item["policyId"] == policy["id"]
                    and item["assetId"] == event["assetId"]
                    and item["siteId"] == event["siteId"]
                    and not item["isTest"]
                    and item["eventId"] != event["id"]
                    and now - item["createdEpoch"] < policy["cooldownSec"]
                    for item in existing
                ):
                    suppressed.append({"policyId": policy["id"], "reason": "cooldown"})
                    continue
                for channel, recipient, key in pending:
                    row = {
                        "id": f"ALERT-{uuid.uuid4().hex}",
                        "eventId": event["id"],
                        "siteId": event["siteId"],
                        "assetId": event["assetId"],
                        "policyId": policy["id"],
                        "policySnapshot": data.copy_payload(policy),
                        "event": data.copy_payload(event),
                        "channel": channel,
                        "recipient": recipient,
                        "isTest": is_test,
                        "status": "pending",
                        "attemptCount": 0,
                        "attempts": [],
                        "createdAt": data.format_rfc3339(stamp),
                        "createdEpoch": now,
                        "nextRetryAt": now,
                        "deliveredAt": None,
                        "lastError": None,
                    }
                    self._db.execute(
                        "INSERT INTO alert_deliveries VALUES (?, ?, ?, ?, ?, ?)",
                        (
                            row["id"],
                            key,
                            "pending",
                            now,
                            json.dumps(row, ensure_ascii=True),
                            channel,
                        ),
                    )
                    deliveries.append(row)
        return {
            "eventId": event["id"],
            "deliveries": deliveries,
            "suppressed": suppressed,
        }

    def observe_events(self):
        with data.STORE_LOCK:
            events = data.copy_payload(data.EVENTS)
            policies = data.copy_payload(data.ALERT_POLICIES)
        # Oldest-first: batch recovery must not prefer later alarms for cooldown.
        events.sort(
            key=lambda row: (
                data.parse_rfc3339("occurredAt", row["occurredAt"]),
                row["id"],
            )
        )
        for event in events:
            with self._lock:
                if self._db.execute(
                    "SELECT 1 FROM alert_seen_events WHERE event_id=?", (event["id"],)
                ).fetchone():
                    continue
                # _queue commits first; a crash here is safe because delivery keys are unique.
                self._queue(event, policies)
                with self._db:
                    self._db.execute(
                        "INSERT OR IGNORE INTO alert_seen_events VALUES (?)",
                        (event["id"],),
                    )

    def _claim(self, *, one_per_channel=False):
        claimed = []
        with self._lock:
            if self._closed:
                return claimed
            with self._db:
                for channel in sorted(data.ALERT_CHANNELS):
                    if channel in self._busy:
                        continue
                    records = self._db.execute(
                        "SELECT payload FROM alert_deliveries WHERE channel=? AND status='pending' AND due_at<=? ORDER BY due_at, id LIMIT ?",
                        (channel, self._clock(), 1 if one_per_channel else 100),
                    ).fetchall()
                    for record in records:
                        row = json.loads(record[0])
                        row["status"] = "sending"
                        self._save(row)
                        claimed.append(row)
            self._busy.update(row["channel"] for row in claimed)
        return claimed

    def _deliver(self, row):
        started = time.monotonic()
        error, retryable = None, False
        try:
            if row["channel"] == "stub":
                LOGGER.info("alert_stub id=%s event=%s", row["id"], row["eventId"])
            elif row["channel"] != "web":
                adapter = self._adapters.get(row["channel"])
                if adapter is None:
                    raise DeliveryError("CHANNEL_NOT_CONFIGURED", False)
                adapter(
                    data.copy_payload(
                        {
                            key: row[key]
                            for key in (
                                "id",
                                "eventId",
                                "siteId",
                                "assetId",
                                "event",
                                "recipient",
                                "channel",
                                "isTest",
                            )
                        }
                    )
                )
        except DeliveryError as exc:
            error, retryable = exc.code, exc.retryable
        except Exception:
            # Do not expose credentials/destination URLs from adapter exceptions.
            error, retryable = "CHANNEL_DELIVERY_FAILED", True
        now = self._clock()
        try:
            with self._lock, self._db:
                row["attemptCount"] += 1
                row["lastError"] = error
                row["attempts"].append(
                    {
                        "number": row["attemptCount"],
                        "at": data.format_rfc3339(
                            datetime.fromtimestamp(now, timezone.utc)
                        ),
                        "success": error is None,
                        "error": error,
                    }
                )
                row["status"] = "sent" if error is None else "failed"
                row["nextRetryAt"] = None
                if error is None:
                    row["deliveredAt"] = row["attempts"][-1]["at"]
                elif retryable and row["attemptCount"] < self.MAX_ATTEMPTS:
                    row["status"] = "pending"
                    row["nextRetryAt"] = now + min(60, 2 ** row["attemptCount"])
                self._save(row)
        except sqlite3.Error:
            with self._lock:
                self._pending_results[row["id"]] = row
            LOGGER.warning("alert_result_save_pending id=%s", row["id"])
        else:
            with self._lock:
                self._busy.discard(row["channel"])
        try:
            data.record_runtime_dependency(
                "alerts",
                success=error is None,
                latency_ms=(time.monotonic() - started) * 1000,
                error_code=error,
                detail=(
                    None
                    if error is None
                    else f"Alert delivery failed on {row['channel']}."
                ),
            )
        except Exception:
            LOGGER.exception("alert_dependency_health_update_failed")

    def _flush_results(self):
        with self._lock:
            for key, row in list(self._pending_results.items()):
                with self._db:
                    self._save(row)
                self._pending_results.pop(key)
                self._busy.discard(row["channel"])

    def tick(self):
        """Called by the outbox coordinator; channel delivery workers are bounded."""
        self._flush_results()
        self.observe_events()
        for row in self._claim(one_per_channel=True):
            self._pools[row["channel"]].submit(self._deliver, row)

    def process_due(self):
        """Synchronous helper for deterministic integration tests, not production loop."""
        self._flush_results()
        for row in self._claim():
            self._deliver(row)

    def list_for(self, user, *, site_id="", status="", channel="", page=1, size=50):
        data.require_permission(user, "alert:read")
        if site_id:
            data.require_site_access(user, site_id)
        if status and status not in {"pending", "sending", "sent", "failed"}:
            raise data.ApiError(
                400, "INVALID_ALERT_STATUS", "Unsupported alert status."
            )
        if channel and channel not in data.ALERT_CHANNELS:
            raise data.ApiError(
                400, "INVALID_ALERT_CHANNEL", "Unsupported alert channel."
            )
        allowed = user.get("allowedSiteIds", [])
        with self._lock:
            rows = [
                json.loads(record[0])
                for record in self._db.execute("SELECT payload FROM alert_deliveries")
            ]
        rows = [
            row
            for row in rows
            if ("*" in allowed or row["siteId"] in allowed)
            and (not site_id or row["siteId"] == site_id)
            and (not status or row["status"] == status)
            and (not channel or row["channel"] == channel)
        ]
        rows.sort(key=lambda row: (row["createdEpoch"], row["id"]), reverse=True)
        # A site-level reader must not learn the other sites/recipients of a global policy.
        for row in rows:
            row.pop("policySnapshot", None)
            if not data.has_permission(user, "alert:send"):
                row.pop("recipient", None)
        return {
            "items": rows[(page - 1) * size : page * size],
            "total": len(rows),
            "page": page,
            "size": size,
        }

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
        for pool in self._pools.values():
            pool.shutdown(wait=True)
        with self._lock:
            self._db.close()
            if self._owner_file is not None:
                self._owner_file.close()
