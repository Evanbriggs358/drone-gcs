"""Fly a survey in SITL with the capture service running, end to end.

The closest thing to a real mapping flight that can be done without hardware:
the ground station plans a grid and uploads it, the flight controller flies it
and decides when to trigger the camera, and the companion service captures and
geotags a photo for every trigger.

    python tools/sitl_capture.py [--side 200] [--altitude 60] [--speedup 8]

Photos land in a timestamped folder under --output, alongside a captures.jsonl
manifest. Run tools/inspect_photos.py on the images afterwards to validate them.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from pymavlink import mavutil

from gcs.companion.camera import FakeCamera
from gcs.companion.capture import CameraFeedbackTrigger, CaptureService, CaptureSession
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


def set_param(link, name: str, value: float, timeout: float = 4.0) -> bool:
    """Set a parameter and confirm the echoed value. Silent failure otherwise."""
    link.mav.param_set_send(
        link.target_system, link.target_component, name.encode(),
        float(value), mavutil.mavlink.MAV_PARAM_TYPE_REAL32,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg = link.recv_match(type="PARAM_VALUE", blocking=True, timeout=1.0)
        if msg is not None and msg.param_id.strip("\x00") == name:
            return abs(msg.param_value - value) < max(0.01, abs(value) * 0.001)
    return False


def set_waypoint_speed(link, speed_ms: float) -> str:
    if set_param(link, "WP_SPD", speed_ms):
        return f"WP_SPD = {speed_ms:g} m/s"
    if set_param(link, "WPNAV_SPEED", speed_ms * 100.0):
        return f"WPNAV_SPEED = {speed_ms * 100:g} cm/s"
    return "FAILED"


def wait_command_ack(link, command: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg = link.recv_match(type=["COMMAND_ACK", "STATUSTEXT"], blocking=True, timeout=1.0)
        if msg is None:
            continue
        if msg.get_type() == "STATUSTEXT":
            text = msg.text.decode() if isinstance(msg.text, bytes) else msg.text
            print(f"      FC: {text}")
        elif msg.command == command:
            return msg.result == mavutil.mavlink.MAV_RESULT_ACCEPTED
    return False


def wait_until_ready(link, waypoint_count: int, timeout: float = 120.0) -> VehicleState:
    """Poll telemetry until the pre-flight checklist passes.

    Uses the same gate the ground station will use before a real flight, so the
    simulated run exercises it rather than bypassing it.
    """
    from gcs.link.preflight import run_preflight

    state = VehicleState()
    deadline = time.monotonic() + timeout
    last_report = 0.0

    print("\nWaiting for pre-flight checks ...")
    while time.monotonic() < deadline:
        msg = link.recv_match(blocking=True, timeout=1.0)
        if msg is None or msg.get_srcSystem() != link.target_system:
            continue
        apply_message(state, msg)

        report = run_preflight(
            state, mission_waypoint_count=waypoint_count, free_disk_gb=64.0
        )
        if report.ready_to_arm:
            print(f"  {report.summary()}")
            for check in report.checks:
                if check.passed:
                    print(f"    ok  {check.name}: {check.detail}")
            return state

        if time.monotonic() - last_report >= 5.0:
            last_report = time.monotonic()
            print(f"  {report.summary()}")

    raise SystemExit("Pre-flight checks did not pass within the timeout")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="tcp:127.0.0.1:5760")
    parser.add_argument("--side", type=float, default=200.0)
    parser.add_argument("--altitude", type=float, default=60.0)
    parser.add_argument("--speedup", type=float, default=8.0)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--output", default=r"C:\DroneData\sitl")
    args = parser.parse_args()

    params = SurveyParams(altitude_m=args.altitude, ground_speed_ms=8.0)
    plan = plan_survey(survey_square(SITL_HOME, args.side), CAM, params)
    items = build_mission(plan, home=SITL_HOME)

    print(
        f"Survey: {plan.area_acres:.1f} ac, {plan.line_count} lines, "
        f"{plan.photo_count} photos predicted, camera every {plan.photo_spacing_m:.1f} m"
    )

    link = mavutil.mavlink_connection(args.endpoint)
    if link.wait_heartbeat(timeout=20) is None:
        raise SystemExit("No heartbeat — is SITL running?")
    print(f"Connected to system {link.target_system}\n")

    print("Configuring:")
    print(f"  SIM_SPEEDUP     {'ok' if set_param(link, 'SIM_SPEEDUP', args.speedup) else 'FAILED'}")
    print(f"  AUTO_OPTIONS=2  {'ok' if set_param(link, 'AUTO_OPTIONS', 2) else 'FAILED'}")
    # A camera backend must exist or the autopilot never announces triggers.
    print(f"  CAM1_TYPE=1     {'ok' if set_param(link, 'CAM1_TYPE', 1) else 'FAILED'}")
    print(f"  SERVO10_FUNC=10 {'ok' if set_param(link, 'SERVO10_FUNCTION', 10) else 'FAILED'}")
    print(f"  {set_waypoint_speed(link, params.ground_speed_ms)}")

    print("\nUploading mission ...")
    upload_mission(link, items)
    problems = diff_missions(items, download_mission(link))
    if problems:
        raise SystemExit("Mission verification failed:\n  " + "\n  ".join(problems))
    print("Mission verified")

    link.mav.request_data_stream_send(
        link.target_system, link.target_component,
        mavutil.mavlink.MAV_DATA_STREAM_ALL, 5, 1,
    )

    session = CaptureSession(FakeCamera(), args.output)
    service = CaptureService(session, CameraFeedbackTrigger())
    print(f"Capturing into {session.directory}\n")

    # A freshly booted autopilot has no GPS lock and an unsettled EKF for the
    # first half-minute. Arming into that fails silently and the aircraft simply
    # sits there. Gate on the real pre-flight checklist instead of a sleep.
    state = wait_until_ready(link, len(items), timeout=120.0)

    print("\nArming in LOITER ...")
    link.set_mode_apm("LOITER")
    time.sleep(1.0)
    link.mav.command_long_send(
        link.target_system, link.target_component,
        mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 0, 1, 0, 0, 0, 0, 0, 0,
    )
    if not wait_command_ack(link, mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM):
        raise SystemExit("Arming was rejected — see the FC messages above")
    print("Armed")

    print("Switching to AUTO — the flight controller now owns the flight\n")
    link.set_mode_apm("AUTO")
    link.mav.command_long_send(
        link.target_system, link.target_component,
        mavutil.mavlink.MAV_CMD_MISSION_START, 0, 0, len(items) - 1, 0, 0, 0, 0, 0,
    )

    furthest = -1
    last_reported = -1
    took_off = False
    camera_messages = set()
    seen_messages = len(state.messages)
    deadline = time.monotonic() + args.timeout

    while time.monotonic() < deadline:
        msg = link.recv_match(blocking=True, timeout=1.0)
        if msg is None:
            continue
        if msg.get_srcSystem() != link.target_system:
            continue

        kind = msg.get_type()
        if kind in ("CAMERA_FEEDBACK", "CAMERA_IMAGE_CAPTURED", "CAMERA_TRIGGER"):
            camera_messages.add(kind)

        apply_message(state, msg)
        service.handle_message(msg)

        while seen_messages < len(state.messages):
            print(f"      FC: {state.messages[seen_messages]}")
            seen_messages += 1

        if (state.relative_alt_m or 0) > 5.0:
            took_off = True

        seq = state.mission_current_seq
        if seq is not None:
            furthest = max(furthest, seq)
            if seq != last_reported:
                last_reported = seq
                print(
                    f"  waypoint {seq:2d}/{len(items) - 1}  "
                    f"alt {state.relative_alt_m or 0:5.1f} m  "
                    f"photos {session.stats.captured:3d}"
                )

        if took_off and not state.armed:
            break

    print(f"\nFlight finished at waypoint {furthest} of {len(items) - 1}")
    print(f"Camera messages seen: {sorted(camera_messages) or 'NONE'}")
    print(
        f"Captured {session.stats.captured} photos "
        f"({session.stats.failed} failed) from {session.stats.triggers} triggers"
    )
    print(f"Predicted {plan.photo_count} photos from the plan")

    files = sorted(Path(session.images_dir).iterdir())
    print(f"Files on disk: {len(files)}")
    if files:
        print(f"  {session.images_dir}")


if __name__ == "__main__":
    main()
