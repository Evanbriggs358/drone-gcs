"""Endurance model tests.

Absolute numbers depend on estimated component figures, so most of these assert
*physical behaviour* — the shape of the curve and how it responds to changes —
rather than pinning values that a better drag estimate would legitimately move.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from gcs.planning.power import CNHL_5000_6S, DEADCAT_7IN, Airframe, Battery

AIRFRAME = DEADCAT_7IN
BATTERY = CNHL_5000_6S


class TestBattery:
    def test_energy_from_capacity_and_cells(self):
        # 5 Ah at 22.2 V nominal = 111 Wh
        assert BATTERY.energy_wh == pytest.approx(111.0, abs=0.5)

    def test_usable_energy_holds_a_reserve(self):
        assert BATTERY.usable_energy_wh < BATTERY.energy_wh
        assert BATTERY.usable_energy_wh == pytest.approx(BATTERY.energy_wh * 0.75)

    def test_more_cells_carry_more_energy(self):
        assert Battery(5000, 6).energy_wh > Battery(5000, 4).energy_wh


class TestPowerCurve:
    def test_cruising_costs_less_than_hovering(self):
        """Translational lift: the headline result, and the counterintuitive one."""
        assert AIRFRAME.power_w(10.0) < AIRFRAME.hover_power_w()

    def test_power_curve_is_u_shaped(self):
        best = AIRFRAME.best_endurance_speed_ms()
        assert 0 < best < 30
        assert AIRFRAME.power_w(best) < AIRFRAME.hover_power_w()
        assert AIRFRAME.power_w(best) < AIRFRAME.power_w(best + 6)

    def test_power_climbs_steeply_at_high_speed(self):
        """Parasitic power goes as the cube of speed, so it dominates eventually."""
        assert AIRFRAME.power_w(25.0) > AIRFRAME.hover_power_w()

    def test_induced_velocity_falls_with_speed(self):
        """The mechanism behind translational lift."""
        assert AIRFRAME.induced_velocity_ms(15.0) < AIRFRAME.induced_velocity_ms(0.0)

    def test_hover_induced_velocity_matches_momentum_theory(self):
        from math import sqrt

        from gcs.planning.power import AIR_DENSITY

        expected = sqrt(AIRFRAME.weight_n / (2 * AIR_DENSITY * AIRFRAME.disc_area_m2))
        assert AIRFRAME.induced_velocity_ms(0.0) == pytest.approx(expected, rel=0.01)

    def test_forward_flight_needs_more_thrust_and_more_tilt(self):
        assert AIRFRAME.thrust_n(20.0) > AIRFRAME.thrust_n(0.0)
        assert AIRFRAME.tilt_deg(20.0) > AIRFRAME.tilt_deg(5.0) > 0
        assert AIRFRAME.tilt_deg(0.0) == pytest.approx(0.0)

    def test_negative_speed_is_rejected(self):
        with pytest.raises(ValueError, match="negative"):
            AIRFRAME.power_w(-1.0)


class TestOptimalSpeeds:
    def test_range_speed_exceeds_endurance_speed(self):
        """Standard aerodynamic result: fly faster for distance than for time."""
        assert AIRFRAME.best_range_speed_ms() > AIRFRAME.best_endurance_speed_ms()

    def test_endurance_speed_really_maximises_endurance(self):
        best = AIRFRAME.best_endurance_speed_ms()
        for other in (best - 4, best - 2, best + 2, best + 4):
            if other > 0:
                assert AIRFRAME.endurance_min(BATTERY, best) >= AIRFRAME.endurance_min(
                    BATTERY, other
                )

    def test_range_speed_really_maximises_range(self):
        best = AIRFRAME.best_range_speed_ms()
        for other in (best - 4, best - 2, best + 2, best + 4):
            if other > 0:
                assert AIRFRAME.range_km(BATTERY, best) >= AIRFRAME.range_km(
                    BATTERY, other
                )

    def test_zero_speed_covers_no_ground(self):
        assert AIRFRAME.range_km(BATTERY, 0.0) == 0.0


class TestSensitivity:
    def test_heavier_aircraft_flies_for_less_time(self):
        heavy = replace(AIRFRAME, all_up_weight_kg=AIRFRAME.all_up_weight_kg * 1.25)
        assert heavy.endurance_min(BATTERY) < AIRFRAME.endurance_min(BATTERY)

    def test_bigger_battery_flies_for_longer(self):
        assert AIRFRAME.endurance_min(Battery(8000, 6)) > AIRFRAME.endurance_min(BATTERY)

    def test_draggier_airframe_loses_most_at_speed(self):
        """Drag barely matters in hover and dominates at cruise."""
        draggy = replace(AIRFRAME, drag_area_m2=AIRFRAME.drag_area_m2 * 2)
        hover_penalty = draggy.power_w(0.0) / AIRFRAME.power_w(0.0)
        cruise_penalty = draggy.power_w(20.0) / AIRFRAME.power_w(20.0)
        assert cruise_penalty > hover_penalty
        assert hover_penalty == pytest.approx(1.0, abs=0.01)

    def test_bigger_rotors_are_more_efficient(self):
        """Disc loading: more swept area per newton of thrust is cheaper."""
        big = replace(AIRFRAME, rotor_diameter_m=0.254)  # 10 inch
        assert big.hover_power_w() < AIRFRAME.hover_power_w()

    def test_avionics_draw_is_included(self):
        without = replace(AIRFRAME, avionics_power_w=0.0)
        assert AIRFRAME.hover_power_w() > without.hover_power_w()


class TestCalibration:
    def test_calibration_reproduces_the_measured_hover_time(self):
        calibrated = AIRFRAME.calibrated_to(15.0, BATTERY)
        assert calibrated.endurance_min(BATTERY) == pytest.approx(15.0, rel=1e-6)

    def test_calibration_preserves_the_shape_of_the_curve(self):
        """Scaling must not move where the optimal speeds sit."""
        calibrated = AIRFRAME.calibrated_to(12.0, BATTERY)
        assert calibrated.best_endurance_speed_ms() == pytest.approx(
            AIRFRAME.best_endurance_speed_ms(), rel=0.02
        )

    def test_a_pessimistic_measurement_scales_the_model_up(self):
        """Measuring worse endurance than predicted means more power, not less."""
        predicted = AIRFRAME.endurance_min(BATTERY)
        calibrated = AIRFRAME.calibrated_to(predicted / 2, BATTERY)
        assert calibrated.calibration > 1.0
        assert calibrated.hover_power_w() > AIRFRAME.hover_power_w()

    def test_calibration_is_idempotent(self):
        once = AIRFRAME.calibrated_to(14.0, BATTERY)
        twice = once.calibrated_to(14.0, BATTERY)
        assert twice.calibration == pytest.approx(once.calibration, rel=1e-6)

    @pytest.mark.parametrize("bad", [0.0, -5.0])
    def test_rejects_impossible_hover_times(self, bad):
        with pytest.raises(ValueError, match="positive"):
            AIRFRAME.calibrated_to(bad, BATTERY)

    def test_rejects_a_hover_time_the_avionics_alone_could_not_allow(self):
        with pytest.raises(ValueError, match="avionics"):
            AIRFRAME.calibrated_to(10_000.0, BATTERY)


class TestPlausibility:
    """Sanity bounds. Wide on purpose — these catch a model that has gone
    badly wrong, not one that is merely imprecise."""

    def test_hover_endurance_is_in_a_believable_range(self):
        assert 10.0 < AIRFRAME.endurance_min(BATTERY) < 40.0

    def test_hover_power_is_in_a_believable_range(self):
        assert 120.0 < AIRFRAME.hover_power_w() < 500.0

    def test_optimal_speeds_are_flyable(self):
        assert 4.0 < AIRFRAME.best_endurance_speed_ms() < 25.0
        assert 6.0 < AIRFRAME.best_range_speed_ms() < 30.0
