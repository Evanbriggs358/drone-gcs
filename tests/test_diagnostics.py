"""Flight-log diagnostics tests.

Built on synthetic message records so each failure mode can be produced
deliberately: a log that actually has a corrupted compass is hard to come by on
demand, and a healthy one proves very little.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from gcs.diagnostics.logs import (
    _check_attitude_bias,
    _check_compass,
    _check_compass_interference,
    _check_gps,
    _check_magfit_readiness,
    _check_vibration,
    analyse_log,
)


def vibe(x, y, z, clip=0, n=100):
    return [
        SimpleNamespace(TimeUS=i * 1000, VibeX=x, VibeY=y, VibeZ=z,
                        Clip0=clip, Clip1=0, Clip2=0)
        for i in range(n)
    ]


def mag(magnitudes, start=0):
    """MAG messages with the given field magnitudes, along the X axis."""
    return [
        SimpleNamespace(TimeUS=(start + i) * 1000, MagX=m, MagY=0.0, MagZ=0.0)
        for i, m in enumerate(magnitudes)
    ]


def battery(currents):
    return [
        SimpleNamespace(TimeUS=i * 1000, Curr=c) for i, c in enumerate(currents)
    ]


def gps(sats=12, hdop=0.9, speed=0.0, n=100):
    return [
        SimpleNamespace(TimeUS=i * 1000, NSats=sats, HDop=hdop, Spd=speed)
        for i in range(n)
    ]


def attitude(roll, pitch, n=200):
    return [
        SimpleNamespace(TimeUS=i * 1000, Roll=roll, Pitch=pitch, Yaw=0.0)
        for i in range(n)
    ]


class TestVibration:
    def test_low_vibration_passes(self):
        assert _check_vibration({"VIBE": vibe(5, 6, 8)}).severity == "ok"

    def test_high_vibration_is_a_problem(self):
        finding = _check_vibration({"VIBE": vibe(70, 65, 80)})
        assert finding.severity == "problem"
        assert "EKF" in finding.detail

    def test_marginal_vibration_warns(self):
        assert _check_vibration({"VIBE": vibe(40, 35, 38)}).severity == "warning"

    def test_any_clipping_is_a_problem(self):
        """Clipping means the accelerometer saturated; the reading is invalid."""
        clipping = vibe(5, 5, 5, clip=0)
        for i, message in enumerate(clipping):
            message.Clip0 = i  # counter climbing through the flight
        assert _check_vibration({"VIBE": clipping}).severity == "problem"

    def test_missing_data_is_reported_as_unknown_not_healthy(self):
        assert _check_vibration({"VIBE": []}).severity == "unknown"


class TestCompass:
    def test_steady_field_passes(self):
        assert _check_compass({"MAG": mag([500.0] * 200)}).severity == "ok"

    def test_wildly_varying_field_is_a_problem(self):
        swinging = [300.0 if i % 2 else 900.0 for i in range(200)]
        finding = _check_compass({"MAG": swinging and mag(swinging)})
        assert finding.severity == "problem"
        assert "MAGFit" in finding.detail

    def test_disagreeing_compasses_are_flagged(self):
        finding = _check_compass({
            "MAG": mag([500.0] * 100),
            "MAG2": mag([900.0] * 100),
        })
        assert finding.severity == "problem"
        assert "disagree" in finding.headline

    def test_missing_data_is_unknown(self):
        assert _check_compass({}).severity == "unknown"


class TestInterference:
    def test_field_tracking_current_is_flagged(self):
        """The signature of ESC interference: compass error scaling with throttle."""
        currents = [float(i % 40) for i in range(200)]
        records = {
            "MAG": mag([500.0 + c * 4 for c in currents]),
            "BAT": battery(currents),
        }
        finding = _check_compass_interference(records)
        assert finding.severity == "problem"
        assert "throttle" in finding.detail

    def test_independent_field_and_current_passes(self):
        currents = [float(i % 40) for i in range(200)]
        records = {
            "MAG": mag([500.0 + (i % 7) for i in range(200)]),
            "BAT": battery(currents),
        }
        assert _check_compass_interference(records).severity == "ok"

    def test_missing_current_is_unknown(self):
        assert _check_compass_interference({"MAG": mag([500.0] * 100)}).severity == "unknown"


class TestGps:
    def test_good_reception_passes(self):
        assert _check_gps({"GPS": gps(sats=14, hdop=0.8)}).severity == "ok"

    def test_dropouts_are_a_problem(self):
        poor = gps(sats=12, hdop=0.9)
        poor[50].NSats = 4
        assert _check_gps({"GPS": poor}).severity == "problem"

    def test_mediocre_reception_warns(self):
        assert _check_gps({"GPS": gps(sats=8, hdop=1.8)}).severity == "warning"


class TestAttitudeBias:
    def test_level_hover_passes(self):
        records = {"ATT": attitude(0.1, -0.2), "GPS": gps(speed=0.3)}
        assert _check_attitude_bias(records).severity == "ok"

    def test_persistent_tilt_in_hover_warns(self):
        records = {"ATT": attitude(4.0, 0.2), "GPS": gps(speed=0.3)}
        finding = _check_attitude_bias(records)
        assert finding.severity == "warning"
        assert "mount" in finding.detail

    def test_cruise_pitch_is_not_mistaken_for_bias(self):
        """A multirotor pitches forward to fly. Averaging over a survey would
        show a large false 'bias' — the check must exclude forward flight."""
        records = {"ATT": attitude(0.0, -12.0), "GPS": gps(speed=12.0)}
        finding = _check_attitude_bias(records)
        assert finding.severity == "unknown"
        assert "slow flight" in finding.headline.lower() or "enough" in finding.headline

    def test_mixed_flight_judges_only_the_hover_part(self):
        hovering = attitude(0.1, 0.1, n=200)
        cruising = attitude(0.0, -15.0, n=200)
        for i, m in enumerate(cruising):
            m.TimeUS = (300 + i) * 1000
        records = {
            "ATT": hovering + cruising,
            "GPS": gps(speed=0.2, n=250) + [
                SimpleNamespace(TimeUS=(300 + i) * 1000, NSats=12, HDop=0.9, Spd=14.0)
                for i in range(250)
            ],
        }
        assert _check_attitude_bias(records).severity == "ok"

    def test_without_speed_data_it_declines_to_guess(self):
        assert _check_attitude_bias({"ATT": attitude(5.0, 5.0)}).severity == "unknown"


class TestMagfitReadiness:
    def test_complete_log_is_usable(self):
        records = {"MAG": mag([500.0] * 10), "GPS": gps(), "ATT": attitude(0, 0)}
        assert _check_magfit_readiness(records).severity == "ok"

    def test_missing_compass_makes_it_unusable(self):
        assert _check_magfit_readiness({"GPS": gps(), "ATT": attitude(0, 0)}).severity == "warning"


class TestReport:
    def test_unreadable_log_is_reported_clearly(self, tmp_path):
        empty = tmp_path / "not-a-log.BIN"
        empty.write_bytes(b"nonsense")
        report = analyse_log(empty)
        assert report.problems
        assert "dataflash" in report.problems[0].detail

    def test_findings_sort_problems_first(self):
        from gcs.diagnostics.logs import Finding, LogReport

        report = LogReport(path="x")
        report.findings = [
            Finding("a", "ok", "fine", ""),
            Finding("b", "problem", "broken", ""),
            Finding("c", "warning", "iffy", ""),
        ]
        assert [f.severity for f in report.sorted_findings()] == [
            "problem", "warning", "ok",
        ]

    def test_summary_counts_problems_and_warnings(self):
        from gcs.diagnostics.logs import Finding, LogReport

        report = LogReport(path="x")
        report.findings = [Finding("a", "problem", "x", ""), Finding("b", "warning", "y", "")]
        assert "1 problem" in report.summary()
