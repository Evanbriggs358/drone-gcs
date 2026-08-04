"""Pre-flight checks that gate the launch button.

The old workflow was a sequence of SSH commands with no verdict at the end — you
found out something was wrong by watching the aircraft misbehave. This module
produces a single answer to "is it safe to arm?", and when the answer is no, says
why in language that points at the fix.

Every failure message names the actual measured value. "GPS: 6 satellites, need
10" is actionable; "GPS check failed" is not.

Pure logic over a :class:`~gcs.link.state.VehicleState` snapshot — no I/O, fully
testable without an aircraft.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .state import VehicleState


class Severity(str, Enum):
    #: Must pass. The launch button stays disabled.
    BLOCKING = "blocking"
    #: Flight is possible but the data or the margin will suffer.
    WARNING = "warning"


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str
    severity: Severity = Severity.BLOCKING

    @property
    def blocks_launch(self) -> bool:
        return not self.passed and self.severity is Severity.BLOCKING


@dataclass(frozen=True)
class PreflightReport:
    checks: list[Check]

    @property
    def ready_to_arm(self) -> bool:
        return not any(c.blocks_launch for c in self.checks)

    @property
    def blockers(self) -> list[Check]:
        return [c for c in self.checks if c.blocks_launch]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if not c.passed and not c.blocks_launch]

    def summary(self) -> str:
        if self.ready_to_arm:
            warned = len(self.warnings)
            suffix = f" ({warned} warning{'s' if warned != 1 else ''})" if warned else ""
            return f"Ready to arm{suffix}"
        blockers = self.blockers
        first = blockers[0]
        extra = f" (+{len(blockers) - 1} more)" if len(blockers) > 1 else ""
        return f"Not ready — {first.name}: {first.detail}{extra}"


@dataclass(frozen=True)
class PreflightLimits:
    """Thresholds. Defaults are deliberately conservative for survey work."""

    max_link_age_s: float = 3.0
    min_satellites: int = 10
    max_hdop: float = 2.0
    #: A 6S pack at 3.8 V/cell is around 50% charge — too low to start a survey.
    min_cell_voltage: float = 3.8
    #: Below this, a full-length mission will not finish.
    min_battery_pct: int = 60


def run_preflight(
    state: VehicleState,
    *,
    mission_waypoint_count: int = 0,
    companion_ready: bool = True,
    companion_detail: str = "",
    camera_ready: bool = True,
    camera_detail: str = "",
    free_disk_gb: float | None = None,
    estimated_photos: int = 0,
    limits: PreflightLimits | None = None,
) -> PreflightReport:
    """Evaluate every gate and return a single verdict."""
    limits = limits or PreflightLimits()
    checks: list[Check] = []

    checks.append(_check_link(state, limits))
    checks.append(_check_gps(state, limits))
    checks.append(_check_hdop(state, limits))
    checks.append(_check_battery(state, limits))
    checks.append(_check_not_armed(state))
    checks.append(_check_mission(mission_waypoint_count))
    checks.append(
        Check(
            "Companion computer",
            companion_ready,
            companion_detail or ("Pi service responding" if companion_ready else "Pi service not responding"),
        )
    )
    checks.append(
        Check(
            "Camera",
            camera_ready,
            camera_detail or ("Camera detected" if camera_ready else "Camera not detected"),
        )
    )
    checks.append(_check_disk(free_disk_gb, estimated_photos))

    return PreflightReport(checks)


def _check_link(state: VehicleState, limits: PreflightLimits) -> Check:
    age = state.link_age_s()
    if age is None:
        return Check("Telemetry link", False, "No heartbeat received from the flight controller")
    if age > limits.max_link_age_s:
        return Check("Telemetry link", False, f"Last heartbeat {age:.1f} s ago")
    return Check("Telemetry link", True, f"Heartbeat {age:.1f} s ago, mode {state.mode}")


def _check_gps(state: VehicleState, limits: PreflightLimits) -> Check:
    if not state.gps_fix.is_usable:
        return Check("GPS fix", False, f"{state.gps_fix.name} — need a 3D fix or better")
    if state.satellites < limits.min_satellites:
        return Check(
            "GPS fix",
            False,
            f"{state.satellites} satellites, need {limits.min_satellites}",
        )
    return Check("GPS fix", True, f"{state.gps_fix.name}, {state.satellites} satellites")


def _check_hdop(state: VehicleState, limits: PreflightLimits) -> Check:
    if state.hdop is None:
        return Check("GPS accuracy", False, "HDOP not reported", Severity.WARNING)
    if state.hdop > limits.max_hdop:
        return Check(
            "GPS accuracy",
            False,
            f"HDOP {state.hdop:.1f}, want below {limits.max_hdop:.1f} — "
            "wait for more satellites or move away from obstructions",
        )
    return Check("GPS accuracy", True, f"HDOP {state.hdop:.1f}")


def _check_battery(state: VehicleState, limits: PreflightLimits) -> Check:
    if state.battery_v is None:
        return Check("Battery", False, "No battery voltage reported")

    per_cell = state.battery_v_per_cell
    if per_cell is None:
        return Check(
            "Battery",
            False,
            f"{state.battery_v:.1f} V — cell count not recognised, check the monitor calibration",
            Severity.WARNING,
        )
    if per_cell < limits.min_cell_voltage:
        return Check(
            "Battery",
            False,
            f"{state.battery_v:.1f} V ({per_cell:.2f} V/cell) — below "
            f"{limits.min_cell_voltage:.2f} V/cell, not enough for a survey",
        )
    if (
        state.battery_remaining_pct is not None
        and state.battery_remaining_pct < limits.min_battery_pct
    ):
        return Check(
            "Battery",
            False,
            f"{state.battery_remaining_pct}% remaining, want {limits.min_battery_pct}%+",
            Severity.WARNING,
        )
    return Check(
        "Battery",
        True,
        f"{state.battery_v:.1f} V ({state.cells}S, {per_cell:.2f} V/cell)",
    )


def _check_not_armed(state: VehicleState) -> Check:
    if state.armed:
        return Check("Arming state", False, "Already armed — disarm before uploading a mission")
    return Check("Arming state", True, "Disarmed")


def _check_mission(waypoint_count: int) -> Check:
    if waypoint_count <= 0:
        return Check("Mission", False, "No mission loaded")
    return Check("Mission", True, f"{waypoint_count} waypoints loaded and verified")


def _check_disk(free_gb: float | None, estimated_photos: int) -> Check:
    if free_gb is None:
        return Check("Storage", False, "Free space on the Pi unknown", Severity.WARNING)

    # 12 MP JPEGs at quality 95 run around 5 MB.
    needed_gb = estimated_photos * 5 / 1024
    if free_gb < needed_gb:
        return Check(
            "Storage",
            False,
            f"{free_gb:.1f} GB free, mission needs about {needed_gb:.1f} GB "
            f"for {estimated_photos} photos",
        )
    return Check("Storage", True, f"{free_gb:.1f} GB free, mission needs about {needed_gb:.1f} GB")
