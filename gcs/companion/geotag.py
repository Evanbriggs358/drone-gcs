"""EXIF geotagging for captured survey imagery.

A photogrammetry photo is only useful if it knows where it was taken. This module
writes position and camera heading into a JPEG's EXIF, and reads it back for
validation before an expensive reconstruction is started.

Carried over from the previous system's approach, with one addition: **camera
heading is written as well as position**. The old capture daemon read roll, pitch,
and yaw from the flight controller and then discarded them, storing only
latitude, longitude, and altitude. Heading measurably helps the photogrammetry
solver converge, and it costs nothing to record.

Altitude should be **above mean sea level**, not above launch. Reconstruction
software interprets ``GPSAltitude`` as AMSL, and feeding it relative altitude
produces a model floating at the wrong elevation.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

import piexif

#: Denominator used for the seconds component of coordinates. 1e7 keeps
#: precision far finer than GPS accuracy, well inside a millimetre of ground.
_SECONDS_DEN = 10_000_000


@dataclass(frozen=True)
class GeoTag:
    """Where and how the camera was pointing when the shutter fired."""

    lat: float
    lon: float
    #: Metres above mean sea level.
    altitude_m: float
    #: Camera heading in degrees from true north. Optional but recommended.
    yaw_deg: float | None = None
    #: Capture time, UTC. Defaults to now when writing.
    timestamp: datetime | None = None
    #: Altitude above the launch point, kept alongside for flight review.
    relative_alt_m: float | None = None

    def __post_init__(self) -> None:
        if not -90.0 <= self.lat <= 90.0:
            raise ValueError(f"latitude out of range: {self.lat}")
        if not -180.0 <= self.lon <= 180.0:
            raise ValueError(f"longitude out of range: {self.lon}")


def write_geotag(image_path: str | Path, tag: GeoTag) -> None:
    """Write position and heading into an existing JPEG's EXIF, in place."""
    path = str(image_path)
    try:
        exif = piexif.load(path)
    except Exception:  # noqa: BLE001 - a JPEG with no EXIF block at all
        exif = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}

    when = tag.timestamp or datetime.now(timezone.utc)

    gps = {
        piexif.GPSIFD.GPSVersionID: (2, 3, 0, 0),
        piexif.GPSIFD.GPSLatitudeRef: b"N" if tag.lat >= 0 else b"S",
        piexif.GPSIFD.GPSLatitude: _to_dms(abs(tag.lat)),
        piexif.GPSIFD.GPSLongitudeRef: b"E" if tag.lon >= 0 else b"W",
        piexif.GPSIFD.GPSLongitude: _to_dms(abs(tag.lon)),
        # AltitudeRef 0 means above sea level, 1 means below.
        piexif.GPSIFD.GPSAltitudeRef: 0 if tag.altitude_m >= 0 else 1,
        piexif.GPSIFD.GPSAltitude: _to_rational(abs(tag.altitude_m)),
        piexif.GPSIFD.GPSDateStamp: when.strftime("%Y:%m:%d").encode(),
        piexif.GPSIFD.GPSTimeStamp: (
            (when.hour, 1), (when.minute, 1), (when.second, 1)
        ),
    }

    if tag.yaw_deg is not None:
        gps[piexif.GPSIFD.GPSImgDirectionRef] = b"T"  # true north, not magnetic
        gps[piexif.GPSIFD.GPSImgDirection] = _to_rational(tag.yaw_deg % 360.0)

    exif["GPS"] = gps
    exif.setdefault("Exif", {})[piexif.ExifIFD.DateTimeOriginal] = when.strftime(
        "%Y:%m:%d %H:%M:%S"
    ).encode()

    piexif.insert(piexif.dump(exif), path)


def read_geotag(image_path: str | Path) -> GeoTag | None:
    """Read a geotag back out of a JPEG, or None if it carries no GPS data."""
    try:
        exif = piexif.load(str(image_path))
    except Exception:  # noqa: BLE001
        return None

    gps = exif.get("GPS") or {}
    if piexif.GPSIFD.GPSLatitude not in gps or piexif.GPSIFD.GPSLongitude not in gps:
        return None

    lat = _from_dms(gps[piexif.GPSIFD.GPSLatitude])
    if gps.get(piexif.GPSIFD.GPSLatitudeRef) == b"S":
        lat = -lat

    lon = _from_dms(gps[piexif.GPSIFD.GPSLongitude])
    if gps.get(piexif.GPSIFD.GPSLongitudeRef) == b"W":
        lon = -lon

    altitude = 0.0
    if piexif.GPSIFD.GPSAltitude in gps:
        altitude = _from_rational(gps[piexif.GPSIFD.GPSAltitude])
        if gps.get(piexif.GPSIFD.GPSAltitudeRef) == 1:
            altitude = -altitude

    yaw = None
    if piexif.GPSIFD.GPSImgDirection in gps:
        yaw = _from_rational(gps[piexif.GPSIFD.GPSImgDirection])

    return GeoTag(lat=lat, lon=lon, altitude_m=altitude, yaw_deg=yaw)


@dataclass(frozen=True)
class PhotoProblem:
    path: str
    reason: str


def validate_photos(paths: list[str | Path]) -> list[PhotoProblem]:
    """Check a set of captured photos before committing to a reconstruction.

    Photogrammetry runs take hours. Finding out afterwards that a third of the
    images have no GPS data is an expensive way to learn it.
    """
    problems: list[PhotoProblem] = []

    for path in paths:
        name = str(path)
        if not os.path.exists(name):
            problems.append(PhotoProblem(name, "file does not exist"))
            continue
        if os.path.getsize(name) == 0:
            problems.append(PhotoProblem(name, "file is empty"))
            continue

        tag = read_geotag(name)
        if tag is None:
            problems.append(PhotoProblem(name, "no GPS data in EXIF"))
            continue
        if tag.lat == 0.0 and tag.lon == 0.0:
            problems.append(
                PhotoProblem(name, "position is 0,0 — captured before GPS lock")
            )
        if tag.altitude_m <= 0.0:
            problems.append(
                PhotoProblem(name, f"implausible altitude {tag.altitude_m:.1f} m")
            )

    return problems


def sharpness(image_path: str | Path) -> float:
    """Relative sharpness score; higher is sharper.

    Variance of a Laplacian-filtered greyscale image — the standard cheap blur
    detector. Absolute values depend on scene content, so compare across images
    from one flight rather than against a fixed threshold.
    """
    from PIL import Image, ImageFilter, ImageStat

    with Image.open(image_path) as img:
        grey = img.convert("L")
        # Downscale first: sharpness ranking is preserved and 12 MP frames are slow.
        grey.thumbnail((1024, 1024))
        laplacian = grey.filter(
            ImageFilter.Kernel((3, 3), [0, 1, 0, 1, -4, 1, 0, 1, 0], scale=1)
        )
        return ImageStat.Stat(laplacian).var[0]


# -- EXIF rational helpers -------------------------------------------------


def _to_dms(degrees: float) -> tuple[tuple[int, int], ...]:
    """Decimal degrees to EXIF (degrees, minutes, seconds) rationals."""
    d = int(degrees)
    minutes_float = (degrees - d) * 60.0
    m = int(minutes_float)
    seconds = (minutes_float - m) * 60.0
    return ((d, 1), (m, 1), (round(seconds * _SECONDS_DEN), _SECONDS_DEN))


def _from_dms(dms: tuple[tuple[int, int], ...]) -> float:
    d, m, s = (_from_rational(part) for part in dms)
    return d + m / 60.0 + s / 3600.0


def _to_rational(value: float, max_denominator: int = 1_000_000) -> tuple[int, int]:
    frac = Fraction(value).limit_denominator(max_denominator)
    return (frac.numerator, frac.denominator)


def _from_rational(rational: tuple[int, int]) -> float:
    numerator, denominator = rational
    return numerator / denominator if denominator else 0.0
