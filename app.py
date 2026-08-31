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
