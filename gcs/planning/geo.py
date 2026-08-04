"""Geodesy helpers for survey-scale areas.

Survey polygons are small — hundreds of metres to a few kilometres — so a local
tangent-plane approximation centred on the polygon is accurate to well under a
metre. That is far below the GPS error the aircraft flies with, so a full
projection library would be precision theatre.

Coordinates are (latitude, longitude) in degrees. Local frame is metres, with
+x east and +y north.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import asin, cos, radians, sin, sqrt

#: Metres per degree of latitude. Varies by ~0.6% pole to equator; the mean is
#: well inside our error budget.
_M_PER_DEG_LAT = 111_320.0

EARTH_RADIUS_M = 6_371_000.0

LatLon = tuple[float, float]


def haversine_m(a: LatLon, b: LatLon) -> float:
    """Great-circle distance in metres between two (lat, lon) points."""
    lat1, lon1 = radians(a[0]), radians(a[1])
    lat2, lon2 = radians(b[0]), radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2.0 * EARTH_RADIUS_M * asin(sqrt(h))


@dataclass(frozen=True)
class LocalFrame:
    """Flat-earth projection anchored at an origin latitude/longitude."""

    origin_lat: float
    origin_lon: float

    @classmethod
    def centred_on(cls, points: list[LatLon]) -> LocalFrame:
        if not points:
            raise ValueError("cannot build a local frame from no points")
        return cls(
            origin_lat=sum(p[0] for p in points) / len(points),
            origin_lon=sum(p[1] for p in points) / len(points),
        )

    @property
    def _m_per_deg_lon(self) -> float:
        return _M_PER_DEG_LAT * cos(radians(self.origin_lat))

    def to_local(self, point: LatLon) -> tuple[float, float]:
        """(lat, lon) -> (east_m, north_m) relative to the origin."""
        lat, lon = point
        return (
            (lon - self.origin_lon) * self._m_per_deg_lon,
            (lat - self.origin_lat) * _M_PER_DEG_LAT,
        )

    def to_latlon(self, xy: tuple[float, float]) -> LatLon:
        """(east_m, north_m) -> (lat, lon)."""
        east, north = xy
        return (
            self.origin_lat + north / _M_PER_DEG_LAT,
            self.origin_lon + east / self._m_per_deg_lon,
        )


def rotate(xy: tuple[float, float], angle_rad: float) -> tuple[float, float]:
    """Rotate a local-frame point counter-clockwise about the origin."""
    x, y = xy
    c, s = cos(angle_rad), sin(angle_rad)
    return (x * c - y * s, x * s + y * c)


def polygon_area_m2(points: list[tuple[float, float]]) -> float:
    """Shoelace area of a local-frame polygon, always positive."""
    if len(points) < 3:
        return 0.0
    total = 0.0
    for i in range(len(points)):
        x1, y1 = points[i]
        x2, y2 = points[(i + 1) % len(points)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0
