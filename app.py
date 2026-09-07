from __future__ import annotations

import logging
import os

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
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
