"""Coverage analysis: did the flight actually photograph everything?

OpenDroneMap reports overlap, but only after a reconstruction that takes hours.
By then you have driven home. This works directly from the capture manifest or
the photos' own geotags, so it can answer the question **while you are still at
the site and can re-fly**.

A point on the ground needs to appear in several photos to be reconstructed at
all. Somewhere seen once or twice becomes a hole in the model, and the usual
causes are mundane: a dropped trigger, a gust pushing the aircraft off line, a
battery swap that lost a strip.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from math import cos, radians, sin
from pathlib import Path

from .planning.camera import Camera
from .planning.geo import LatLon, LocalFrame

#: Photos that must see a point before it reconstructs reliably. Below three,
#: structure-from-motion has too little to triangulate against.
MIN_PHOTOS_FOR_RECONSTRUCTION = 3


@dataclass(frozen=True)
class PhotoFootprint:
    """The patch of ground one photo covers, in local metres."""

    centre_x: float
    centre_y: float
    half_width: float   # across track
    half_height: float  # along track
    yaw_deg: float

    def contains(self, x: float, y: float) -> bool:
        """Is a local-frame point inside this footprint?

        The footprint is a rectangle rotated by the camera's heading, so the
        test rotates the point into the photo's own frame rather than rotating
        the rectangle.
        """
        angle = radians(-self.yaw_deg)
        dx, dy = x - self.centre_x, y - self.centre_y
        local_x = dx * cos(angle) - dy * sin(angle)
        local_y = dx * sin(angle) + dy * cos(angle)
        return abs(local_x) <= self.half_width and abs(local_y) <= self.half_height


@dataclass
class CoverageCell:
    lat: float
    lon: float
    count: int
    #: Grid indices, used to find neighbours.
    gx: int = 0
    gy: int = 0
    #: True when every surrounding cell was also photographed. A thin patch in
    #: the middle of good coverage is a real hole; a thin patch on the perimeter
    #: is just the edge of the survey, which is always thin.
    interior: bool = False


@dataclass
class CoverageReport:
    photos: int
    cells: list[CoverageCell] = field(default_factory=list)
    cell_size_m: float = 10.0
    min_required: int = MIN_PHOTOS_FOR_RECONSTRUCTION

    @property
    def covered_cells(self) -> list[CoverageCell]:
        return [c for c in self.cells if c.count >= self.min_required]

    @property
    def seen_cells(self) -> list[CoverageCell]:
        return [c for c in self.cells if c.count > 0]

    @property
    def thin_cells(self) -> list[CoverageCell]:
        """Photographed too few times to reconstruct — including not at all."""
        return [c for c in self.cells if c.count < self.min_required]

    @property
    def gap_cells(self) -> list[CoverageCell]:
        """Under-covered ground surrounded by good coverage — worth re-flying.

        Includes cells photographed **zero** times, which are the worst case: an
        earlier version recorded only cells at least one photo saw, so a complete
        hole was silently absent from the report rather than flagged.

        The perimeter of every survey is thin, because the outermost photos have
        neighbours on one side only, so only interior cells count. Reporting the
        edge would cry wolf on every flight.
        """
        return [c for c in self.thin_cells if c.interior]

    @property
    def edge_cells(self) -> list[CoverageCell]:
        return [c for c in self.cells if 0 < c.count < self.min_required and not c.interior]

    @property
    def coverage_fraction(self) -> float:
        seen = self.seen_cells
        if not seen:
            return 0.0
        return len(self.covered_cells) / len(seen)

    @property
    def is_complete(self) -> bool:
        return not self.gap_cells

    def summary(self) -> str:
        if not self.photos:
            return "No geotagged photos to analyse"

        edge_note = (
            f" The {len(self.edge_cells)} thin cells around the perimeter are "
            "normal — the outermost photos have neighbours on one side only."
            if self.edge_cells
            else ""
        )

        if self.is_complete:
            return (
                f"{self.photos} photos, no interior gaps: everywhere inside the "
                f"flown area is covered by at least {self.min_required} photos."
                + edge_note
            )

        area = len(self.gap_cells) * self.cell_size_m**2
        missed = sum(1 for c in self.gap_cells if c.count == 0)
        never = f", {missed} of them never photographed at all" if missed else ""
        return (
            f"{self.photos} photos, {len(self.gap_cells)} interior gap(s) covering "
            f"about {area:,.0f} m2{never} — surrounded by good coverage, so "
            f"something was missed. Worth re-flying before you leave." + edge_note
        )


def footprints_from_manifest(
    manifest_path: str | Path, camera: Camera, frame: LocalFrame | None = None
) -> tuple[list[PhotoFootprint], LocalFrame]:
    """Build footprints from a capture manifest written during the flight."""
    records = []
    with Path(manifest_path).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            # A trigger that failed to produce a file covers no ground.
            if record.get("file") and record.get("lat") is not None:
                records.append(record)

    positions: list[LatLon] = [(r["lat"], r["lon"]) for r in records]
    if not positions:
        return [], frame or LocalFrame(0.0, 0.0)

    frame = frame or LocalFrame.centred_on(positions)

    footprints = []
    for record in records:
        altitude = record.get("alt_rel_m")
        if not altitude or altitude <= 0:
            continue  # without height above ground the footprint is unknowable
        across, along = camera.footprint_m(altitude)
        x, y = frame.to_local((record["lat"], record["lon"]))
        footprints.append(
            PhotoFootprint(x, y, across / 2, along / 2, record.get("yaw_deg") or 0.0)
        )

    return footprints, frame


def footprints_from_photos(
    photo_paths: list[str | Path],
    camera: Camera,
    ground_altitude_m: float | None = None,
    frame: LocalFrame | None = None,
) -> tuple[list[PhotoFootprint], LocalFrame]:
    """Build footprints from geotagged photos.

    EXIF records altitude above sea level, not above ground, so the terrain
    height must be supplied or inferred. Without it, footprint size would be
    wrong by whatever the site sits above sea level — which for most places is
    a great deal.
    """
    from .companion.geotag import read_geotag

    tags = [(p, read_geotag(p)) for p in photo_paths]
    located = [(p, t) for p, t in tags if t is not None]
    if not located:
        return [], frame or LocalFrame(0.0, 0.0)

    frame = frame or LocalFrame.centred_on([(t.lat, t.lon) for _, t in located])

    if ground_altitude_m is None:
        # Assume the lowest recorded altitude is roughly ground level. Crude,
        # but far better than treating sea level as the ground.
        ground_altitude_m = min(t.altitude_m for _, t in located)

    footprints = []
    for _, tag in located:
        height = tag.altitude_m - ground_altitude_m
        if height <= 0:
            continue
        across, along = camera.footprint_m(height)
        x, y = frame.to_local((tag.lat, tag.lon))
        footprints.append(
            PhotoFootprint(x, y, across / 2, along / 2, tag.yaw_deg or 0.0)
        )

    return footprints, frame


def analyse_coverage(
    footprints: list[PhotoFootprint],
    frame: LocalFrame,
    cell_size_m: float = 10.0,
    min_required: int = MIN_PHOTOS_FOR_RECONSTRUCTION,
    margin_m: float = 20.0,
) -> CoverageReport:
    """Count how many photos see each patch of ground."""
    report = CoverageReport(
        photos=len(footprints), cell_size_m=cell_size_m, min_required=min_required
    )
    if not footprints:
        return report

    xs = [f.centre_x for f in footprints]
    ys = [f.centre_y for f in footprints]
    reach = max(max(f.half_width, f.half_height) for f in footprints) + margin_m

    x0, x1 = min(xs) - reach, max(xs) + reach
    y0, y1 = min(ys) - reach, max(ys) + reach

    # Bucket footprints by cell so each grid point only tests nearby photos.
    # Without this a 2000-photo survey is an hour of brute force.
    buckets: dict[tuple[int, int], list[PhotoFootprint]] = {}
    for footprint in footprints:
        span = max(footprint.half_width, footprint.half_height)
        for gx in range(
            int((footprint.centre_x - span - x0) // cell_size_m),
            int((footprint.centre_x + span - x0) // cell_size_m) + 1,
        ):
            for gy in range(
                int((footprint.centre_y - span - y0) // cell_size_m),
                int((footprint.centre_y + span - y0) // cell_size_m) + 1,
            ):
                buckets.setdefault((gx, gy), []).append(footprint)

    columns = int((x1 - x0) / cell_size_m) + 1
    rows = int((y1 - y0) / cell_size_m) + 1

    for gx in range(columns):
        x = x0 + (gx + 0.5) * cell_size_m
        for gy in range(rows):
            y = y0 + (gy + 0.5) * cell_size_m
            candidates = buckets.get((gx, gy)) or []
            count = sum(1 for f in candidates if f.contains(x, y))
            # Every cell is recorded, including zero-count ones. Ground nobody
            # photographed is the most important thing this can find, and
            # omitting it makes a complete hole invisible.
            lat, lon = frame.to_latlon((x, y))
            report.cells.append(
                CoverageCell(lat=lat, lon=lon, count=count, gx=gx, gy=gy)
            )

    _mark_interior(report)
    return report


def _mark_interior(report: CoverageReport) -> None:
    """Flag cells whose eight neighbours are all *well covered*.

    Distinguishes a genuine hole from the naturally thin perimeter. Requiring
    neighbours to be merely photographed is not enough: the second ring in from
    the edge is surrounded by thin cells that are themselves photographed, so it
    would be reported as an interior hole on every flight.

    A real gap is a thin patch with good coverage on every side — the signature
    of a dropped trigger or a gust pushing the aircraft off line, rather than the
    edge of the survey.
    """
    well_covered = {
        (c.gx, c.gy) for c in report.cells if c.count >= report.min_required
    }
    for cell in report.cells:
        cell.interior = all(
            (cell.gx + dx, cell.gy + dy) in well_covered
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            if (dx, dy) != (0, 0)
        )
