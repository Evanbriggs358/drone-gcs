"""Link layer: MAVLink message handling, vehicle state, pre-flight gating."""

from .preflight import Check, PreflightLimits, PreflightReport, Severity, run_preflight
from .state import COPTER_MODES, GpsFix, VehicleState, apply_message

__all__ = [
    "COPTER_MODES",
    "Check",
    "GpsFix",
    "PreflightLimits",
    "PreflightReport",
    "Severity",
    "VehicleState",
    "apply_message",
    "run_preflight",
]
