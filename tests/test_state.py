"""Vehicle state assembly from MAVLink messages.

Uses real pymavlink message objects rather than stubs, so a wrong field name
fails here instead of surviving until the first flight.
"""

from __future__ import annotations

from math import radians

import pytest
from pymavlink.dialects.v20 import ardupilotmega as m

from gcs.link.state import GpsFix, VehicleState, apply_message

ARMED = 0b1000_0000


def heartbeat(custom_mode: int = 0, armed: bool = False):
    return m.MAVLink_heartbeat_message(
        type=2, autopilot=3, base_mode=ARMED if armed else 0,
        custom_mode=custom_mode, system_status=4, mavlink_version=3,
    )


def global_position(lat=47.6062, lon=-122.3321, rel_alt_m=100.0, hdg_deg=90.0):
    return m.MAVLink_global_position_int_message(
        time_boot_ms=1000, lat=int(lat * 1e7), lon=int(lon * 1e7),
        alt=int((rel_alt_m + 50) * 1000), relative_alt=int(rel_alt_m * 1000),
        vx=0, vy=0, vz=0, hdg=int(hdg_deg * 100),
    )


def gps_raw(fix_type=3, satellites=12, eph=120):
    return m.MAVLink_gps_raw_int_message(
        time_usec=0, fix_type=fix_type, lat=0, lon=0, alt=0, eph=eph, epv=100,
        vel=0, cog=0, satellites_visible=satellites, alt_ellipsoid=0,
        h_acc=0, v_acc=0, vel_acc=0, hdg_acc=0, yaw=0,
    )


def attitude(roll_deg=0.0, pitch_deg=0.0, yaw_deg=0.0):
    return m.MAVLink_attitude_message(
        time_boot_ms=1000, roll=radians(roll_deg), pitch=radians(pitch_deg),
        yaw=radians(yaw_deg), rollspeed=0, pitchspeed=0, yawspeed=0,
    )


def sys_status(voltage_mv=24_000, current_ca=1_500, remaining_pct=85):
    return m.MAVLink_sys_status_message(
        onboard_control_sensors_present=0, onboard_control_sensors_enabled=0,
        onboard_control_sensors_health=0, load=250, voltage_battery=voltage_mv,
        current_battery=current_ca, battery_remaining=remaining_pct,
        drop_rate_comm=0, errors_comm=0, errors_count1=0, errors_count2=0,
        errors_count3=0, errors_count4=0,
    )


def statustext(text: str):
    return m.MAVLink_statustext_message(severity=6, text=text.encode(), id=0, chunk_seq=0)


class TestHeartbeat:
    def test_marks_connected_and_reads_mode(self):
        state = apply_message(VehicleState(), heartbeat(custom_mode=3))
        assert state.connected
        assert state.mode == "AUTO"
        assert not state.armed

    def test_reads_armed_flag(self):
        assert apply_message(VehicleState(), heartbeat(armed=True)).armed

    def test_unknown_mode_is_reported_not_swallowed(self):
        assert apply_message(VehicleState(), heartbeat(custom_mode=99)).mode == "MODE_99"

    @pytest.mark.parametrize(
        "code,name", [(0, "STABILIZE"), (5, "LOITER"), (6, "RTL"), (16, "POSHOLD")]
    )
    def test_known_copter_modes(self, code, name):
        assert apply_message(VehicleState(), heartbeat(custom_mode=code)).mode == name


class TestPosition:
    def test_scales_position_and_altitude(self):
        state = apply_message(VehicleState(), global_position())
        assert state.lat == pytest.approx(47.6062, abs=1e-6)
        assert state.lon == pytest.approx(-122.3321, abs=1e-6)
        assert state.relative_alt_m == pytest.approx(100.0)
        assert state.heading_deg == pytest.approx(90.0)

    def test_unknown_heading_sentinel_becomes_none(self):
        msg = global_position()
        msg.hdg = 65535
        assert apply_message(VehicleState(), msg).heading_deg is None


class TestGnss:
    def test_reads_fix_and_satellites(self):
        state = apply_message(VehicleState(), gps_raw(fix_type=3, satellites=14, eph=90))
        assert state.gps_fix is GpsFix.FIX_3D
        assert state.satellites == 14
        assert state.hdop == pytest.approx(0.9)

    @pytest.mark.parametrize("fix,usable", [(0, False), (1, False), (2, False), (3, True), (6, True)])
    def test_usable_fix_threshold(self, fix, usable):
        assert apply_message(VehicleState(), gps_raw(fix_type=fix)).gps_fix.is_usable is usable

    @pytest.mark.parametrize("sentinel", [0, 65535])
    def test_unknown_hdop_sentinels(self, sentinel):
        assert apply_message(VehicleState(), gps_raw(eph=sentinel)).hdop is None


class TestAttitude:
    def test_converts_radians_to_degrees(self):
        state = apply_message(VehicleState(), attitude(roll_deg=15.0, pitch_deg=-5.0, yaw_deg=270.0))
        assert state.roll_deg == pytest.approx(15.0)
        assert state.pitch_deg == pytest.approx(-5.0)
        assert state.yaw_deg == pytest.approx(270.0)

    def test_negative_yaw_wraps_to_compass_range(self):
        assert apply_message(VehicleState(), attitude(yaw_deg=-90.0)).yaw_deg == pytest.approx(270.0)


class TestBattery:
    def test_reads_voltage_current_and_remaining(self):
        state = apply_message(VehicleState(), sys_status(voltage_mv=24_000, current_ca=1_500))
        assert state.battery_v == pytest.approx(24.0)
        assert state.battery_current_a == pytest.approx(15.0)
        assert state.battery_remaining_pct == 85

    def test_infers_6s_pack(self):
        state = apply_message(VehicleState(), sys_status(voltage_mv=24_000))
        assert state.cells == 6
        assert state.battery_v_per_cell == pytest.approx(4.0)

    def test_unmeasured_current_is_none_not_negative(self):
        state = apply_message(VehicleState(), sys_status(current_ca=-1, remaining_pct=-1))
        assert state.battery_current_a is None
        assert state.battery_remaining_pct is None


class TestStatusText:
    def test_collects_messages(self):
        state = apply_message(VehicleState(), statustext("EKF3 IMU0 is using GPS"))
        assert state.messages == ["EKF3 IMU0 is using GPS"]

    def test_keeps_the_backlog_bounded(self):
        state = VehicleState()
        for i in range(200):
            apply_message(state, statustext(f"message {i}"))
        assert len(state.messages) == 50
        assert state.messages[-1] == "message 199"


class TestForeignHeartbeats:
    """Regression tests for a bug that aborted SITL missions at random points.

    Mission Planner, companion computers, and gimbals all emit HEARTBEAT with
    base_mode 0. Treating those as the vehicle's makes the app believe the
    aircraft disarmed mid-flight.
    """

    def gcs_heartbeat(self):
        # MAV_TYPE_GCS = 6, base_mode 0.
        return m.MAVLink_heartbeat_message(
            type=6, autopilot=8, base_mode=0, custom_mode=0,
            system_status=4, mavlink_version=3,
        )

    def test_gcs_heartbeat_cannot_clear_the_armed_flag(self):
        state = apply_message(VehicleState(), heartbeat(custom_mode=3, armed=True))
        apply_message(state, self.gcs_heartbeat())
        assert state.armed, "a ground station's heartbeat must not disarm the vehicle"
        assert state.mode == "AUTO"

    def test_gcs_heartbeat_does_not_mark_the_vehicle_connected(self):
        state = apply_message(VehicleState(), self.gcs_heartbeat())
        assert not state.connected

    @pytest.mark.parametrize("mav_type", [5, 6, 18, 26, 27])
    def test_non_vehicle_heartbeats_are_all_ignored(self, mav_type):
        state = apply_message(VehicleState(), heartbeat(custom_mode=5, armed=True))
        foreign = m.MAVLink_heartbeat_message(
            type=mav_type, autopilot=8, base_mode=0, custom_mode=0,
            system_status=4, mavlink_version=3,
        )
        apply_message(state, foreign)
        assert state.armed and state.mode == "LOITER"


class TestRobustness:
    def test_unknown_message_types_are_ignored(self):
        state = VehicleState()
        apply_message(state, m.MAVLink_ping_message(time_usec=0, seq=0, target_system=0, target_component=0))
        assert state.mode == "UNKNOWN"

    def test_state_accumulates_across_messages(self):
        state = VehicleState()
        for msg in (heartbeat(custom_mode=3), global_position(), gps_raw(), sys_status()):
            apply_message(state, msg)
        assert state.mode == "AUTO"
        assert state.lat is not None
        assert state.satellites == 12
        assert state.battery_v == pytest.approx(24.0)
