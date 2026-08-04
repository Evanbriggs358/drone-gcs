"""Pre-flight gating tests.

The point of this layer is that it refuses to let a bad setup fly, and explains
itself. Both halves are tested: the verdict, and the wording of the reason.
"""

from __future__ import annotations

import time

import pytest

from gcs.link.preflight import PreflightLimits, Severity, run_preflight
from gcs.link.state import GpsFix, VehicleState


def healthy_state() -> VehicleState:
    return VehicleState(
        connected=True,
        last_heartbeat_s=time.monotonic(),
        armed=False,
        mode="LOITER",
        lat=47.6062,
        lon=-122.3321,
        relative_alt_m=0.0,
        gps_fix=GpsFix.FIX_3D,
        satellites=14,
        hdop=0.9,
        battery_v=25.0,
        battery_remaining_pct=95,
    )


def report(state=None, **kwargs):
    defaults = dict(
        mission_waypoint_count=20,
        companion_ready=True,
        camera_ready=True,
        free_disk_gb=64.0,
        estimated_photos=200,
    )
    return run_preflight(state or healthy_state(), **{**defaults, **kwargs})


def find(rep, name):
    return next(c for c in rep.checks if c.name == name)


class TestHappyPath:
    def test_healthy_setup_is_ready(self):
        rep = report()
        assert rep.ready_to_arm, rep.summary()
        assert rep.blockers == []
        assert rep.summary().startswith("Ready to arm")

    def test_every_check_reports_a_detail(self):
        assert all(c.detail for c in report().checks)


class TestGps:
    def test_no_fix_blocks(self):
        state = healthy_state()
        state.gps_fix = GpsFix.NO_FIX
        rep = report(state)
        assert not rep.ready_to_arm
        assert "3D fix" in find(rep, "GPS fix").detail

    def test_too_few_satellites_blocks_and_names_the_count(self):
        state = healthy_state()
        state.satellites = 6
        rep = report(state)
        assert not rep.ready_to_arm
        detail = find(rep, "GPS fix").detail
        assert "6 satellites" in detail and "need 10" in detail

    def test_poor_hdop_blocks_with_a_suggested_action(self):
        state = healthy_state()
        state.hdop = 3.4
        rep = report(state)
        assert not rep.ready_to_arm
        assert "wait for more satellites" in find(rep, "GPS accuracy").detail

    def test_missing_hdop_warns_but_does_not_block(self):
        state = healthy_state()
        state.hdop = None
        rep = report(state)
        assert rep.ready_to_arm
        assert find(rep, "GPS accuracy").severity is Severity.WARNING


class TestBattery:
    def test_low_cell_voltage_blocks(self):
        state = healthy_state()
        state.battery_v = 22.0  # 3.67 V/cell on 6S
        rep = report(state)
        assert not rep.ready_to_arm
        assert "V/cell" in find(rep, "Battery").detail

    def test_healthy_pack_reports_cell_count(self):
        assert "6S" in find(report(), "Battery").detail

    def test_missing_voltage_blocks(self):
        state = healthy_state()
        state.battery_v = None
        assert not report(state).ready_to_arm

    def test_low_percentage_warns_only(self):
        state = healthy_state()
        state.battery_remaining_pct = 40
        rep = report(state)
        assert rep.ready_to_arm
        assert find(rep, "Battery").severity is Severity.WARNING


class TestLink:
    def test_no_heartbeat_blocks(self):
        rep = report(VehicleState())
        assert not rep.ready_to_arm
        assert "No heartbeat" in find(rep, "Telemetry link").detail

    def test_stale_heartbeat_blocks(self):
        state = healthy_state()
        state.last_heartbeat_s = time.monotonic() - 10.0
        rep = report(state)
        assert not rep.ready_to_arm
        assert "ago" in find(rep, "Telemetry link").detail


class TestMissionAndPayload:
    def test_no_mission_blocks(self):
        assert not report(mission_waypoint_count=0).ready_to_arm

    def test_already_armed_blocks(self):
        state = healthy_state()
        state.armed = True
        rep = report(state)
        assert not rep.ready_to_arm
        assert "disarm" in find(rep, "Arming state").detail.lower()

    def test_companion_down_blocks(self):
        assert not report(companion_ready=False).ready_to_arm

    def test_camera_down_blocks(self):
        """A mapping flight without a camera is a wasted battery."""
        assert not report(camera_ready=False).ready_to_arm

    def test_insufficient_disk_blocks_and_quantifies(self):
        rep = report(free_disk_gb=0.5, estimated_photos=1000)
        assert not rep.ready_to_arm
        detail = find(rep, "Storage").detail
        assert "0.5 GB free" in detail and "4.9 GB" in detail


class TestSummary:
    def test_summary_names_the_first_blocker(self):
        state = healthy_state()
        state.satellites = 4
        assert "GPS fix" in report(state).summary()

    def test_summary_counts_additional_blockers(self):
        rep = report(VehicleState())
        assert "more)" in rep.summary()

    def test_summary_counts_warnings_when_ready(self):
        state = healthy_state()
        state.hdop = None
        assert "1 warning" in report(state).summary()


class TestLimits:
    def test_thresholds_are_configurable(self):
        state = healthy_state()
        state.satellites = 8
        strict = PreflightLimits(min_satellites=12)
        lenient = PreflightLimits(min_satellites=6)
        assert not run_preflight(state, mission_waypoint_count=1, limits=strict).ready_to_arm
        assert run_preflight(state, mission_waypoint_count=1, limits=lenient).ready_to_arm
