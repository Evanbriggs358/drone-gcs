"""Show how endurance and range vary with speed for the 7-inch platform.

    python tools/power_curve.py
    python tools/power_curve.py --hover-min 18 --weight 1.75

``--hover-min`` calibrates the model against a real timed hover, which is the
difference between a physics-flavoured guess and a number worth planning around.
"""

from __future__ import annotations

import argparse
from dataclasses import replace

from gcs.planning import PI_CAMERA_MODULE_3 as CAM
from gcs.planning.power import CNHL_5000_6S, DEADCAT_7IN, Battery


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--weight", type=float, help="all-up weight in kg")
    parser.add_argument("--capacity", type=float, help="battery capacity in mAh")
    parser.add_argument("--cells", type=int, help="battery cell count")
    parser.add_argument("--hover-min", type=float, help="measured hover time, minutes")
    parser.add_argument("--altitude", type=float, default=100.0, help="survey altitude")
    parser.add_argument("--overlap", type=float, default=0.70)
    args = parser.parse_args()

    airframe = DEADCAT_7IN
    if args.weight:
        airframe = replace(airframe, all_up_weight_kg=args.weight)

    battery = CNHL_5000_6S
    if args.capacity or args.cells:
        battery = Battery(
            capacity_mah=args.capacity or battery.capacity_mah,
            cells=args.cells or battery.cells,
        )

    if args.hover_min:
        airframe = airframe.calibrated_to(args.hover_min, battery)
        print(f"Calibrated to a measured {args.hover_min:g} min hover "
              f"(scale {airframe.calibration:.2f})\n")
    else:
        print("UNCALIBRATED — estimates from component figures. Fly a timed hover\n"
              "and re-run with --hover-min to replace guesswork with measurement.\n")

    print(f"Aircraft : {airframe.all_up_weight_kg:.2f} kg, "
          f"{airframe.rotor_count}x {airframe.rotor_diameter_m * 39.37:.0f}\" rotors")
    print(f"Battery  : {battery.capacity_mah:.0f} mAh {battery.cells}S, "
          f"{battery.energy_wh:.0f} Wh ({battery.usable_energy_wh:.0f} Wh usable)")
    print(f"Hover    : {airframe.hover_power_w():.0f} W, "
          f"{airframe.endurance_min(battery):.1f} min\n")

    print(f"{'speed':>6} {'power':>7} {'endurance':>10} {'range':>8} {'tilt':>6}")
    print("-" * 42)
    for speed in [0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 24]:
        print(
            f"{speed:>4} m/s {airframe.power_w(speed):>6.0f} W"
            f" {airframe.endurance_min(battery, speed):>8.1f} min"
            f" {airframe.range_km(battery, speed):>6.1f} km"
            f" {airframe.tilt_deg(speed):>5.1f}°"
        )

    endurance_speed = airframe.best_endurance_speed_ms()
    range_speed = airframe.best_range_speed_ms()
    shutter_limit = CAM.max_ground_speed_ms(args.altitude, args.overlap)
    survey_speed = min(range_speed, shutter_limit)

    print(f"\nBest endurance : {endurance_speed:>5.1f} m/s "
          f"({airframe.endurance_min(battery, endurance_speed):.1f} min aloft)")
    print(f"Best range     : {range_speed:>5.1f} m/s "
          f"({airframe.range_km(battery, range_speed):.1f} km covered)")
    print(f"Shutter limit  : {shutter_limit:>5.1f} m/s "
          f"(at {args.altitude:.0f} m, {args.overlap:.0%} overlap)")
    print(f"→ Survey at    : {survey_speed:>5.1f} m/s", end="")
    print("  (camera-limited)" if shutter_limit < range_speed else "  (range-limited)")

    hover_endurance = airframe.endurance_min(battery)
    survey_endurance = airframe.endurance_min(battery, survey_speed)
    print(
        f"\nFlying the survey at {survey_speed:.1f} m/s rather than hovering gives "
        f"{survey_endurance:.1f} min instead of {hover_endurance:.1f} — "
        f"{survey_endurance / hover_endurance - 1:+.0%}."
    )


if __name__ == "__main__":
    main()
