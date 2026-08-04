"""Print survey plans for a square test area at various settings.

A quick way to sanity-check the planner's numbers without a UI:

    python tools/preview_plan.py
"""

from __future__ import annotations

from gcs.planning import PI_CAMERA_MODULE_3 as CAM
from gcs.planning import Pattern, SurveyParams, plan_survey
from gcs.planning.geo import LocalFrame


def square(frame: LocalFrame, side_m: float) -> list[tuple[float, float]]:
    h = side_m / 2.0
    return [frame.to_latlon(p) for p in ((-h, -h), (h, -h), (h, h), (-h, h))]


def main() -> None:
    frame = LocalFrame(47.6062, -122.3321)
    polygon = square(frame, 400.0)

    scenarios = [
        ("Nadir  @100 m, 8 m/s", SurveyParams(altitude_m=100.0, ground_speed_ms=8.0)),
        (
            "Cross  @100 m, 8 m/s",
            SurveyParams(
                altitude_m=100.0, ground_speed_ms=8.0, pattern=Pattern.CROSSHATCH
            ),
        ),
        (
            "Cross  @60 m,  6 m/s",
            SurveyParams(
                altitude_m=60.0, ground_speed_ms=6.0, pattern=Pattern.CROSSHATCH
            ),
        ),
        (
            "Cross  @40 m, 20 m/s",
            SurveyParams(
                altitude_m=40.0, ground_speed_ms=20.0, pattern=Pattern.CROSSHATCH
            ),
        ),
    ]

    for label, params in scenarios:
        plan = plan_survey(polygon, CAM, params)
        print(
            f"{label} | {plan.area_acres:5.1f} ac"
            f" | GSD {plan.gsd_cm_per_px:5.2f} cm/px"
            f" | {plan.line_count:3d} lines"
            f" | {plan.photo_count:4d} photos"
            f" | {plan.path_length_m / 1000:4.1f} km"
            f" | {plan.duration_min:5.1f} min"
        )
        for warning in plan.warnings:
            print(f"    ! {warning}")
        print()


if __name__ == "__main__":
    main()
