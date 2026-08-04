"""Plan a survey, upload it to SITL, and verify the autopilot stored it.

End-to-end proof of the planning and link layers together:

    python tools/sitl_upload.py

Uses ArduPilot's default SITL home near Canberra so the generated grid lands
where the simulated vehicle actually is.
"""

from __future__ import annotations

import argparse

from pymavlink import mavutil

from gcs.link.mission import build_mission, download_mission, diff_missions, upload_mission
from gcs.planning import PI_CAMERA_MODULE_3 as CAM
from gcs.planning import Pattern, SurveyParams, plan_survey
from gcs.planning.geo import LocalFrame

#: ArduPilot's default SITL start location (CMAC, Canberra).
SITL_HOME = (-35.363262, 149.165237)


def survey_square(centre: tuple[float, float], side_m: float) -> list[tuple[float, float]]:
    frame = LocalFrame(*centre)
    h = side_m / 2.0
    return [frame.to_latlon(p) for p in ((-h, -h), (h, -h), (h, h), (-h, h))]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="tcp:127.0.0.1:5762")
    parser.add_argument("--side", type=float, default=250.0, help="survey square side, metres")
    parser.add_argument("--altitude", type=float, default=80.0)
    parser.add_argument("--crosshatch", action="store_true")
    args = parser.parse_args()

    params = SurveyParams(
        altitude_m=args.altitude,
        ground_speed_ms=8.0,
        pattern=Pattern.CROSSHATCH if args.crosshatch else Pattern.NADIR,
    )
    plan = plan_survey(survey_square(SITL_HOME, args.side), CAM, params)

    print(
        f"Plan: {plan.area_acres:.1f} ac, GSD {plan.gsd_cm_per_px:.2f} cm/px, "
        f"{plan.line_count} lines, {plan.photo_count} photos, "
        f"{plan.duration_min:.1f} min"
    )
    print(f"Trigger every {plan.photo_spacing_m:.1f} m, lines {plan.line_spacing_m:.1f} m apart")

    items = build_mission(plan, home=SITL_HOME)
    print(f"\nMission: {len(items)} items")
    for item in items[:4]:
        print("  " + item.describe())
    print(f"  ... {len(items) - 6} waypoints ...")
    for item in items[-2:]:
        print("  " + item.describe())

    print(f"\nConnecting to {args.endpoint} ...")
    link = mavutil.mavlink_connection(args.endpoint)
    if link.wait_heartbeat(timeout=15) is None:
        raise SystemExit("No heartbeat — is SITL running?")
    print(f"Connected to system {link.target_system}")

    print("Uploading ...")
    upload_mission(link, items)

    print("Reading back ...")
    stored = download_mission(link)
    print(f"Autopilot reports {len(stored)} items stored")

    problems = diff_missions(items, stored)
    if problems:
        print("\nVERIFICATION FAILED:")
        for p in problems:
            print(f"  {p}")
        raise SystemExit(1)

    print("\nVerified: every item read back matches what was uploaded.")


if __name__ == "__main__":
    main()
