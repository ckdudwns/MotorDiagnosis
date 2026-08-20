from __future__ import annotations

import argparse
import logging
import os
import ssl

from .data import ApiError, ingest_mqtt_message


LOGGER = logging.getLogger("motor_diagnosis.mqtt")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Subscribe to MQTT/TLS telemetry and use the shared ingest validator."
    )
    parser.add_argument("--host", default=os.environ.get("MQTT_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("MQTT_PORT", "8883"))
    )
    parser.add_argument("--topic", default="devices/+/telemetry")
    parser.add_argument("--qos", type=int, choices=(0, 1, 2), default=1)
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
        "--tls-insecure",
        action="store_true",
        help="Disable broker hostname verification for local testing only.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if bool(args.client_cert) != bool(args.client_key):
        raise SystemExit("--client-cert and --client-key must be provided together")

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
    )
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
            return
        client.subscribe(args.topic, qos=args.qos)
        LOGGER.info("mqtt_subscribed topic=%s qos=%s", args.topic, args.qos)

    def on_message(_client, _userdata, message) -> None:
        try:
            response, status = ingest_mqtt_message(
                message.topic, message.payload, token=args.ingest_token
            )
            LOGGER.info(
                "mqtt_ingest status=%s device=%s sequence=%s duplicate=%s",
                status,
                response["deviceId"],
                response["sequence"],
                response["duplicate"],
            )
        except ApiError as error:
            LOGGER.warning(
                "mqtt_ingest_rejected topic=%s code=%s message=%s",
                message.topic,
                error.code,
                error.message,
            )

    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(args.host, args.port, keepalive=60)
    LOGGER.info("mqtt_connecting host=%s port=%s tls=true", args.host, args.port)
    client.loop_forever()


if __name__ == "__main__":
    main()
