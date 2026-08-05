"""Mission planning: camera geometry, survey grids, flight-time estimation.

Deliberately free of I/O and hardware dependencies so it can be developed and
tested without a drone, a simulator, or a network.
"""

from .camera import CAMERAS, PI_CAMERA_MODULE_3, PI_CAMERA_MODULE_3_WIDE, Camera
from .geo import LatLon, LocalFrame, haversine_m
from .grid import Pattern, SurveyParams, SurveyPlan, Waypoint, plan_survey
from .power import CNHL_5000_6S, DEADCAT_7IN, Airframe, Battery

__all__ = [
    "CAMERAS",
    "CNHL_5000_6S",
    "DEADCAT_7IN",
    "PI_CAMERA_MODULE_3",
    "PI_CAMERA_MODULE_3_WIDE",
    "Airframe",
    "Battery",
    "Camera",
    "LatLon",
    "LocalFrame",
    "Pattern",
    "SurveyParams",
    "SurveyPlan",
    "Waypoint",
    "haversine_m",
    "plan_survey",
]
