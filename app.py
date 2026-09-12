from __future__ import annotations

import logging
import os
import signal
import threading

from motor_diagnosis.server import create_server
from motor_diagnosis.alerts import configured_adapters

HOST = "127.0.0.1"
PORT = int(os.environ.get("PORT", "8787"))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    server = create_server(
        HOST,
        PORT,
        alert_database=os.environ.get("ALERT_DB_PATH", "output/alerts.sqlite3"),
        alert_adapters=configured_adapters(),
        state_database=os.environ.get("STATE_DB_PATH", "output/runtime.sqlite3"),
        analysis_database=os.environ.get("ANALYSIS_DB_PATH", "output/analysis.sqlite3"),
        model_artifact=os.environ.get("SHADOW_MODEL_ARTIFACT"),
        window_database=os.environ.get("VIBRATION_WINDOW_DB_PATH", "output/vibration-windows.sqlite3"),
        raw_window_database=os.environ.get("RAW_VIBRATION_WINDOW_DB_PATH", "output/raw-vibration-windows.sqlite3"),
        snapshot_database=os.environ.get("PERIODIC_SNAPSHOT_DB_PATH", "output/periodic-snapshots.sqlite3"),
        snapshot_event_mode=os.environ.get("SNAPSHOT_EVENT_MODE", "events"),
        window_model_variant=os.environ.get("WINDOW_MODEL_VARIANT") or None,
        model_candidate=os.environ.get("SHADOW_MODEL_CANDIDATE", "lstm_autoencoder"),
        model_checksum=os.environ.get("SHADOW_MODEL_CHECKSUM"),
        model_preprocessing_profile=os.environ.get(
            "SHADOW_MODEL_PREPROCESSING_PROFILE"
        ),
        model_database=os.environ.get(
            "SHADOW_MODEL_DB_PATH", "output/model-inference.sqlite3"
        ),
        communication_database=os.environ.get(
            "COMMUNICATION_QUALITY_DB_PATH", "output/communication-quality.sqlite3"
        ),
    )
    print(f"Bind Edge AI app is running at http://{HOST}:{PORT}")
    print("Press Ctrl+C to stop.")
    # systemd sends SIGTERM by default. shutdown() must run off the serving thread.
    stopping = threading.Event()

    def stop_service(signum, frame):
        if not stopping.is_set():
            stopping.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    previous_term = signal.signal(signal.SIGTERM, stop_service)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            server.server_close()
        finally:
            signal.signal(signal.SIGTERM, previous_term)


if __name__ == "__main__":
    main()
