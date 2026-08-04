"""How much ground fits in one battery?

Binary-searches the largest square survey area that fits inside a flight-time
budget, for a range of altitude and pattern settings.

    python tools/battery_budget.py

The endurance figure is an assumption until measured. Fly a timed hover with the
actual all-up weight and replace ``USABLE_FLIGHT_MIN`` with the real number.
"""

from __future__ import annotations

from gcs.planning import PI_CAMERA_MODULE_3 as CAM
from gcs.planning import Pattern, SurveyParams, plan_survey
from gcs.planning.geo import LocalFrame

#: Minutes of survey flying available per battery, after reserve.
#: A 5000 mAh 6S pack on a 7" long-range build gives roughly 18-25 min total.
#: Landing with less than 20% remaining is how packs get ruined, so the usable
#: figure is well below the headline number.
USABLE_FLIGHT_MIN = 15.0

#: Climb to altitude, transit to the survey area, return, descend, land.
OVERHEAD_MIN = 3.0


def square_polygon(frame: LocalFrame, side_m: float) -> list[tuple[float, float]]:
    h = side_m / 2.0
    return [frame.to_latlon(p) for p in ((-h, -h), (h, -h), (h, h), (-h, h))]


def largest_square_side(frame: LocalFrame, params: SurveyParams, budget_min: float) -> float:
    """Largest square side length, in metres, flyable within the budget."""
    low, high = 10.0, 3000.0

    if plan_survey(square_polygon(frame, low), CAM, params).duration_min > budget_min:
        return 0.0

    for _ in range(40):
        mid = (low + high) / 2.0
        duration = plan_survey(square_polygon(frame, mid), CAM, params).duration_min
        if duration <= budget_min:
            low = mid
        else:
            high = mid
    return low


def main() -> None:
    frame = LocalFrame(47.6062, -122.3321)
    budget = USABLE_FLIGHT_MIN - OVERHEAD_MIN

    print(f"Battery budget: {USABLE_FLIGHT_MIN:.0f} min usable "
          f"- {OVERHEAD_MIN:.0f} min overhead = {budget:.0f} min surveying\n")

    scenarios = [
        ("Nadir      @120 m", SurveyParams(altitude_m=120.0, ground_speed_ms=10.0)),
        ("Nadir      @100 m", SurveyParams(altitude_m=100.0, ground_speed_ms=8.0)),
        ("Nadir       @60 m", SurveyParams(altitude_m=60.0, ground_speed_ms=6.0)),
        (
            "Crosshatch @120 m",
            SurveyParams(altitude_m=120.0, ground_speed_ms=10.0, pattern=Pattern.CROSSHATCH),
        ),
        (
            "Crosshatch @100 m",
            SurveyParams(altitude_m=100.0, ground_speed_ms=8.0, pattern=Pattern.CROSSHATCH),
        ),
        (
            "Crosshatch  @60 m",
            SurveyParams(altitude_m=60.0, ground_speed_ms=6.0, pattern=Pattern.CROSSHATCH),
        ),
    ]

    print(f"{'Setting':<18} {'GSD':>9}  {'Max area':>10}  {'Square':>10}  {'Photos':>7}")
    print("-" * 62)
    for label, params in scenarios:
        side = largest_square_side(frame, params, budget)
        if side <= 0:
            print(f"{label:<18} {'-':>9}  {'too small':>10}")
            continue
        plan = plan_survey(square_polygon(frame, side), CAM, params)
        print(
            f"{label:<18} {plan.gsd_cm_per_px:6.2f} cm  "
            f"{plan.area_acres:7.1f} ac  {side:7.0f} m  {plan.photo_count:7d}"
        )


if __name__ == "__main__":
    main()
