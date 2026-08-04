"""Companion computer: camera capture, geotagging, offload.

Runs on the Raspberry Pi 4B in production. The geotagging and validation logic
here is hardware-independent so it can be developed and tested on any machine.
"""

from .geotag import GeoTag, PhotoProblem, read_geotag, sharpness, validate_photos, write_geotag

__all__ = [
    "GeoTag",
    "PhotoProblem",
    "read_geotag",
    "sharpness",
    "validate_photos",
    "write_geotag",
]
