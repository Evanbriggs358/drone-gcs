"""How much ground fits in one battery, using the real endurance model.

Binary-searches the largest square survey area that fits inside one pack, for a
range of altitude and pattern settings.

    python tools/battery_budget.py
    python tools/battery_budget.py --hover-min 18

Endurance now varies with speed rather than being a flat assumption, so the
answer accounts for the fact that a multirotor cruises more efficiently than it
hovers. ``--hover-min`` calibrates against a measured hover, which is what turns
these from estimates into planning figures.
"""

from __future__ import annotations

import argparse
from dataclasses import replace

from gcs.planning import PI_CAMERA_MODULE_3 as CAM
from gcs.planning import Pattern, SurveyParams, plan_survey
from gcs.planning.geo import LocalFrame
from gcs.planning.power import CNHL_5000_6S, DEADCAT_7IN

#: Climb to altitude, transit to the survey area, return, descend, land.
OVERHEAD_MIN = 3.0


def square_polygon(frame: LocalFrame, side_m: float) -> list[tuple[float, float]]:
    h = side_m / 2.0
    return [frame.to_latlon(p) for p in ((-h, -h), (h, -h), (h, h), (-h, h))]


def largest_square_side(frame, params, airframe, battery, budget_min: float) -> float:
    """Largest square side length, in metres, flyable within the budget."""
    low, high = 10.0, 3000.0

    def duration(side: float) -> float:
        return plan_survey(square_polygon(frame, side), CAM, params).duration_min

    if duration(low) > budget_min:
        return 0.0

    for _ in range(40):
        mid = (low + high) / 2.0
        if duration(mid) <= budget_min:
            low = mid
        else:
            high = mid
    return low


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--weight", type=float, help="all-up weight in kg")
    parser.add_argument("--hover-min", type=float, help="measured hover time, minutes")
    args = parser.parse_args()

    airframe = DEADCAT_7IN
    if args.weight:
        airframe = replace(airframe, all_up_weight_kg=args.weight)

    battery = CNHL_5000_6S
    if args.hover_min:
        airframe = airframe.calibrated_to(args.hover_min, battery)
        print(f"Calibrated to a measured {args.hover_min:g} min hover.\n")
    else:
        print("UNCALIBRATED — fly a timed hover and re-run with --hover-min.\n")

    frame = LocalFrame(47.6062, -122.3321)

    print(f"Aircraft {airframe.all_up_weight_kg:.2f} kg, battery "
          f"{battery.capacity_mah:.0f} mAh {battery.cells}S "
          f"({battery.usable_energy_wh:.0f} Wh usable)")
    print(f"Hover endurance {airframe.endurance_min(battery):.1f} min, "
          f"best survey speed {airframe.best_range_speed_ms():.1f} m/s\n")

    scenarios = [
        ("Nadir      @120 m", 120.0, Pattern.NADIR),
        ("Nadir      @100 m", 100.0, Pattern.NADIR),
        ("Nadir       @60 m", 60.0, Pattern.NADIR),
        ("Crosshatch @120 m", 120.0, Pattern.CROSSHATCH),
        ("Crosshatch @100 m", 100.0, Pattern.CROSSHATCH),
        ("Crosshatch  @60 m", 60.0, Pattern.CROSSHATCH),
    ]

    print(f"{'Setting':<18} {'speed':>7} {'endurance':>10} {'GSD':>9} "
          f"{'max area':>10} {'photos':>7}")
    print("-" * 68)

    for label, altitude, pattern in scenarios:
        # Fly each survey at the speed that covers the most ground per battery,
        # capped by what the shutter can keep up with at that altitude.
        speed = min(
            airframe.best_range_speed_ms(),
            CAM.max_ground_speed_ms(altitude, 0.70),
        )
        params = SurveyParams(altitude_m=altitude, ground_speed_ms=speed, pattern=pattern)

        endurance = airframe.endurance_min(battery, speed)
        budget = endurance - OVERHEAD_MIN
        side = largest_square_side(frame, params, airframe, battery, budget)

        if side <= 0:
            print(f"{label:<18} {speed:>5.1f} m/s {endurance:>8.1f} min  too small")
            continue

        plan = plan_survey(
            square_polygon(frame, side), CAM, params, airframe=airframe, battery=battery
        )
        print(
            f"{label:<18} {speed:>5.1f} m/s {endurance:>8.1f} min "
            f"{plan.gsd_cm_per_px:>6.2f} cm {plan.area_acres:>7.1f} ac "
            f"{plan.photo_count:>7d}"
        )


if __name__ == "__main__":
    main()
