from __future__ import annotations

import argparse
import json
import logging
import os
import ssl
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


LOGGER = logging.getLogger("motor_diagnosis.mqtt")
RETRYABLE_HTTP_STATUSES = {408, 425, 429}


@dataclass
class MqttBridgeError(Exception):
    status: int
    code: str
    message: str


def decode_mqtt_payload(topic: str, message: bytes | str) -> dict[str, Any]:
    parts = [part for part in topic.strip("/").split("/") if part]
    if len(parts) != 3 or parts[0] != "devices" or parts[2] != "telemetry":
        raise MqttBridgeError(
            400,
            "INVALID_MQTT_TOPIC",
            "MQTT topic must be devices/{deviceId}/telemetry.",
        )
    try:
        payload = json.loads(
            message.decode("utf-8") if isinstance(message, bytes) else message
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MqttBridgeError(
            400, "INVALID_JSON", "MQTT payload is not valid JSON."
        ) from exc
    if not isinstance(payload, dict):
        raise MqttBridgeError(
            400, "INVALID_JSON_BODY", "MQTT payload must be a JSON object."
        )
    topic_device_id = parts[1].strip().upper()
    payload_device_id = str(payload.get("deviceId") or "").strip().upper()
    if topic_device_id != payload_device_id:
        raise MqttBridgeError(
            409,
            "DEVICE_MAPPING_MISMATCH",
            "MQTT topic deviceId and payload deviceId must match.",
        )
    return payload


def post_json(
    endpoint: str,
    token: str,
    payload: dict[str, Any],
    *,
    timeout: float,
) -> tuple[dict[str, Any], int]:
    request = Request(
        endpoint,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
            return body, response.status
    except HTTPError as exc:
        try:
            error_body = json.loads(exc.read().decode("utf-8"))
            error = error_body.get("error", {})
            code = str(error.get("code") or "INGEST_REJECTED")
            message_text = str(error.get("message") or exc.reason)
        except (UnicodeDecodeError, json.JSONDecodeError):
            code = "INGEST_REJECTED"
            message_text = str(exc.reason)
        raise MqttBridgeError(exc.code, code, message_text) from exc
    except URLError as exc:
        raise MqttBridgeError(
            503,
            "HTTP_SERVICE_UNAVAILABLE",
            f"Backend API is unavailable: {exc.reason}",
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


def is_permanent_ingest_error(error: MqttBridgeError) -> bool:
    return (
        400 <= error.status < 500
        and error.status not in RETRYABLE_HTTP_STATUSES
    )


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


def process_mqtt_message(
    client: Any,
    message: Any,
    *,
    endpoint: str,
    token: str,
) -> str:
    """Return accepted, quarantined, or retry based on HTTP processing outcome."""
    try:
        response, status = forward_mqtt_message(
            message.topic,
            message.payload,
            endpoint=endpoint,
            token=token,
        )
    except MqttBridgeError as error:
        if is_permanent_ingest_error(error):
            acknowledged = acknowledge_message(client, message)
            LOGGER.warning(
                "mqtt_ingest_quarantined topic=%s status=%s code=%s message=%s",
                message.topic,
                error.status,
                error.code,
                error.message,
            )
            return "quarantined" if acknowledged else "retry"
        LOGGER.warning(
            "mqtt_ingest_retry topic=%s status=%s code=%s message=%s",
            message.topic,
            error.status,
            error.code,
            error.message,
        )
        return "retry"

    if not acknowledge_message(client, message):
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
    parser.add_argument("--qos", type=int, choices=(1, 2), default=1)
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
        "--tls-insecure",
        action="store_true",
        help="Disable broker hostname verification for local testing only.",
    )
    return parser.parse_args()


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

    def on_connect(client, _userdata, _flags, reason_code, _properties) -> None:
        if reason_code != 0:
            LOGGER.error("mqtt_connect_failed reason=%s", reason_code)
            report_mqtt_status_safely(
                endpoint=args.health_endpoint,
                token=args.ingest_token,
                status="degraded",
                detail=f"Broker connection refused: {reason_code}",
                error_code="MQTT_CONNECT_REFUSED",
            )
            return
        result, _mid = client.subscribe(args.topic, qos=args.qos)
        if int(result) != 0:
            LOGGER.error("mqtt_subscribe_failed topic=%s result=%s", args.topic, result)
            report_mqtt_status_safely(
                endpoint=args.health_endpoint,
                token=args.ingest_token,
                status="degraded",
                detail=f"Broker subscription failed: {result}",
                error_code="MQTT_SUBSCRIBE_FAILED",
            )
            return
        report_mqtt_status_safely(
            endpoint=args.health_endpoint,
            token=args.ingest_token,
            status="healthy",
            detail=f"Subscribed to {args.topic} with QoS {args.qos}",
        )
        LOGGER.info("mqtt_subscribed topic=%s qos=%s", args.topic, args.qos)

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
            token=args.ingest_token,
        )

    client.on_connect = on_connect
    client.on_connect_fail = on_connect_fail
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.connect_async(args.host, args.port, keepalive=60)
    LOGGER.info("mqtt_connecting host=%s port=%s tls=true", args.host, args.port)
    client.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    main()
