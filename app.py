from __future__ import annotations

import logging
import os

from motor_diagnosis.server import create_server


HOST = "127.0.0.1"
PORT = int(os.environ.get("PORT", "8787"))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    server = create_server(HOST, PORT)
    print(f"Bind Edge AI app is running at http://{HOST}:{PORT}")
    print("Press Ctrl+C to stop.")
    server.serve_forever()


if __name__ == "__main__":
    main()
