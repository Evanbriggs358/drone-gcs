"""Companion computer: camera capture, geotagging, offload.

Runs on the Raspberry Pi 4B in production. The geotagging and validation logic
here is hardware-independent so it can be developed and tested on any machine.
"""

from .camera import Camera, FakeCamera, PiCameraModule3, open_camera
from .capture import (
    CameraFeedbackTrigger,
    CaptureService,
    CaptureSession,
    CaptureStats,
    DistanceTrigger,
    TriggerEvent,
)
from .geotag import GeoTag, PhotoProblem, read_geotag, sharpness, validate_photos, write_geotag

__all__ = [
    "Camera",
    "CameraFeedbackTrigger",
    "CaptureService",
    "CaptureSession",
    "CaptureStats",
    "DistanceTrigger",
    "FakeCamera",
    "GeoTag",
    "PhotoProblem",
    "PiCameraModule3",
    "TriggerEvent",
    "open_camera",
    "read_geotag",
    "sharpness",
    "validate_photos",
    "write_geotag",
]
