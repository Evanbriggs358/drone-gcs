"""Endurance model: how long the aircraft flies, and how that varies with speed.

Replaces a flat "about fifteen minutes" with something that responds to weight,
battery, and — importantly — airspeed.

**Flying faster does not simply cost endurance.** For a multirotor, most power
goes into staying airborne rather than moving forward. In hover the rotors
recirculate their own wake; as the aircraft moves they meet undisturbed air and
induced power falls. This is translational lift, and it means endurance *rises*
with speed up to a point before parasitic drag overwhelms the gain. The result
is a U-shaped power curve with a minimum somewhere above hover.

Three speeds matter, and they are not the same:

- **Best endurance** — minimum power. Longest time in the air. Right for loiter.
- **Best range** — minimum power per metre travelled. Longest distance.
- **Best survey speed** — the most ground mapped per battery. This is the range
  speed, capped by how fast the camera can shoot (see :mod:`gcs.planning.camera`).

The physics gives the *shape* of the curve. Component figures like rotor
efficiency and drag area are estimates, so the absolute numbers carry real
uncertainty. :meth:`Airframe.calibrated_to` fixes that: fly one timed hover and
the whole curve is scaled to match measured reality.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from math import atan, degrees, hypot, pi, sqrt

#: Air density at sea level, kg/m^3. Thinner air at altitude costs endurance.
AIR_DENSITY = 1.225
GRAVITY = 9.81


@dataclass(frozen=True)
class Battery:
    capacity_mah: float
    cells: int
    #: Nominal volts per cell for a LiPo. Used for energy, not for the cutoff.
    nominal_cell_v: float = 3.7
    #: Fraction of rated capacity actually usable. Running a LiPo flat ruins it;
    #: landing with 20-25% in reserve is normal practice.
    usable_fraction: float = 0.75

    @property
    def nominal_voltage(self) -> float:
        return self.cells * self.nominal_cell_v

    @property
    def energy_wh(self) -> float:
        return self.capacity_mah / 1000.0 * self.nominal_voltage

    @property
    def usable_energy_wh(self) -> float:
        return self.energy_wh * self.usable_fraction


@dataclass(frozen=True)
class Airframe:
    """Enough of the aircraft to estimate power draw."""

    all_up_weight_kg: float
    rotor_count: int
    rotor_diameter_m: float

    #: Rotor figure of merit — how close the rotor gets to the momentum-theory
    #: ideal. Small props do worse; 0.6-0.7 is realistic for 7-inch class.
    figure_of_merit: float = 0.65

    #: Drag coefficient times frontal area, m^2. A clean racing quad is nearer
    #: 0.015; a mapping build with a companion computer, GPS mast, and camera
    #: mount is draggier.
    drag_area_m2: float = 0.025

    #: Combined motor and ESC electrical efficiency.
    drivetrain_efficiency: float = 0.80

    #: Everything drawing power that is not propulsion: flight controller,
    #: companion computer, camera, GPS, radios. A Raspberry Pi 4 is not small.
    avionics_power_w: float = 8.0

    #: Scale factor applied to propulsion power, set by :meth:`calibrated_to`.
    calibration: float = 1.0

    # -- geometry ----------------------------------------------------------

    @property
    def weight_n(self) -> float:
        return self.all_up_weight_kg * GRAVITY

    @property
    def disc_area_m2(self) -> float:
        """Total area swept by all rotors."""
        return self.rotor_count * pi * (self.rotor_diameter_m / 2.0) ** 2

    # -- power -------------------------------------------------------------

    def drag_n(self, speed_ms: float) -> float:
        return 0.5 * AIR_DENSITY * self.drag_area_m2 * speed_ms**2

    def thrust_n(self, speed_ms: float) -> float:
        """Thrust required in level flight.

        Moving forward means tilting, so the rotors must fight drag as well as
        gravity and the thrust vector grows.
        """
        return hypot(self.weight_n, self.drag_n(speed_ms))

    def tilt_deg(self, speed_ms: float) -> float:
        return degrees(atan(self.drag_n(speed_ms) / self.weight_n))

    def induced_velocity_ms(self, speed_ms: float) -> float:
        """Air velocity induced through the rotor disc, from momentum theory.

        Solves ``v_i = v_h^2 / sqrt(V^2 + v_i^2)`` by fixed-point iteration.
        In hover this reduces to ``v_i = v_h``; as V grows it falls away, which
        is exactly the translational-lift benefit.
        """
        thrust = self.thrust_n(speed_ms)
        hover_induced_sq = thrust / (2.0 * AIR_DENSITY * self.disc_area_m2)

        v_i = sqrt(hover_induced_sq)  # hover value as the starting guess
        for _ in range(100):
            previous = v_i
            v_i = hover_induced_sq / sqrt(speed_ms**2 + v_i**2)
            # Damped update; the raw iteration oscillates near hover.
            v_i = (v_i + previous) / 2.0
            if abs(v_i - previous) < 1e-9:
                break
        return v_i

    def power_w(self, speed_ms: float) -> float:
        """Total electrical power draw at a given ground speed, in still air."""
        if speed_ms < 0:
            raise ValueError("speed must not be negative")

        induced = self.thrust_n(speed_ms) * self.induced_velocity_ms(speed_ms)
        parasitic = self.drag_n(speed_ms) * speed_ms

        mechanical = (induced / self.figure_of_merit) + parasitic
        propulsion = mechanical / self.drivetrain_efficiency

        return propulsion * self.calibration + self.avionics_power_w

    def hover_power_w(self) -> float:
        return self.power_w(0.0)

    # -- endurance ---------------------------------------------------------

    def endurance_min(self, battery: Battery, speed_ms: float = 0.0) -> float:
        return battery.usable_energy_wh / self.power_w(speed_ms) * 60.0

    def range_km(self, battery: Battery, speed_ms: float) -> float:
        if speed_ms <= 0:
            return 0.0
        return speed_ms * self.endurance_min(battery, speed_ms) * 60.0 / 1000.0

    # -- optimal speeds ----------------------------------------------------

    def best_endurance_speed_ms(self, max_speed_ms: float = 30.0) -> float:
        """Speed drawing least power — longest time airborne."""
        return _minimise(self.power_w, max_speed_ms)

    def best_range_speed_ms(self, max_speed_ms: float = 30.0) -> float:
        """Speed with least power per metre — most distance, and most ground
        surveyed per battery."""
        return _minimise(lambda v: self.power_w(v) / v, max_speed_ms, low=0.5)

    # -- calibration -------------------------------------------------------

    def calibrated_to(self, measured_hover_min: float, battery: Battery) -> Airframe:
        """Scale the model so predicted hover time matches a measured one.

        The single most valuable input available. Component estimates carry real
        uncertainty; one timed hover at true all-up weight replaces guesswork
        with measurement, and the shape of the curve is preserved.
        """
        if measured_hover_min <= 0:
            raise ValueError("measured hover time must be positive")

        uncalibrated = replace(self, calibration=1.0)
        propulsion = uncalibrated.hover_power_w() - self.avionics_power_w
        target_total = battery.usable_energy_wh / measured_hover_min * 60.0
        target_propulsion = target_total - self.avionics_power_w

        if target_propulsion <= 0:
            raise ValueError(
                "measured hover time implies avionics alone exceed the power draw"
            )
        return replace(self, calibration=target_propulsion / propulsion)


def _minimise(function, high: float, low: float = 0.0, iterations: int = 200) -> float:
    """Golden-section search for the minimum of a unimodal function."""
    invphi = (sqrt(5.0) - 1.0) / 2.0
    a, b = low, high
    c, d = b - invphi * (b - a), a + invphi * (b - a)

    for _ in range(iterations):
        if function(c) < function(d):
            b, d = d, c
            c = b - invphi * (b - a)
        else:
            a, c = c, d
            d = a + invphi * (b - a)
        if abs(b - a) < 1e-4:
            break
    return (a + b) / 2.0


#: The platform this project targets: 7-inch deadcat, 6S, Pi 4 companion.
#: All-up weight assumes frame, motors, ESC, FC, GPS on a mast, Pi 4 with camera,
#: wiring, and a 5000 mAh 6S pack. Weigh yours and correct it.
DEADCAT_7IN = Airframe(
    all_up_weight_kg=1.6,
    rotor_count=4,
    rotor_diameter_m=0.1778,  # 7 inches
)

CNHL_5000_6S = Battery(capacity_mah=5000, cells=6)
