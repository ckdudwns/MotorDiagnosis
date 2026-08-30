from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import ssl
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .json_validation import loads_strict_json

LOGGER = logging.getLogger("motor_diagnosis.mqtt")
SUPPORTED_SUBSCRIPTION_QOS = 1
RETRYABLE_HTTP_STATUSES = {408, 425, 429}
BACKEND_QUARANTINED_INGEST_ERRORS = frozenset(
    {
        (400, "INVALID_TELEMETRY_PAYLOAD"),
        (400, "INVALID_RAW_ONLY_FIELD"),
        (404, "SITE_NOT_FOUND"),
        (404, "ASSET_NOT_FOUND"),
        (404, "DEVICE_NOT_FOUND"),
        (409, "DEVICE_MAPPING_MISMATCH"),
        (409, "DEVICE_CERTIFICATE_NOT_ACTIVE"),
        (409, "SEQUENCE_CONFLICT"),
    }
)


@dataclass
class MqttBridgeError(Exception):
    status: int
    code: str
    message: str
    local: bool = False


def _mqtt_text_for_storage(value: str) -> str:
    return value.encode("utf-8", errors="backslashreplace").decode("utf-8")


def decode_mqtt_payload(topic: str, message: bytes | str) -> dict[str, Any]:
    parts = [part for part in topic.strip("/").split("/") if part]
    if len(parts) != 3 or parts[0] != "devices" or parts[2] != "telemetry":
        raise MqttBridgeError(
            400,
            "INVALID_MQTT_TOPIC",
            "MQTT topic must be devices/{deviceId}/telemetry.",
            local=True,
        )
    try:
        payload = loads_strict_json(
            message.decode("utf-8") if isinstance(message, bytes) else message
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise MqttBridgeError(
            400,
            "INVALID_JSON",
            "MQTT payload is not valid JSON.",
            local=True,
        ) from exc
    if not isinstance(payload, dict):
        raise MqttBridgeError(
            400,
            "INVALID_JSON_BODY",
            "MQTT payload must be a JSON object.",
            local=True,
        )
    topic_device_id = parts[1].strip().upper()
    payload_device_id = str(payload.get("deviceId") or "").strip().upper()
    if topic_device_id != payload_device_id:
        raise MqttBridgeError(
            409,
            "DEVICE_MAPPING_MISMATCH",
            "MQTT topic deviceId and payload deviceId must match.",
            local=True,
        )
    return payload


def post_json(
    endpoint: str,
    token: str,
    payload: dict[str, Any],
    *,
    timeout: float,
) -> tuple[dict[str, Any], int]:
    try:
        request_body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode(
            "utf-8"
        )
    except (UnicodeEncodeError, ValueError) as exc:
        raise MqttBridgeError(
            400,
            "INVALID_JSON",
            "Outbound JSON payload contains invalid Unicode or numeric values.",
        ) from exc
    request = Request(
        endpoint,
        data=request_body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            try:
                body = json.loads(response.read().decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise MqttBridgeError(
                    502,
                    "INVALID_HTTP_RESPONSE",
                    "Backend API returned an invalid JSON response.",
                ) from exc
            if not isinstance(body, dict):
                raise MqttBridgeError(
                    502,
                    "INVALID_HTTP_RESPONSE",
                    "Backend API response must be a JSON object.",
                )
            return body, response.status
    except HTTPError as exc:
        try:
            error_body = json.loads(exc.read().decode("utf-8"))
            error = error_body.get("error", {}) if isinstance(error_body, dict) else {}
            code = str(error.get("code") or "INGEST_REJECTED")
            message_text = str(error.get("message") or exc.reason)
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
            code = "INGEST_REJECTED"
            message_text = str(exc.reason)
        raise MqttBridgeError(exc.code, code, message_text) from exc
    except (TimeoutError, URLError) as exc:
        reason = getattr(exc, "reason", exc)
        raise MqttBridgeError(
            503,
            "HTTP_SERVICE_UNAVAILABLE",
            f"Backend API is unavailable: {reason}",
        ) from exc


def forward_mqtt_message(
    topic: str,
    message: bytes | str,
    *,
    endpoint: str,
    token: str,
    timeout: float = 10,
) -> tuple[dict[str, Any], int]:
    """Forward MQTT telemetry into the HTTP server that owns runtime storage."""
    payload = decode_mqtt_payload(topic, message)
    return post_json(endpoint, token, payload, timeout=timeout)


def report_mqtt_status(
    endpoint: str,
    token: str,
    status: str,
    *,
    detail: str | None = None,
    error_code: str | None = None,
    timeout: float = 3,
) -> dict[str, Any]:
    response, _status = post_json(
        endpoint,
        token,
        {"status": status, "detail": detail, "errorCode": error_code},
        timeout=timeout,
    )
    return response


def quarantine_local_mqtt_message(
    endpoint: str,
    token: str,
    topic: str,
    message: bytes | str,
    error: MqttBridgeError,
    *,
    idempotency_key: str | None = None,
    timeout: float = 10,
) -> tuple[dict[str, Any], int]:
    raw_payload = (
        message.decode("utf-8", errors="replace")
        if isinstance(message, bytes)
        else str(message)
    )
    raw_payload = _mqtt_text_for_storage(raw_payload)
    quarantine_payload = {
        "topic": _mqtt_text_for_storage(topic),
        "payload": raw_payload,
        "reason": error.code,
        "message": error.message,
    }
    if idempotency_key is not None:
        quarantine_payload["idempotencyKey"] = idempotency_key
    return post_json(
        endpoint,
        token,
        quarantine_payload,
        timeout=timeout,
    )


def is_permanent_ingest_error(error: MqttBridgeError) -> bool:
    if error.local:
        return 400 <= error.status < 500 and error.status not in RETRYABLE_HTTP_STATUSES
    return (error.status, error.code) in BACKEND_QUARANTINED_INGEST_ERRORS


def acknowledge_message(client: Any, message: Any) -> bool:
    if int(message.qos) == 0:
        return True
    result = client.ack(message.mid, message.qos)
    if int(result) == 0:
        return True
    LOGGER.error(
        "mqtt_ack_failed mid=%s qos=%s result=%s",
        message.mid,
        message.qos,
        result,
    )
    return False


@dataclass(frozen=True)
class RetryMessage:
    topic: str
    payload: bytes
    mid: int
    qos: int
    dup: bool
    session_epoch: str
    delivery_id: str = ""
    delivery_state: str = "active"
    delivery_outcome: str = ""


class MqttRetryQueue:
    """SQLite-backed retry worker for HTTP delivery and local quarantine writes."""

    def __init__(
        self,
        *,
        database_path: str | Path,
        ingest_endpoint: str,
        quarantine_endpoint: str,
        token: str,
        initial_delay: float = 1.0,
        max_delay: float = 120.0,
        poll_interval: float = 0.5,
        database_timeout: float = 0.25,
        migration_timeout: float = 5.0,
        lease_seconds: float = 60.0,
        session_ready: bool = True,
    ) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.ingest_endpoint = ingest_endpoint
        self.quarantine_endpoint = quarantine_endpoint
        self.token = token
        self.initial_delay = max(0.0, initial_delay)
        self.max_delay = max(self.initial_delay, max_delay)
        self.poll_interval = max(0.05, poll_interval)
        self.database_timeout = max(0.05, database_timeout)
        self.migration_timeout = max(self.database_timeout, migration_timeout)
        self.lease_seconds = max(1.0, lease_seconds)
        self._claim_owner = secrets.token_hex(16)
        self._session_epoch = ""
        self._session_lock = threading.Lock()
        self._session_ready = threading.Event()
        if session_ready:
            self._session_ready.set()
        self._ack_targets: dict[str, tuple[Any, RetryMessage]] = {}
        self._target_lock = threading.Lock()
        self._processing_lock = threading.Lock()
        self._inflight_message_keys: set[str] = set()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._worker: threading.Thread | None = None
        self._initialize_database()

    def _connect(self, *, timeout: float | None = None) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=self.database_timeout if timeout is None else timeout,
        )
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _connection(
        self, *, timeout: float | None = None
    ) -> Iterator[sqlite3.Connection]:
        connection = self._connect(timeout=timeout)
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize_database(self) -> None:
        delay = 0.05
        while True:
            try:
                self._migrate_database_once()
                return
            except sqlite3.OperationalError as error:
                if (
                    "locked" not in str(error).casefold()
                    and "busy" not in str(error).casefold()
                ):
                    raise
                LOGGER.warning(
                    "mqtt_retry_migration_wait database=%s delay=%.2f error=%s",
                    self.database_path,
                    delay,
                    error,
                )
                time.sleep(delay)
                delay = min(2.0, delay * 2)

    def _migrate_database_once(self) -> None:
        with self._connection(timeout=self.migration_timeout) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS mqtt_retry_queue (
                    message_key TEXT PRIMARY KEY,
                    operation TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    payload BLOB NOT NULL,
                    mid INTEGER NOT NULL,
                    qos INTEGER NOT NULL,
                    attempts INTEGER NOT NULL,
                    next_attempt_at REAL NOT NULL,
                    error_code TEXT NOT NULL,
                    error_message TEXT NOT NULL,
                    last_error_code TEXT NOT NULL,
                    last_error_message TEXT NOT NULL,
                    delivery_completed INTEGER NOT NULL DEFAULT 0,
                    delivery_outcome TEXT NOT NULL DEFAULT '',
                    claim_owner TEXT NOT NULL DEFAULT '',
                    claim_token INTEGER NOT NULL DEFAULT 0,
                    claim_until REAL NOT NULL DEFAULT 0,
                    session_epoch TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL
                )
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(mqtt_retry_queue)"
                ).fetchall()
            }
            migrations = {
                "last_error_code": "TEXT NOT NULL DEFAULT ''",
                "last_error_message": "TEXT NOT NULL DEFAULT ''",
                "delivery_completed": "INTEGER NOT NULL DEFAULT 0",
                "delivery_outcome": "TEXT NOT NULL DEFAULT ''",
                "claim_owner": "TEXT NOT NULL DEFAULT ''",
                "claim_token": "INTEGER NOT NULL DEFAULT 0",
                "claim_until": "REAL NOT NULL DEFAULT 0",
                "session_epoch": "TEXT NOT NULL DEFAULT ''",
            }
            added_columns: set[str] = set()
            for column, definition in migrations.items():
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE mqtt_retry_queue ADD COLUMN {column} {definition}"
                    )
                    added_columns.add(column)
            if "last_error_code" in added_columns:
                connection.execute(
                    """
                    UPDATE mqtt_retry_queue
                    SET last_error_code = error_code
                    WHERE last_error_code = ''
                    """
                )
            if "last_error_message" in added_columns:
                connection.execute(
                    """
                    UPDATE mqtt_retry_queue
                    SET last_error_message = error_message
                    WHERE last_error_message = ''
                    """
                )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS mqtt_retry_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS mqtt_delivery_generations (
                    delivery_id TEXT PRIMARY KEY,
                    session_epoch TEXT NOT NULL,
                    mid INTEGER NOT NULL,
                    topic TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    active_identity INTEGER NOT NULL DEFAULT 1,
                    state TEXT NOT NULL,
                    outcome TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            generation_columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(mqtt_delivery_generations)"
                ).fetchall()
            }
            if "active_identity" not in generation_columns:
                connection.execute(
                    """
                    ALTER TABLE mqtt_delivery_generations
                    ADD COLUMN active_identity INTEGER NOT NULL DEFAULT 1
                    """
                )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS mqtt_delivery_generation_lookup
                ON mqtt_delivery_generations (
                    session_epoch, mid, created_at DESC
                )
                """
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO mqtt_retry_state (key, value)
                VALUES ('session_epoch', ?)
                """,
                (secrets.token_hex(16),),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO mqtt_retry_state (key, value)
                VALUES ('claim_sequence', '0')
                """
            )
            session_epoch = str(
                connection.execute(
                    "SELECT value FROM mqtt_retry_state WHERE key = 'session_epoch'"
                ).fetchone()["value"]
            )
            if "session_epoch" in added_columns:
                connection.execute(
                    """
                    UPDATE mqtt_retry_queue
                    SET session_epoch = ?
                    WHERE session_epoch = ''
                    """,
                    (session_epoch,),
                )
        with self._session_lock:
            self._session_epoch = session_epoch

    def begin_session(self, *, session_present: bool) -> str:
        """Persist the broker session generation used to identify packet IDs."""
        reset_ack_targets = False
        with self._connection(timeout=self.migration_timeout) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if session_present:
                session_epoch = str(
                    connection.execute(
                        "SELECT value FROM mqtt_retry_state WHERE key = 'session_epoch'"
                    ).fetchone()["value"]
                )
            else:
                reset_ack_targets = True
                session_epoch = secrets.token_hex(16)
                connection.execute(
                    """
                    UPDATE mqtt_retry_state
                    SET value = ?
                    WHERE key = 'session_epoch'
                    """,
                    (session_epoch,),
                )
                connection.execute(
                    """
                    DELETE FROM mqtt_retry_queue
                    WHERE session_epoch != ? AND delivery_completed = 1
                    """,
                    (session_epoch,),
                )
                connection.execute(
                    """
                    UPDATE mqtt_retry_queue
                    SET qos = 0
                    WHERE session_epoch != ?
                    """,
                    (session_epoch,),
                )
        with self._session_lock:
            self._session_epoch = session_epoch
        if reset_ack_targets:
            with self._target_lock:
                self._ack_targets.clear()
        self._session_ready.set()
        LOGGER.info(
            "mqtt_session_epoch session_present=%s epoch=%s",
            session_present,
            session_epoch,
        )
        return session_epoch

    def pause_session(self) -> None:
        self._session_ready.clear()
        with self._target_lock:
            self._ack_targets.clear()

    @staticmethod
    def _payload_hash(topic: str, payload: bytes) -> str:
        digest = hashlib.sha256()
        digest.update(topic.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payload)
        return digest.hexdigest()

    def _snapshot(
        self,
        message: Any,
        *,
        delivery_id: str = "",
        delivery_state: str = "active",
        delivery_outcome: str = "",
    ) -> RetryMessage:
        payload = message.payload
        if isinstance(payload, str):
            payload_bytes = _mqtt_text_for_storage(payload).encode("utf-8")
        else:
            payload_bytes = bytes(payload)
        with self._session_lock:
            session_epoch = self._session_epoch
        return RetryMessage(
            topic=_mqtt_text_for_storage(str(message.topic)),
            payload=payload_bytes,
            mid=int(message.mid),
            qos=int(message.qos),
            dup=bool(getattr(message, "dup", False)),
            session_epoch=session_epoch,
            delivery_id=delivery_id,
            delivery_state=delivery_state,
            delivery_outcome=delivery_outcome,
        )

    def prepare_delivery(self, message: Any) -> RetryMessage:
        if not self._session_ready.is_set():
            raise sqlite3.OperationalError("MQTT session state is not ready")
        retry_message = self._snapshot(message)
        payload_hash = self._payload_hash(retry_message.topic, retry_message.payload)
        now = time.time()
        superseded_delivery_ids: list[str] = []
        with self._processing_lock:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                if retry_message.dup:
                    generation = connection.execute(
                        """
                        SELECT * FROM mqtt_delivery_generations
                        WHERE session_epoch = ? AND mid = ? AND topic = ?
                            AND payload_hash = ? AND active_identity = 1
                        ORDER BY created_at DESC, rowid DESC
                        LIMIT 1
                        """,
                        (
                            retry_message.session_epoch,
                            retry_message.mid,
                            retry_message.topic,
                            payload_hash,
                        ),
                    ).fetchone()
                    if generation is not None:
                        return self._snapshot(
                            message,
                            delivery_id=str(generation["delivery_id"]),
                            delivery_state=str(generation["state"]),
                            delivery_outcome=str(generation["outcome"]),
                        )

                previous = connection.execute(
                    """
                    SELECT delivery_id FROM mqtt_delivery_generations
                    WHERE session_epoch = ? AND mid = ?
                        AND active_identity = 1
                    """,
                    (retry_message.session_epoch, retry_message.mid),
                ).fetchall()
                for row in previous:
                    delivery_id = str(row["delivery_id"])
                    superseded_delivery_ids.append(delivery_id)
                    connection.execute(
                        """
                        UPDATE mqtt_retry_queue
                        SET qos = 0
                        WHERE message_key IN (?, ?)
                        """,
                        (f"ingest:{delivery_id}", f"quarantine:{delivery_id}"),
                    )
                connection.execute(
                    """
                    UPDATE mqtt_delivery_generations
                    SET active_identity = 0, updated_at = ?
                    WHERE session_epoch = ? AND mid = ?
                        AND active_identity = 1
                    """,
                    (now, retry_message.session_epoch, retry_message.mid),
                )
                delivery_id = secrets.token_hex(16)
                connection.execute(
                    """
                    INSERT INTO mqtt_delivery_generations (
                        delivery_id, session_epoch, mid, topic, payload_hash,
                        state, outcome, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'active', '', ?, ?)
                    """,
                    (
                        delivery_id,
                        retry_message.session_epoch,
                        retry_message.mid,
                        retry_message.topic,
                        payload_hash,
                        now,
                        now,
                    ),
                )
        if superseded_delivery_ids:
            with self._target_lock:
                for old_delivery_id in superseded_delivery_ids:
                    self._ack_targets.pop(f"ingest:{old_delivery_id}", None)
                    self._ack_targets.pop(f"quarantine:{old_delivery_id}", None)
        return self._snapshot(message, delivery_id=delivery_id)

    def _delivery_state_for(self, delivery_id: str) -> tuple[str, str] | None:
        if not delivery_id:
            return None
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT state, outcome FROM mqtt_delivery_generations
                WHERE delivery_id = ?
                """,
                (delivery_id,),
            ).fetchone()
        if row is None:
            return None
        return str(row["state"]), str(row["outcome"])

    def _set_delivery_state(
        self, delivery_id: str, state: str, outcome: str = ""
    ) -> None:
        if not delivery_id:
            return
        with self._connection() as connection:
            updated = connection.execute(
                """
                UPDATE mqtt_delivery_generations
                SET state = CASE
                        WHEN state = 'acked' AND ? = 'terminal' THEN state
                        ELSE ?
                    END,
                    outcome = CASE
                        WHEN outcome != '' AND ? = '' THEN outcome
                        ELSE ?
                    END,
                    updated_at = ?
                WHERE delivery_id = ?
                """,
                (state, state, outcome, outcome, time.time(), delivery_id),
            )
        if updated.rowcount != 1:
            raise sqlite3.OperationalError("MQTT delivery generation is unavailable")

    def _delete_delivery_rows(self, delivery_id: str) -> None:
        if not delivery_id:
            return
        with self._connection() as connection:
            connection.execute(
                """
                DELETE FROM mqtt_retry_queue
                WHERE message_key IN (?, ?)
                """,
                (f"ingest:{delivery_id}", f"quarantine:{delivery_id}"),
            )

    @staticmethod
    def _delivery_id_from_key(message_key: str) -> str:
        operation, separator, delivery_id = message_key.partition(":")
        if separator and operation in {"ingest", "quarantine"}:
            return delivery_id
        return ""

    @staticmethod
    def _message_key(operation: str, message: RetryMessage) -> str:
        if message.delivery_id:
            return f"{operation}:{message.delivery_id}"
        digest = hashlib.sha256()
        digest.update(message.session_epoch.encode("ascii"))
        digest.update(b"\0")
        digest.update(operation.encode("utf-8"))
        digest.update(b"\0")
        digest.update(message.topic.encode("utf-8"))
        digest.update(b"\0")
        digest.update(message.payload)
        digest.update(b"\0")
        digest.update(str(message.mid).encode("ascii"))
        return digest.hexdigest()

    @staticmethod
    def _pre_epoch_message_key(operation: str, message: RetryMessage) -> str:
        digest = hashlib.sha256()
        digest.update(operation.encode("utf-8"))
        digest.update(b"\0")
        digest.update(message.topic.encode("utf-8"))
        digest.update(b"\0")
        digest.update(message.payload)
        digest.update(b"\0")
        digest.update(str(message.mid).encode("ascii"))
        return digest.hexdigest()

    def delivery_idempotency_key(
        self,
        message: Any,
        operation: str,
        retry_message: RetryMessage | None = None,
    ) -> str:
        prepared = retry_message or self.prepare_delivery(message)
        return self._message_key(operation, prepared)

    @staticmethod
    def _legacy_message_key(operation: str, message: RetryMessage) -> str:
        digest = hashlib.sha256()
        digest.update(operation.encode("utf-8"))
        digest.update(b"\0")
        digest.update(message.topic.encode("utf-8"))
        digest.update(b"\0")
        digest.update(message.payload)
        return digest.hexdigest()

    def enqueue(
        self,
        client: Any,
        message: Any,
        error: MqttBridgeError,
        *,
        operation: str = "ingest",
        delivery_completed: bool = False,
        ack_pending: bool = False,
        retry_message: RetryMessage | None = None,
    ) -> bool:
        if operation not in {"ingest", "quarantine"}:
            raise ValueError(f"Unsupported retry operation: {operation}")
        try:
            prepared_message = retry_message or self.prepare_delivery(message)
        except sqlite3.Error:
            LOGGER.exception("mqtt_delivery_prepare_failed topic=%s", message.topic)
            return False
        retry_message = prepared_message
        message_key = self._message_key(operation, retry_message)
        now = time.time()
        delivery_outcome = (
            ("quarantined" if operation == "quarantine" else "accepted")
            if delivery_completed
            else ""
        )
        ack_pending = ack_pending or error.code == "MQTT_ACK_FAILED"
        last_error_code = "MQTT_ACK_FAILED" if ack_pending else error.code
        last_error_message = "MQTT ACK failed." if ack_pending else error.message
        with self._target_lock:
            self._ack_targets[message_key] = (client, retry_message)
        if retry_message.dup:
            conflict_action = """
                mid = excluded.mid,
                qos = excluded.qos,
                next_attempt_at = MIN(
                    mqtt_retry_queue.next_attempt_at,
                    excluded.next_attempt_at
                ),
                last_error_code = excluded.last_error_code,
                last_error_message = excluded.last_error_message,
                delivery_completed = MAX(
                    mqtt_retry_queue.delivery_completed,
                    excluded.delivery_completed
                ),
                delivery_outcome = CASE
                    WHEN mqtt_retry_queue.delivery_completed = 1
                    THEN mqtt_retry_queue.delivery_outcome
                    ELSE excluded.delivery_outcome
                END
            """
        else:
            conflict_action = """
                mid = excluded.mid,
                qos = excluded.qos,
                attempts = 0,
                next_attempt_at = excluded.next_attempt_at,
                error_code = excluded.error_code,
                error_message = excluded.error_message,
                last_error_code = excluded.last_error_code,
                last_error_message = excluded.last_error_message,
                delivery_completed = excluded.delivery_completed,
                delivery_outcome = excluded.delivery_outcome,
                claim_owner = '',
                claim_token = 0,
                claim_until = 0,
                created_at = excluded.created_at
            """
        try:
            if delivery_completed:
                self._set_delivery_state(
                    retry_message.delivery_id, "terminal", delivery_outcome
                )
            with self._connection() as connection:
                connection.execute(
                    f"""
                    INSERT INTO mqtt_retry_queue (
                        message_key, operation, topic, payload, mid, qos,
                        attempts, next_attempt_at, error_code, error_message,
                        last_error_code, last_error_message, delivery_completed,
                        delivery_outcome, session_epoch, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(message_key) DO UPDATE SET
                        {conflict_action}
                    """,
                    (
                        message_key,
                        operation,
                        retry_message.topic,
                        sqlite3.Binary(retry_message.payload),
                        retry_message.mid,
                        retry_message.qos,
                        now + self.initial_delay,
                        error.code,
                        error.message,
                        last_error_code,
                        last_error_message,
                        int(delivery_completed),
                        delivery_outcome,
                        retry_message.session_epoch,
                        now,
                    ),
                )
        except sqlite3.Error:
            with self._target_lock:
                self._ack_targets.pop(message_key, None)
            LOGGER.exception("mqtt_retry_enqueue_failed topic=%s", message.topic)
            return False
        self._wake_event.set()
        LOGGER.warning(
            "mqtt_retry_queued operation=%s topic=%s code=%s",
            operation,
            retry_message.topic,
            error.code,
        )
        return True

    def pending_count(self) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM mqtt_retry_queue"
            ).fetchone()
        return int(row["count"])

    def resume_pending_message(
        self,
        client: Any,
        message: Any,
        *,
        retry_message: RetryMessage | None = None,
    ) -> str | None:
        retry_message = retry_message or self.prepare_delivery(message)
        if not retry_message.dup:
            return None
        if retry_message.delivery_state in {"terminal", "acked"}:
            outcome = retry_message.delivery_outcome or "accepted"
            if not acknowledge_message(client, message):
                self.enqueue(
                    client,
                    message,
                    MqttBridgeError(503, "MQTT_ACK_FAILED", "MQTT ACK failed."),
                    operation=("quarantine" if outcome == "quarantined" else "ingest"),
                    delivery_completed=True,
                    ack_pending=True,
                    retry_message=retry_message,
                )
                return "retry"
            try:
                self._set_delivery_state(retry_message.delivery_id, "acked", outcome)
                self._delete_delivery_rows(retry_message.delivery_id)
            except sqlite3.Error as error:
                LOGGER.warning(
                    "mqtt_delivery_ack_cleanup_failed delivery=%s error=%s",
                    retry_message.delivery_id,
                    error,
                )
            return outcome
        message_keys = tuple(
            self._message_key(operation, retry_message)
            for operation in ("quarantine", "ingest")
        )
        legacy_message_keys = tuple(
            self._legacy_message_key(operation, retry_message)
            for operation in ("quarantine", "ingest")
        )
        pre_epoch_message_keys = tuple(
            self._pre_epoch_message_key(operation, retry_message)
            for operation in ("quarantine", "ingest")
        )
        try:
            with self._processing_lock:
                with self._connection() as connection:
                    row = connection.execute(
                        """
                        SELECT * FROM mqtt_retry_queue
                        WHERE message_key IN (?, ?)
                        ORDER BY delivery_completed DESC, created_at
                        LIMIT 1
                        """,
                        message_keys,
                    ).fetchone()
                    if row is None:
                        row = connection.execute(
                            """
                            SELECT * FROM mqtt_retry_queue
                            WHERE message_key IN (?, ?, ?, ?)
                                AND mid = ? AND session_epoch = ?
                            ORDER BY delivery_completed DESC, created_at
                            LIMIT 1
                            """,
                            (
                                *pre_epoch_message_keys,
                                *legacy_message_keys,
                                retry_message.mid,
                                retry_message.session_epoch,
                            ),
                        ).fetchone()
                    if row is None:
                        return None
                    bound_message_key = self._message_key(
                        str(row["operation"]), retry_message
                    )
                    if str(row["message_key"]) != bound_message_key:
                        rebound = connection.execute(
                            """
                            UPDATE mqtt_retry_queue
                            SET message_key = ?, session_epoch = ?
                            WHERE message_key = ?
                            """,
                            (
                                bound_message_key,
                                retry_message.session_epoch,
                                row["message_key"],
                            ),
                        )
                        if rebound.rowcount != 1:
                            return "retry"
                        row = connection.execute(
                            "SELECT * FROM mqtt_retry_queue WHERE message_key = ?",
                            (bound_message_key,),
                        ).fetchone()
                        if row is None:
                            return "retry"
                    connection.execute(
                        """
                        UPDATE mqtt_retry_queue
                        SET qos = ?, next_attempt_at = ?
                        WHERE message_key = ?
                        """,
                        (
                            retry_message.qos,
                            time.time(),
                            row["message_key"],
                        ),
                    )
                with self._target_lock:
                    message_key = str(row["message_key"])
                    self._ack_targets[message_key] = (client, retry_message)
                self._wake_event.set()
                if bool(row["delivery_completed"]):
                    if message_key in self._inflight_message_keys:
                        return "retry"
                    outcome = str(row["delivery_outcome"]) or (
                        "quarantined"
                        if row["operation"] == "quarantine"
                        else "accepted"
                    )
                    return self._complete(row, outcome)
                LOGGER.info(
                    "mqtt_retry_reconnected operation=%s topic=%s mid=%s qos=%s",
                    row["operation"],
                    retry_message.topic,
                    retry_message.mid,
                    retry_message.qos,
                )
                return "retry"
        except sqlite3.Error as error:
            LOGGER.warning(
                "mqtt_retry_lookup_failed topic=%s mid=%s error=%s",
                retry_message.topic,
                retry_message.mid,
                error,
            )
            return "retry"

    def _reschedule(self, row: sqlite3.Row, error: MqttBridgeError) -> None:
        attempts = int(row["attempts"]) + 1
        delay = min(
            self.max_delay,
            self.initial_delay * (2 ** min(max(0, attempts - 1), 10)),
        )
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE mqtt_retry_queue
                SET attempts = ?, next_attempt_at = ?,
                    last_error_code = ?, last_error_message = ?,
                    claim_owner = '', claim_token = 0, claim_until = 0
                WHERE message_key = ?
                """,
                (
                    attempts,
                    time.time() + delay,
                    error.code,
                    error.message,
                    row["message_key"],
                ),
            )
        LOGGER.warning(
            "mqtt_retry_scheduled operation=%s topic=%s attempt=%s delay=%.1f code=%s",
            row["operation"],
            row["topic"],
            attempts,
            delay,
            error.code,
        )

    def _mark_delivery_completed(self, row: sqlite3.Row, outcome: str) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE mqtt_retry_queue
                SET delivery_completed = 1, delivery_outcome = ?
                WHERE message_key = ?
                """,
                (outcome, row["message_key"]),
            )

    def _defer_until_ack_target(self, row: sqlite3.Row) -> None:
        delay = max(0.5, self.poll_interval, self.initial_delay)
        with self._connection() as connection:
            connection.execute(
                """
                UPDATE mqtt_retry_queue
                SET next_attempt_at = ?, claim_owner = '',
                    claim_token = 0, claim_until = 0
                WHERE message_key = ?
                """,
                (time.time() + delay, row["message_key"]),
            )
        LOGGER.info(
            "mqtt_retry_awaiting_ack_target operation=%s topic=%s qos=%s",
            row["operation"],
            row["topic"],
            row["qos"],
        )

    def _delete_claimed_row(self, message_key: str, claim_token: int) -> bool:
        with self._connection() as connection:
            deleted = connection.execute(
                """
                DELETE FROM mqtt_retry_queue
                WHERE message_key = ? AND claim_owner = ? AND claim_token = ?
                """,
                (message_key, self._claim_owner, claim_token),
            )
        return deleted.rowcount == 1

    def _complete(self, row: sqlite3.Row, outcome: str) -> str:
        message_key = str(row["message_key"])
        delivery_id = self._delivery_id_from_key(message_key)
        if delivery_id:
            self._set_delivery_state(delivery_id, "terminal", outcome)
        if not bool(row["delivery_completed"]) or not str(row["delivery_outcome"]):
            self._mark_delivery_completed(row, outcome)
        with self._target_lock:
            target = self._ack_targets.get(message_key)
        if target is None and int(row["qos"]) > 0:
            if not delivery_id or str(row["last_error_code"]) == "MQTT_ACK_FAILED":
                self._defer_until_ack_target(row)
                return "awaiting_ack"
            try:
                with self._connection() as connection:
                    connection.execute(
                        "DELETE FROM mqtt_retry_queue WHERE message_key = ?",
                        (message_key,),
                    )
            except sqlite3.Error as error:
                LOGGER.warning(
                    "mqtt_retry_terminal_cleanup_failed key=%s error=%s",
                    message_key,
                    error,
                )
            return outcome
        if target is not None:
            client, message = target
            if not acknowledge_message(client, message):
                self._reschedule(
                    row,
                    MqttBridgeError(503, "MQTT_ACK_FAILED", "MQTT ACK failed."),
                )
                return "retry"
        try:
            with self._connection() as connection:
                connection.execute(
                    "DELETE FROM mqtt_retry_queue WHERE message_key = ?",
                    (message_key,),
                )
        except sqlite3.Error as error:
            LOGGER.warning(
                "mqtt_retry_ack_cleanup_failed key=%s error=%s",
                message_key,
                error,
            )
        with self._target_lock:
            self._ack_targets.pop(message_key, None)
        if delivery_id:
            try:
                self._set_delivery_state(delivery_id, "acked", outcome)
            except sqlite3.Error as error:
                LOGGER.warning(
                    "mqtt_delivery_ack_cleanup_failed delivery=%s error=%s",
                    delivery_id,
                    error,
                )
        LOGGER.info(
            "mqtt_retry_completed operation=%s topic=%s outcome=%s",
            row["operation"],
            row["topic"],
            outcome,
        )
        return outcome

    def _claim_next_due(self, now: float) -> sqlite3.Row | None:
        claimed_at = time.time()
        claim_until = claimed_at + self.lease_seconds
        with self._processing_lock:
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT * FROM mqtt_retry_queue
                    WHERE next_attempt_at <= ? AND claim_until <= ?
                    ORDER BY next_attempt_at, created_at, message_key
                    LIMIT 1
                    """,
                    (now, claimed_at),
                ).fetchone()
                if row is None:
                    return None
                message_key = str(row["message_key"])
                connection.execute(
                    """
                    UPDATE mqtt_retry_state
                    SET value = CAST(value AS INTEGER) + 1
                    WHERE key = 'claim_sequence'
                    """
                )
                claim_token = int(
                    connection.execute(
                        "SELECT value FROM mqtt_retry_state "
                        "WHERE key = 'claim_sequence'"
                    ).fetchone()["value"]
                )
                claimed = connection.execute(
                    """
                    UPDATE mqtt_retry_queue
                    SET claim_owner = ?, claim_token = ?, claim_until = ?
                    WHERE message_key = ? AND claim_until <= ?
                    """,
                    (
                        self._claim_owner,
                        claim_token,
                        claim_until,
                        message_key,
                        claimed_at,
                    ),
                )
                if claimed.rowcount != 1:
                    return None
                row = connection.execute(
                    """
                    SELECT * FROM mqtt_retry_queue
                    WHERE message_key = ? AND claim_owner = ? AND claim_token = ?
                    """,
                    (message_key, self._claim_owner, claim_token),
                ).fetchone()
                if row is None:
                    return None
            self._inflight_message_keys.add(message_key)
            return row

    def _release_claim(self, row: sqlite3.Row) -> None:
        with self._processing_lock:
            message_key = str(row["message_key"])
            try:
                with self._connection() as connection:
                    connection.execute(
                        """
                        UPDATE mqtt_retry_queue
                        SET claim_owner = '', claim_token = 0, claim_until = 0
                        WHERE message_key = ? AND claim_owner = ?
                            AND claim_token = ?
                        """,
                        (message_key, self._claim_owner, int(row["claim_token"])),
                    )
            except sqlite3.Error as error:
                LOGGER.warning(
                    "mqtt_retry_claim_release_failed key=%s error=%s",
                    message_key,
                    error,
                )
            self._inflight_message_keys.discard(message_key)

    def _renew_claim(self, row: sqlite3.Row) -> bool:
        with self._connection() as connection:
            renewed = connection.execute(
                """
                UPDATE mqtt_retry_queue
                SET claim_until = ?
                WHERE message_key = ? AND claim_owner = ? AND claim_token = ?
                """,
                (
                    time.time() + self.lease_seconds,
                    row["message_key"],
                    self._claim_owner,
                    int(row["claim_token"]),
                ),
            )
        return renewed.rowcount == 1

    def _keep_claim_alive(self, row: sqlite3.Row, stop_event: threading.Event) -> None:
        interval = max(0.1, min(5.0, self.lease_seconds / 4))
        while not stop_event.wait(interval):
            try:
                if not self._renew_claim(row):
                    LOGGER.warning(
                        "mqtt_retry_claim_lost key=%s owner=%s",
                        row["message_key"],
                        self._claim_owner,
                    )
                    return
            except sqlite3.Error as error:
                LOGGER.warning(
                    "mqtt_retry_claim_renew_failed key=%s error=%s",
                    row["message_key"],
                    error,
                )

    def _complete_claimed(self, row: sqlite3.Row, outcome: str) -> str:
        message_key = str(row["message_key"])
        delivery_id = self._delivery_id_from_key(message_key)
        if delivery_id:
            try:
                self._set_delivery_state(delivery_id, "terminal", outcome)
            except sqlite3.Error as error:
                LOGGER.warning(
                    "mqtt_delivery_terminal_failed delivery=%s error=%s",
                    delivery_id,
                    error,
                )
                return "retry"
        with self._processing_lock:
            try:
                claim_token = int(row["claim_token"])
                with self._connection() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    current = connection.execute(
                        """
                        SELECT * FROM mqtt_retry_queue
                        WHERE message_key = ? AND claim_owner = ?
                            AND claim_token = ?
                        """,
                        (message_key, self._claim_owner, claim_token),
                    ).fetchone()
                    if current is None:
                        return "retry"
                    completed = connection.execute(
                        """
                        UPDATE mqtt_retry_queue
                        SET delivery_completed = 1, delivery_outcome = ?,
                            claim_until = ?
                        WHERE message_key = ? AND claim_owner = ?
                            AND claim_token = ?
                        """,
                        (
                            outcome,
                            time.time() + self.lease_seconds,
                            message_key,
                            self._claim_owner,
                            claim_token,
                        ),
                    )
                    if completed.rowcount != 1:
                        return "retry"
                with self._target_lock:
                    target = self._ack_targets.get(message_key)
                if target is None:
                    if int(current["qos"]) > 0 and (
                        not delivery_id
                        or str(current["last_error_code"]) == "MQTT_ACK_FAILED"
                    ):
                        delay = max(0.5, self.poll_interval, self.initial_delay)
                        with self._connection() as connection:
                            deferred = connection.execute(
                                """
                                UPDATE mqtt_retry_queue
                                SET next_attempt_at = ?, claim_owner = '',
                                    claim_token = 0, claim_until = 0
                                WHERE message_key = ? AND claim_owner = ?
                                    AND claim_token = ?
                                """,
                                (
                                    time.time() + delay,
                                    message_key,
                                    self._claim_owner,
                                    claim_token,
                                ),
                            )
                        if deferred.rowcount != 1:
                            return "retry"
                        LOGGER.info(
                            "mqtt_retry_awaiting_ack_target operation=%s "
                            "topic=%s qos=%s",
                            current["operation"],
                            current["topic"],
                            current["qos"],
                        )
                        return "awaiting_ack"
                    if not self._delete_claimed_row(message_key, claim_token):
                        return "retry"
                    return outcome
                with self._connection() as connection:
                    fenced = connection.execute(
                        """
                        UPDATE mqtt_retry_queue
                        SET claim_until = ?
                        WHERE message_key = ? AND claim_owner = ?
                            AND claim_token = ?
                        """,
                        (
                            time.time() + self.lease_seconds,
                            message_key,
                            self._claim_owner,
                            claim_token,
                        ),
                    )
                if fenced.rowcount != 1:
                    return "retry"
                client, message = target
                if not acknowledge_message(client, message):
                    attempts = int(current["attempts"]) + 1
                    delay = min(
                        self.max_delay,
                        self.initial_delay * (2 ** min(max(0, attempts - 1), 10)),
                    )
                    with self._connection() as connection:
                        rescheduled = connection.execute(
                            """
                            UPDATE mqtt_retry_queue
                            SET attempts = ?, next_attempt_at = ?,
                                last_error_code = 'MQTT_ACK_FAILED',
                                last_error_message = 'MQTT ACK failed.',
                                claim_owner = '', claim_token = 0,
                                claim_until = 0
                            WHERE message_key = ? AND claim_owner = ?
                                AND claim_token = ?
                            """,
                            (
                                attempts,
                                time.time() + delay,
                                message_key,
                                self._claim_owner,
                                claim_token,
                            ),
                        )
                    if rescheduled.rowcount != 1:
                        return "retry"
                    return "retry"
                try:
                    if not self._delete_claimed_row(message_key, claim_token):
                        return "retry"
                except sqlite3.Error as error:
                    LOGGER.warning(
                        "mqtt_retry_ack_cleanup_failed key=%s error=%s",
                        message_key,
                        error,
                    )
                with self._target_lock:
                    self._ack_targets.pop(message_key, None)
                if delivery_id:
                    try:
                        self._set_delivery_state(delivery_id, "acked", outcome)
                    except sqlite3.Error as error:
                        LOGGER.warning(
                            "mqtt_delivery_ack_cleanup_failed delivery=%s error=%s",
                            delivery_id,
                            error,
                        )
                LOGGER.info(
                    "mqtt_retry_completed operation=%s topic=%s outcome=%s",
                    row["operation"],
                    row["topic"],
                    outcome,
                )
                return outcome
            finally:
                self._inflight_message_keys.discard(str(row["message_key"]))

    def _reschedule_claimed(self, row: sqlite3.Row, error: MqttBridgeError) -> str:
        with self._processing_lock:
            try:
                attempts = int(row["attempts"]) + 1
                delay = min(
                    self.max_delay,
                    self.initial_delay * (2 ** min(max(0, attempts - 1), 10)),
                )
                with self._connection() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    updated = connection.execute(
                        """
                        UPDATE mqtt_retry_queue
                        SET attempts = ?, next_attempt_at = ?,
                            last_error_code = ?, last_error_message = ?,
                            claim_owner = '', claim_token = 0, claim_until = 0
                        WHERE message_key = ? AND claim_owner = ?
                            AND claim_token = ?
                        """,
                        (
                            attempts,
                            time.time() + delay,
                            error.code,
                            error.message,
                            row["message_key"],
                            self._claim_owner,
                            int(row["claim_token"]),
                        ),
                    )
                    if updated.rowcount != 1:
                        return "retry"
                LOGGER.warning(
                    "mqtt_retry_scheduled operation=%s topic=%s attempt=%s "
                    "delay=%.1f code=%s",
                    row["operation"],
                    row["topic"],
                    attempts,
                    delay,
                    error.code,
                )
                return "retry"
            finally:
                self._inflight_message_keys.discard(str(row["message_key"]))

    def _process_due_once(self, row: sqlite3.Row) -> str:
        lease_stop = threading.Event()
        lease_worker = threading.Thread(
            target=self._keep_claim_alive,
            args=(row, lease_stop),
            name="mqtt-retry-lease",
            daemon=True,
        )
        lease_worker.start()
        try:
            delivery_id = self._delivery_id_from_key(str(row["message_key"]))
            delivery_state = self._delivery_state_for(delivery_id)
            if delivery_state is not None and delivery_state[0] in {
                "terminal",
                "acked",
            }:
                outcome = delivery_state[1] or (
                    "quarantined" if row["operation"] == "quarantine" else "accepted"
                )
                return self._complete_claimed(row, outcome)
            if bool(row["delivery_completed"]):
                outcome = str(row["delivery_outcome"]) or (
                    "quarantined" if row["operation"] == "quarantine" else "accepted"
                )
                return self._complete_claimed(row, outcome)
            if row["operation"] == "quarantine":
                quarantine_local_mqtt_message(
                    self.quarantine_endpoint,
                    self.token,
                    str(row["topic"]),
                    bytes(row["payload"]),
                    MqttBridgeError(
                        400,
                        str(row["error_code"]),
                        str(row["error_message"]),
                        local=True,
                    ),
                    idempotency_key=str(row["message_key"]),
                )
                return self._complete_claimed(row, "quarantined")

            forward_mqtt_message(
                str(row["topic"]),
                bytes(row["payload"]),
                endpoint=self.ingest_endpoint,
                token=self.token,
            )
            return self._complete_claimed(row, "accepted")
        except MqttBridgeError as error:
            if row["operation"] == "ingest" and is_permanent_ingest_error(error):
                return self._complete_claimed(row, "quarantined")
            return self._reschedule_claimed(row, error)
        finally:
            lease_stop.set()
            lease_worker.join(timeout=1)
            self._release_claim(row)

    def process_due_once(self, *, now: float | None = None) -> str:
        if not self._session_ready.is_set():
            return "idle"
        row = self._claim_next_due(time.time() if now is None else now)
        if row is None:
            return "idle"
        return self._process_due_once(row)

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.process_due_once()
            except (OSError, sqlite3.Error):
                LOGGER.exception("mqtt_retry_worker_failed")
            self._wake_event.wait(self.poll_interval)
            self._wake_event.clear()

    def start(self) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._stop_event.clear()
        self._worker = threading.Thread(
            target=self._run,
            name="mqtt-http-retry",
            daemon=True,
        )
        self._worker.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self._worker:
            self._worker.join(timeout=5)

    def complete_live_message(
        self,
        client: Any,
        message: Any,
        retry_message: RetryMessage,
        *,
        outcome: str,
        operation: str,
        error: MqttBridgeError,
    ) -> str:
        try:
            self._set_delivery_state(retry_message.delivery_id, "terminal", outcome)
        except sqlite3.Error as state_error:
            LOGGER.warning(
                "mqtt_delivery_terminal_failed delivery=%s error=%s",
                retry_message.delivery_id,
                state_error,
            )
            return "retry"
        if not acknowledge_message(client, message):
            self.enqueue(
                client,
                message,
                error,
                operation=operation,
                delivery_completed=True,
                ack_pending=True,
                retry_message=retry_message,
            )
            return "retry"
        try:
            self._set_delivery_state(retry_message.delivery_id, "acked", outcome)
        except sqlite3.Error as state_error:
            LOGGER.warning(
                "mqtt_delivery_ack_cleanup_failed delivery=%s error=%s",
                retry_message.delivery_id,
                state_error,
            )
        return outcome


def process_mqtt_message(
    client: Any,
    message: Any,
    *,
    endpoint: str,
    token: str,
    quarantine_endpoint: str | None = None,
    retry_queue: MqttRetryQueue | None = None,
) -> str:
    """Return accepted, quarantined, or retry based on HTTP processing outcome."""
    retry_message = None
    if retry_queue:
        try:
            retry_message = retry_queue.prepare_delivery(message)
        except sqlite3.Error as error:
            LOGGER.warning(
                "mqtt_delivery_prepare_failed topic=%s error=%s",
                message.topic,
                error,
            )
            return "retry"
        resumed = retry_queue.resume_pending_message(
            client, message, retry_message=retry_message
        )
        if resumed is not None:
            return resumed
    try:
        response, status = forward_mqtt_message(
            message.topic,
            message.payload,
            endpoint=endpoint,
            token=token,
        )
    except MqttBridgeError as error:
        if is_permanent_ingest_error(error):
            if error.local:
                try:
                    if not quarantine_endpoint:
                        raise MqttBridgeError(
                            503,
                            "QUARANTINE_ENDPOINT_UNAVAILABLE",
                            "A quarantine endpoint is required before MQTT ACK.",
                        )
                    idempotency_key = (
                        retry_queue.delivery_idempotency_key(
                            message, "quarantine", retry_message
                        )
                        if retry_queue
                        else None
                    )
                    quarantine_local_mqtt_message(
                        quarantine_endpoint,
                        token,
                        message.topic,
                        message.payload,
                        error,
                        idempotency_key=idempotency_key,
                    )
                except MqttBridgeError as quarantine_error:
                    queued = bool(
                        retry_queue
                        and retry_queue.enqueue(
                            client,
                            message,
                            error,
                            operation="quarantine",
                            retry_message=retry_message,
                        )
                    )
                    LOGGER.warning(
                        "mqtt_quarantine_retry topic=%s status=%s code=%s queued=%s",
                        message.topic,
                        quarantine_error.status,
                        quarantine_error.code,
                        queued,
                    )
                    return "retry"
            if retry_queue and retry_message:
                return retry_queue.complete_live_message(
                    client,
                    message,
                    retry_message,
                    outcome="quarantined",
                    operation="quarantine",
                    error=error,
                )
            acknowledged = acknowledge_message(client, message)
            if not acknowledged and retry_queue:
                retry_queue.enqueue(
                    client,
                    message,
                    error,
                    operation="quarantine",
                    delivery_completed=True,
                )
            LOGGER.warning(
                "mqtt_ingest_quarantined topic=%s status=%s code=%s message=%s",
                message.topic,
                error.status,
                error.code,
                error.message,
            )
            return "quarantined" if acknowledged else "retry"
        queued = bool(
            retry_queue
            and retry_queue.enqueue(
                client,
                message,
                error,
                retry_message=retry_message,
            )
        )
        LOGGER.warning(
            "mqtt_ingest_retry topic=%s status=%s code=%s message=%s queued=%s",
            message.topic,
            error.status,
            error.code,
            error.message,
            queued,
        )
        return "retry"

    if retry_queue and retry_message:
        outcome = retry_queue.complete_live_message(
            client,
            message,
            retry_message,
            outcome="accepted",
            operation="ingest",
            error=MqttBridgeError(503, "MQTT_ACK_FAILED", "MQTT ACK failed."),
        )
        if outcome == "retry":
            return outcome
    elif not acknowledge_message(client, message):
        if retry_queue:
            retry_queue.enqueue(
                client,
                message,
                MqttBridgeError(503, "MQTT_ACK_FAILED", "MQTT ACK failed."),
                delivery_completed=True,
            )
        return "retry"
    LOGGER.info(
        "mqtt_ingest status=%s device=%s sequence=%s duplicate=%s",
        status,
        response["deviceId"],
        response["sequence"],
        response["duplicate"],
    )
    return "accepted"


def report_mqtt_status_safely(
    *,
    endpoint: str,
    token: str,
    status: str,
    detail: str,
    error_code: str | None = None,
) -> None:
    try:
        report_mqtt_status(
            endpoint,
            token,
            status,
            detail=detail,
            error_code=error_code,
        )
    except MqttBridgeError as error:
        LOGGER.warning(
            "mqtt_status_report_failed status=%s code=%s message=%s",
            error.status,
            error.code,
            error.message,
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Subscribe to MQTT/TLS telemetry and forward it to the ingest API."
    )
    parser.add_argument("--host", default=os.environ.get("MQTT_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("MQTT_PORT", "8883"))
    )
    parser.add_argument("--topic", default="devices/+/telemetry")
    parser.add_argument(
        "--qos",
        type=int,
        choices=(SUPPORTED_SUBSCRIPTION_QOS,),
        default=SUPPORTED_SUBSCRIPTION_QOS,
        help="Subscription QoS. Only QoS 1 is supported across process restarts.",
    )
    parser.add_argument("--client-id", default="bind-edge-ai-backend")
    parser.add_argument("--username", default=os.environ.get("MQTT_USERNAME"))
    parser.add_argument("--password", default=os.environ.get("MQTT_PASSWORD"))
    parser.add_argument("--ca-cert", default=os.environ.get("MQTT_CA_CERT"))
    parser.add_argument("--client-cert", default=os.environ.get("MQTT_CLIENT_CERT"))
    parser.add_argument("--client-key", default=os.environ.get("MQTT_CLIENT_KEY"))
    parser.add_argument(
        "--ingest-token",
        default=os.environ.get("MQTT_INGEST_TOKEN", "demo-mqtt-ingest-token"),
    )
    parser.add_argument(
        "--ingest-endpoint",
        default=os.environ.get(
            "TELEMETRY_INGEST_ENDPOINT",
            "http://127.0.0.1:8787/api/telemetry/ingest",
        ),
    )
    parser.add_argument(
        "--health-endpoint",
        default=os.environ.get(
            "MQTT_HEALTH_ENDPOINT",
            "http://127.0.0.1:8787/api/health/dependencies/mqtt",
        ),
    )
    parser.add_argument(
        "--quarantine-endpoint",
        default=os.environ.get(
            "MQTT_QUARANTINE_ENDPOINT",
            "http://127.0.0.1:8787/api/telemetry/quarantine",
        ),
    )
    parser.add_argument(
        "--retry-db",
        default=os.environ.get("MQTT_RETRY_DB", "output/mqtt_retry.sqlite3"),
        help="SQLite file used to preserve pending HTTP deliveries across restarts.",
    )
    parser.add_argument(
        "--tls-insecure",
        action="store_true",
        help="Disable broker hostname verification for local testing only.",
    )
    return parser.parse_args()


def mqtt_reason_failed(reason_code: Any) -> bool:
    is_failure = getattr(reason_code, "is_failure", None)
    if is_failure is not None:
        return bool(is_failure)
    try:
        return int(reason_code) >= 128
    except (TypeError, ValueError):
        return True


def mqtt_reason_value(reason_code: Any) -> int | None:
    value = getattr(reason_code, "value", reason_code)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def mqtt_session_present(connect_flags: Any) -> bool:
    if isinstance(connect_flags, dict):
        return bool(
            connect_flags.get("session_present") or connect_flags.get("session present")
        )
    return bool(getattr(connect_flags, "session_present", False))


def subscription_reason_meets_qos(reason_code: Any, requested_qos: int) -> bool:
    granted_qos = mqtt_reason_value(reason_code)
    if granted_qos is None:
        return False
    return (
        not mqtt_reason_failed(reason_code)
        and granted_qos in {0, 1, 2}
        and granted_qos >= requested_qos
    )


def subscription_is_granted(reason_code_list: Any, requested_qos: int) -> bool:
    if requested_qos != SUPPORTED_SUBSCRIPTION_QOS:
        return False
    reason_codes = list(reason_code_list or [])
    return bool(reason_codes) and all(
        subscription_reason_meets_qos(reason_code, requested_qos)
        for reason_code in reason_codes
    )


def configure_mqtt_callbacks(
    client: Any,
    args: argparse.Namespace,
    retry_queue: MqttRetryQueue,
) -> set[int]:
    if int(args.qos) != SUPPORTED_SUBSCRIPTION_QOS:
        raise ValueError(
            "Only MQTT QoS 1 is supported because inbound QoS 2 state is not "
            "durable across process restarts."
        )
    pending_subscriptions: set[int] = set()
    activation_lock = threading.Lock()
    activation_generation = 0

    def current_activation_generation() -> int:
        nonlocal activation_generation
        with activation_lock:
            return activation_generation

    def advance_activation_generation() -> int:
        nonlocal activation_generation
        with activation_lock:
            activation_generation += 1
            return activation_generation

    def activate_session(
        client: Any,
        flags: Any,
        generation: int,
        attempt: int = 0,
    ) -> None:
        if generation != current_activation_generation():
            return
        try:
            retry_queue.begin_session(session_present=mqtt_session_present(flags))
        except sqlite3.Error as error:
            retry_queue.pause_session()
            delay = min(30.0, 0.25 * (2 ** min(attempt, 7)))
            LOGGER.error(
                "mqtt_session_epoch_failed error=%s retry_in=%.2f",
                error,
                delay,
            )
            report_mqtt_status_safely(
                endpoint=args.health_endpoint,
                token=args.ingest_token,
                status="degraded",
                detail=f"MQTT retry session state failed: {error}",
                error_code="MQTT_RETRY_STATE_FAILED",
            )
            timer = threading.Timer(
                delay,
                activate_session,
                args=(client, flags, generation, attempt + 1),
            )
            timer.daemon = True
            timer.start()
            return
        if generation != current_activation_generation():
            retry_queue.pause_session()
            return
        result, mid = client.subscribe(args.topic, qos=args.qos)
        if int(result) != 0:
            retry_queue.pause_session()
            LOGGER.error("mqtt_subscribe_failed topic=%s result=%s", args.topic, result)
            report_mqtt_status_safely(
                endpoint=args.health_endpoint,
                token=args.ingest_token,
                status="degraded",
                detail=f"Broker subscription request failed: {result}",
                error_code="MQTT_SUBSCRIBE_FAILED",
            )
            return
        pending_subscriptions.add(int(mid))
        LOGGER.info(
            "mqtt_subscribe_pending topic=%s qos=%s mid=%s",
            args.topic,
            args.qos,
            mid,
        )

    def on_connect(client, _userdata, flags, reason_code, _properties) -> None:
        retry_queue.pause_session()
        generation = advance_activation_generation()
        if mqtt_reason_failed(reason_code):
            LOGGER.error("mqtt_connect_failed reason=%s", reason_code)
            report_mqtt_status_safely(
                endpoint=args.health_endpoint,
                token=args.ingest_token,
                status="degraded",
                detail=f"Broker connection refused: {reason_code}",
                error_code="MQTT_CONNECT_REFUSED",
            )
            return
        activate_session(client, flags, generation)

    def on_subscribe(_client, _userdata, mid, reason_code_list, _properties) -> None:
        if int(mid) not in pending_subscriptions:
            LOGGER.info("mqtt_suback_stale mid=%s", mid)
            return
        pending_subscriptions.discard(int(mid))
        reason_codes = list(reason_code_list or [])
        if not subscription_is_granted(reason_codes, args.qos):
            reasons = ", ".join(str(code) for code in reason_codes)
            broker_rejected = not reason_codes or any(
                mqtt_reason_failed(code) for code in reason_codes
            )
            if broker_rejected:
                error_code = "MQTT_SUBSCRIBE_REJECTED"
                detail = (
                    "Broker rejected subscription: "
                    f"{reasons or 'missing SUBACK code'}"
                )
                LOGGER.error(
                    "mqtt_suback_rejected topic=%s mid=%s reasons=%s",
                    args.topic,
                    mid,
                    reasons or "missing",
                )
            else:
                granted_qos = [mqtt_reason_value(code) for code in reason_codes]
                error_code = "MQTT_SUBSCRIBE_QOS_DOWNGRADED"
                detail = (
                    f"Broker granted QoS {granted_qos} below requested "
                    f"QoS {args.qos}."
                )
                LOGGER.error(
                    "mqtt_suback_qos_downgraded topic=%s mid=%s "
                    "requested=%s granted=%s",
                    args.topic,
                    mid,
                    args.qos,
                    granted_qos,
                )
            report_mqtt_status_safely(
                endpoint=args.health_endpoint,
                token=args.ingest_token,
                status="degraded",
                detail=detail,
                error_code=error_code,
            )
            return
        report_mqtt_status_safely(
            endpoint=args.health_endpoint,
            token=args.ingest_token,
            status="healthy",
            detail=f"Subscribed to {args.topic} with QoS {args.qos}",
        )
        LOGGER.info(
            "mqtt_subscribed topic=%s qos=%s mid=%s",
            args.topic,
            args.qos,
            mid,
        )

    def on_connect_fail(_client, _userdata) -> None:
        LOGGER.error("mqtt_connect_failed reason=network")
        report_mqtt_status_safely(
            endpoint=args.health_endpoint,
            token=args.ingest_token,
            status="degraded",
            detail="Broker connection attempt failed.",
            error_code="MQTT_CONNECT_FAILED",
        )

    def on_disconnect(
        _client, _userdata, _disconnect_flags, reason_code, _properties
    ) -> None:
        advance_activation_generation()
        retry_queue.pause_session()
        pending_subscriptions.clear()
        LOGGER.warning("mqtt_disconnected reason=%s", reason_code)
        report_mqtt_status_safely(
            endpoint=args.health_endpoint,
            token=args.ingest_token,
            status="degraded",
            detail=f"Broker disconnected: {reason_code}",
            error_code="MQTT_DISCONNECTED",
        )

    def on_message(client, _userdata, message) -> None:
        process_mqtt_message(
            client,
            message,
            endpoint=args.ingest_endpoint,
            quarantine_endpoint=args.quarantine_endpoint,
            token=args.ingest_token,
            retry_queue=retry_queue,
        )

    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_connect_fail = on_connect_fail
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    return pending_subscriptions


def main() -> None:
    args = parse_args()
    if bool(args.client_cert) != bool(args.client_key):
        raise SystemExit("--client-cert and --client-key must be provided together")
    if not args.client_id.strip():
        raise SystemExit("--client-id is required for the persistent MQTT session")

    try:
        import paho.mqtt.client as mqtt
    except ImportError as exc:
        raise SystemExit(
            "Install requirements.txt to run the MQTT/TLS service"
        ) from exc

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=args.client_id,
        clean_session=False,
        protocol=mqtt.MQTTv311,
        reconnect_on_failure=True,
        manual_ack=True,
    )
    client.reconnect_delay_set(min_delay=1, max_delay=120)
    if args.username:
        client.username_pw_set(args.username, args.password)
    client.tls_set(
        ca_certs=args.ca_cert,
        certfile=args.client_cert,
        keyfile=args.client_key,
        tls_version=ssl.PROTOCOL_TLS_CLIENT,
    )
    client.tls_insecure_set(args.tls_insecure)
    retry_queue = MqttRetryQueue(
        database_path=args.retry_db,
        ingest_endpoint=args.ingest_endpoint,
        quarantine_endpoint=args.quarantine_endpoint,
        token=args.ingest_token,
        session_ready=False,
    )
    configure_mqtt_callbacks(client, args, retry_queue)
    retry_queue.start()
    try:
        client.connect_async(args.host, args.port, keepalive=60)
        LOGGER.info("mqtt_connecting host=%s port=%s tls=true", args.host, args.port)
        client.loop_forever(retry_first_connection=True)
    finally:
        retry_queue.stop()


if __name__ == "__main__":
    main()
