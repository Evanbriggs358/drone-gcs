"""Fly an uploaded survey mission in SITL, start to finish.

Closes the loop on the flight path: plan, upload, verify, arm, run the mission in
AUTO, and watch it complete. Nothing here commands the aircraft's position — the
flight controller executes the stored mission on its own, which is the whole point
of the AUTO-mission design (SPEC §2.1).

    python tools/sitl_fly.py [--side 200] [--speedup 10]

Uses SIM_SPEEDUP so a six-minute survey completes in well under a minute.
"""

from __future__ import annotations

import argparse
import time

from pymavlink import mavutil

from gcs.link.mission import build_mission, diff_missions, download_mission, upload_mission
from gcs.link.state import VehicleState, apply_message
from gcs.planning import PI_CAMERA_MODULE_3 as CAM
from gcs.planning import SurveyParams, plan_survey
from gcs.planning.geo import LocalFrame

SITL_HOME = (-35.363262, 149.165237)


def survey_square(centre, side_m: float):
    frame = LocalFrame(*centre)
    h = side_m / 2.0
    return [frame.to_latlon(p) for p in ((-h, -h), (h, -h), (h, h), (-h, h))]


def set_param(link, name: str, value: float, timeout: float = 3.0) -> bool:
    """Set a parameter and confirm the autopilot echoed the new value.

    MAVLink parameter writes fail **silently** when the name does not exist —
    no error, no ack. Unverified writes are how a mission ends up flying with
    settings you believe you changed. Returns False if unconfirmed.
    """
    link.mav.param_set_send(
        link.target_system, link.target_component, name.encode(),
        value, mavutil.mavlink.MAV_PARAM_TYPE_REAL32,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg = link.recv_match(type="PARAM_VALUE", blocking=True, timeout=1.0)
        if msg is not None and msg.param_id.strip("\x00") == name:
            return abs(msg.param_value - value) < max(0.01, abs(value) * 0.001)
    return False


def set_waypoint_speed(link, speed_ms: float) -> str:
    """Set waypoint cruise speed, coping with ArduPilot's parameter rename.

    Recent firmware uses ``WP_SPD`` in metres per second; older builds use
    ``WPNAV_SPEED`` in centimetres per second. Which one exists depends on the
    firmware flashed to the aircraft, so try the modern name and fall back.
    """
    if set_param(link, "WP_SPD", speed_ms):
        return f"WP_SPD = {speed_ms:g} m/s"
    if set_param(link, "WPNAV_SPEED", speed_ms * 100.0):
        return f"WPNAV_SPEED = {speed_ms * 100:g} cm/s"
    return "FAILED — neither WP_SPD nor WPNAV_SPEED accepted"


def wait_command_ack(link, command: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg = link.recv_match(type="COMMAND_ACK", blocking=True, timeout=1.0)
        if msg is not None and msg.command == command:
            return msg.result == mavutil.mavlink.MAV_RESULT_ACCEPTED
    return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="tcp:127.0.0.1:5762")
    parser.add_argument("--side", type=float, default=200.0)
    parser.add_argument("--altitude", type=float, default=60.0)
    parser.add_argument("--speedup", type=float, default=10.0)
    parser.add_argument("--timeout", type=float, default=300.0)
    args = parser.parse_args()

    params = SurveyParams(altitude_m=args.altitude, ground_speed_ms=8.0)
    plan = plan_survey(survey_square(SITL_HOME, args.side), CAM, params)
    items = build_mission(plan, home=SITL_HOME)

    print(
        f"Survey: {plan.area_acres:.1f} ac, {plan.line_count} lines, "
        f"{plan.photo_count} photos, {plan.duration_min:.1f} min of flying"
    )
    print(f"Mission: {len(items)} items, camera every {plan.photo_spacing_m:.1f} m\n")

    link = mavutil.mavlink_connection(args.endpoint)
    if link.wait_heartbeat(timeout=15) is None:
        raise SystemExit("No heartbeat — is SITL running?")
    print(f"Connected to system {link.target_system}")

    print(f"Setting SIM_SPEEDUP to {args.speedup:g}")
    set_param(link, "SIM_SPEEDUP", args.speedup)

    # AUTO_OPTIONS bit 1 = "allow takeoff without raising throttle", so entering
    # AUTO starts the mission's TAKEOFF command. Without it Copter sits armed on
    # the ground waiting for throttle input that an autonomous launch never gives.
    #
    # Bit 0 ("allow arming in AUTO") is deliberately NOT set: arming stays an
    # explicit action in a normal flight mode. This applies to the real aircraft
    # too — it is a required setup parameter, not a simulator workaround.
    ok = set_param(link, "AUTO_OPTIONS", 2)
    print(f"AUTO_OPTIONS = 2 (takeoff without throttle){'' if ok else '  [UNCONFIRMED]'}")

    # Waypoint speed is a flight-controller parameter, not part of the mission.
    # Without setting it the aircraft flies at its configured default and every
    # duration estimate the planner produced is wrong.
    print(f"Waypoint speed: {set_waypoint_speed(link, params.ground_speed_ms)}")

    print("Uploading mission ...")
    upload_mission(link, items)
    problems = diff_missions(items, download_mission(link))
    if problems:
        raise SystemExit("Mission verification failed:\n  " + "\n  ".join(problems))
    print("Mission verified\n")

    link.mav.request_data_stream_send(
        link.target_system, link.target_component,
        mavutil.mavlink.MAV_DATA_STREAM_ALL, 4, 1,
    )

    # ArduCopter refuses to arm in AUTO ("Auto mode not armable") — a deliberate
    # safety behaviour. Arm in LOITER, then hand control to the mission. This is
    # the sequence a real operator flies, so the simulation matches reality
    # rather than papering over it with AUTO_OPTIONS.
    print("Switching to LOITER to arm ...")
    link.set_mode_apm("LOITER")
    time.sleep(1.0)

    print("Arming ...")
    link.mav.command_long_send(
        link.target_system, link.target_component,
        mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 0,
        1, 0, 0, 0, 0, 0, 0,
    )
    if not wait_command_ack(link, mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM):
        raise SystemExit("Arming rejected — check the STATUSTEXT stream for the reason")

    print("Switching to AUTO ...")
    link.set_mode_apm("AUTO")
    time.sleep(1.0)

    print("Starting mission ...\n")
    link.mav.command_long_send(
        link.target_system, link.target_component,
        mavutil.mavlink.MAV_CMD_MISSION_START, 0,
        0, len(items) - 1, 0, 0, 0, 0, 0,
    )

    state = VehicleState()
    last_seq = -1
    furthest_seq = -1
    max_alt = 0.0
    max_speed = 0.0
    took_off = False
    seen_messages = 0
    final_seq = len(items) - 1
    deadline = time.monotonic() + args.timeout

    while time.monotonic() < deadline:
        msg = link.recv_match(blocking=True, timeout=1.0)
        if msg is None:
            continue
        # Other ground stations share this link; only the vehicle's traffic counts.
        if msg.get_srcSystem() != link.target_system:
            continue
        apply_message(state, msg)

        # Surface the autopilot's own reports — they explain aborts far faster
        # than inferring cause from telemetry.
        while seen_messages < len(state.messages):
            print(f"      FC: {state.messages[seen_messages]}")
            seen_messages += 1

        if state.relative_alt_m is not None:
            max_alt = max(max_alt, state.relative_alt_m)
            if state.relative_alt_m > 5.0:
                took_off = True

        # Sample peak speed continuously. Reading speed only at waypoint changes
        # measures the aircraft mid-turn, not its cruise speed.
        if state.ground_speed_ms is not None:
            max_speed = max(max_speed, state.ground_speed_ms)

        seq = state.mission_current_seq
        if seq is not None and seq != last_seq:
            last_seq = seq
            # ArduPilot resets MISSION_CURRENT to 0 once the mission ends, so
            # progress must be tracked by the furthest item reached.
            furthest_seq = max(furthest_seq, seq)
            alt = state.relative_alt_m or 0.0
            speed = state.ground_speed_ms or 0.0
            volts = f"{state.battery_v:5.2f}V" if state.battery_v else "   --"
            print(
                f"  waypoint {seq:2d}/{final_seq}  alt {alt:5.1f} m"
                f"  {speed:4.1f} m/s  {volts}  {state.mode}"
            )

        if took_off and not state.armed:
            print(
                f"\nDisarmed. Peak altitude {max_alt:.1f} m, peak speed "
                f"{max_speed:.1f} m/s, furthest waypoint {furthest_seq} of {final_seq}."
            )
            if furthest_seq >= final_seq:
                print("Mission complete: survey flown, camera triggering ended, returned and landed.")
                return
            raise SystemExit(
                f"Mission did NOT complete — stopped at waypoint {furthest_seq} of {final_seq}."
            )

    print(f"\nTimed out after {args.timeout:.0f} s at waypoint {furthest_seq}, alt {max_alt:.1f} m.")
    raise SystemExit(1)


if __name__ == "__main__":
    main()
