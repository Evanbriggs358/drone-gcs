"""Connect to a running SITL instance and print live vehicle state.

Sprint 0's exit criterion, and the first end-to-end check of the link layer
against a real ArduPilot instance rather than synthetic messages.

Mission Planner's built-in simulator launches ArduCopter.exe, which listens on
TCP 5760 (taken by Mission Planner itself) plus 5762 and 5763, which are free for
another ground station.

    python tools/sitl_probe.py [--endpoint tcp:127.0.0.1:5762] [--seconds 10]
"""

from __future__ import annotations

import argparse
import time

from pymavlink import mavutil

from gcs.link.preflight import run_preflight
from gcs.link.state import VehicleState, apply_message

DEFAULT_ENDPOINT = "tcp:127.0.0.1:5762"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--seconds", type=float, default=10.0)
    args = parser.parse_args()

    print(f"Connecting to {args.endpoint} ...")
    link = mavutil.mavlink_connection(args.endpoint)

    heartbeat = link.wait_heartbeat(timeout=15)
    if heartbeat is None:
        raise SystemExit("No heartbeat within 15 s — is SITL running?")
    print(f"Heartbeat from system {link.target_system}, component {link.target_component}")

    # Ask for a useful stream rate; SITL defaults are sparse on some links.
    link.mav.request_data_stream_send(
        link.target_system, link.target_component,
        mavutil.mavlink.MAV_DATA_STREAM_ALL, 4, 1,
    )

    state = VehicleState()
    deadline = time.monotonic() + args.seconds
    last_print = 0.0
    seen: set[str] = set()

    while time.monotonic() < deadline:
        msg = link.recv_match(blocking=True, timeout=1.0)
        if msg is None:
            continue
        seen.add(msg.get_type())
        apply_message(state, msg)

        now = time.monotonic()
        if now - last_print >= 1.0:
            last_print = now
            print(_format(state))

    print(f"\nMessage types seen ({len(seen)}): {', '.join(sorted(seen))}")

    report = run_preflight(state, mission_waypoint_count=0, free_disk_gb=64.0)
    print(f"\nPre-flight: {report.summary()}")
    for check in report.checks:
        mark = "ok  " if check.passed else ("BLOCK" if check.blocks_launch else "warn ")
        print(f"  [{mark}] {check.name}: {check.detail}")


def _format(s: VehicleState) -> str:
    pos = "no position" if s.lat is None else f"{s.lat:10.6f},{s.lon:11.6f}"
    alt = "  --" if s.relative_alt_m is None else f"{s.relative_alt_m:5.1f}m"
    hdg = " --" if s.heading_deg is None else f"{s.heading_deg:5.1f}"
    volts = " --" if s.battery_v is None else f"{s.battery_v:5.2f}V"
    return (
        f"{s.mode:<10} {'ARMED' if s.armed else 'disarmed':<9} {pos} {alt}"
        f" hdg {hdg} {volts} {s.gps_fix.name} sats {s.satellites:2d}"
    )


if __name__ == "__main__":
    main()
