"""Hold SITL's SERIAL0 open so the simulator stays up.

ArduPilot SITL blocks at "Waiting for connection ...." until something attaches
to SERIAL0 (TCP 5760), and **exits when that client disconnects**. Normally a
ground station occupies that port. Running headless, nothing does, so the
simulator dies the moment a script finishes.

This holds SERIAL0 and discards traffic, leaving SERIAL1 (5762) and SERIAL2
(5763) free for the tools. Start it once, leave it running:

    python tools/sitl_keepalive.py

Then point everything else at tcp:127.0.0.1:5762.
"""

from __future__ import annotations

import argparse
import time

from pymavlink import mavutil


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="tcp:127.0.0.1:5760")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    print(f"Attaching to {args.endpoint} to keep SITL alive ...")
    link = mavutil.mavlink_connection(args.endpoint)
    if link.wait_heartbeat(timeout=30) is None:
        raise SystemExit("No heartbeat — is SITL running?")

    print(f"Holding SERIAL0. System {link.target_system}. Ctrl-C to release.")
    last = time.monotonic()

    while True:
        link.recv_match(blocking=True, timeout=1.0)
        if not args.quiet and time.monotonic() - last >= 30:
            last = time.monotonic()
            print(f"  still holding ({time.strftime('%H:%M:%S')})")


if __name__ == "__main__":
    main()
