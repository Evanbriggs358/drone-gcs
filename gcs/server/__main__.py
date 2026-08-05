"""Run the viewer.

    python -m gcs.server

Binds to localhost only. The data directory can be overridden:

    set DRONE_DATA_DIR=C:\\DroneData\\flights
"""

from __future__ import annotations

import argparse
import webbrowser

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    from .app import DATA_ROOT

    url = f"http://{args.host}:{args.port}"
    print(f"Serving reconstructions from {DATA_ROOT}")
    print(f"  {url}")

    if not args.no_browser:
        webbrowser.open(url)

    uvicorn.run("gcs.server.app:app", host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
