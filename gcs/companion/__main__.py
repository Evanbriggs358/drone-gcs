"""Run the companion service.

On the Raspberry Pi this is started by systemd at boot — see
deploy/drone-companion.service. Run it directly for testing:

    python -m gcs.companion

Environment:
    DRONE_FC_ENDPOINT   serial device or MAVLink URL (default /dev/serial0)
    DRONE_FC_BAUD       serial baud rate (default 921600)
    DRONE_PHOTO_ROOT    where flights are stored (default /home/pi/flights)
    DRONE_FAKE_CAMERA   set to 1 to run without a real camera
"""

from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser()
    # Binds to all interfaces by default: the ground station connects over WiFi,
    # so localhost-only would make the service useless.
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8001)
    args = parser.parse_args()

    from .service import FC_ENDPOINT, PHOTO_ROOT, USE_FAKE_CAMERA

    print(f"Flight controller : {FC_ENDPOINT}")
    print(f"Photo storage     : {PHOTO_ROOT}")
    print(f"Camera            : {'FAKE (testing only)' if USE_FAKE_CAMERA else 'Pi Camera Module 3'}")
    print(f"Listening on      : http://{args.host}:{args.port}")

    uvicorn.run(
        "gcs.companion.service:app",
        host=args.host,
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
