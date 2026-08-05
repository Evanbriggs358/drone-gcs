"""Coverage-gap detection tests.

The value of this analysis is entirely in its false-positive and false-negative
behaviour: it must find a real hole, and must not cry wolf over the naturally
thin edge of every survey. Both are tested directly.
"""

from __future__ import annotations

import json

import pytest

from gcs.coverage import (
    PhotoFootprint,
    analyse_coverage,
    footprints_from_manifest,
)
from gcs.planning import PI_CAMERA_MODULE_3 as CAM
from gcs.planning.geo import LocalFrame

FRAME = LocalFrame(47.6062, -122.3321)


def grid_of_photos(spacing=20.0, extent=200.0, skip=None):
    """A lawnmower-like blanket of overlapping footprints.

    ``skip`` is a callable taking (x, y) and returning True where a photo is
    missing, used to punch holes.
    """
    footprints = []
    steps = int(extent / spacing) + 1
    for i in range(steps):
        for j in range(steps):
            x, y = i * spacing, j * spacing
            if skip and skip(x, y):
                continue
            # Generous footprints so a regular grid genuinely overlaps.
            footprints.append(PhotoFootprint(x, y, 35.0, 35.0, 0.0))
    return footprints


class TestFootprint:
    def test_point_inside_is_contained(self):
        assert PhotoFootprint(0, 0, 10, 5, 0).contains(5, 2)

    def test_point_outside_is_not(self):
        assert not PhotoFootprint(0, 0, 10, 5, 0).contains(20, 0)

    def test_rotation_is_respected(self):
        """A point beyond the long axis falls outside once the photo is turned."""
        upright = PhotoFootprint(0, 0, 20, 5, 0)
        turned = PhotoFootprint(0, 0, 20, 5, 90)
        assert upright.contains(15, 0)
        assert not turned.contains(15, 0)
        assert turned.contains(0, 15)


class TestCleanSurvey:
    def test_dense_overlapping_coverage_has_no_gaps(self):
        report = analyse_coverage(grid_of_photos(), FRAME, cell_size_m=10.0)
        assert report.is_complete
        assert report.gap_cells == []

    def test_the_thin_perimeter_is_not_called_a_gap(self):
        """Every survey has a thin edge. Reporting it would cry wolf each flight."""
        report = analyse_coverage(grid_of_photos(), FRAME, cell_size_m=10.0)
        assert report.edge_cells, "expected a naturally thin perimeter"
        assert not report.gap_cells

    def test_summary_explains_the_perimeter(self):
        report = analyse_coverage(grid_of_photos(), FRAME, cell_size_m=10.0)
        assert "no interior gaps" in report.summary()
        assert "perimeter" in report.summary()


class TestHoleDetection:
    def test_a_hole_in_the_middle_is_found(self):
        """The whole point: a dropped strip surrounded by good coverage."""
        hole = lambda x, y: 80 <= x <= 120 and 80 <= y <= 120  # noqa: E731
        report = analyse_coverage(
            grid_of_photos(skip=hole), FRAME, cell_size_m=10.0
        )
        assert not report.is_complete
        assert report.gap_cells

    def test_the_hole_is_reported_in_roughly_the_right_place(self):
        hole = lambda x, y: 80 <= x <= 120 and 80 <= y <= 120  # noqa: E731
        report = analyse_coverage(grid_of_photos(skip=hole), FRAME, cell_size_m=10.0)

        centre_lat, centre_lon = FRAME.to_latlon((100.0, 100.0))
        nearest = min(
            report.gap_cells,
            key=lambda c: abs(c.lat - centre_lat) + abs(c.lon - centre_lon),
        )
        assert abs(nearest.lat - centre_lat) < 0.001
        assert abs(nearest.lon - centre_lon) < 0.001

    def test_summary_calls_for_a_re_fly(self):
        hole = lambda x, y: 80 <= x <= 120 and 80 <= y <= 120  # noqa: E731
        report = analyse_coverage(grid_of_photos(skip=hole), FRAME, cell_size_m=10.0)
        assert "re-flying" in report.summary()

    def test_a_stricter_requirement_finds_more_thin_ground(self):
        sparse = grid_of_photos(spacing=45.0)
        lenient = analyse_coverage(sparse, FRAME, cell_size_m=10.0, min_required=2)
        strict = analyse_coverage(sparse, FRAME, cell_size_m=10.0, min_required=6)
        assert len(strict.thin_cells) > len(lenient.thin_cells)


class TestEmptyInput:
    def test_no_photos_reports_plainly(self):
        report = analyse_coverage([], FRAME)
        assert report.photos == 0
        assert "No geotagged photos" in report.summary()
        assert report.coverage_fraction == 0.0


class TestManifest:
    def write(self, tmp_path, records):
        path = tmp_path / "captures.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
        return path

    def test_reads_positions_and_altitude(self, tmp_path):
        records = [
            {"seq": i, "file": f"IMG_{i:04d}.jpg", "lat": 47.6 + i * 1e-4,
             "lon": -122.3, "alt_rel_m": 80.0, "yaw_deg": 0.0}
            for i in range(5)
        ]
        footprints, _ = footprints_from_manifest(self.write(tmp_path, records), CAM)
        assert len(footprints) == 5

    def test_failed_captures_cover_no_ground(self, tmp_path):
        """A trigger that produced no file photographed nothing."""
        records = [
            {"seq": 1, "file": "IMG_0001.jpg", "lat": 47.6, "lon": -122.3,
             "alt_rel_m": 80.0, "yaw_deg": 0.0},
            {"seq": 2, "file": None, "error": "camera failure", "lat": 47.6001,
             "lon": -122.3, "alt_rel_m": 80.0, "yaw_deg": 0.0},
        ]
        footprints, _ = footprints_from_manifest(self.write(tmp_path, records), CAM)
        assert len(footprints) == 1

    def test_photos_without_height_are_skipped(self, tmp_path):
        """Footprint size is unknowable without height above ground."""
        records = [
            {"seq": 1, "file": "a.jpg", "lat": 47.6, "lon": -122.3, "alt_rel_m": None},
            {"seq": 2, "file": "b.jpg", "lat": 47.6, "lon": -122.3, "alt_rel_m": 0.0},
        ]
        footprints, _ = footprints_from_manifest(self.write(tmp_path, records), CAM)
        assert footprints == []

    def test_higher_flight_covers_more_ground_per_photo(self, tmp_path):
        low = [{"seq": 1, "file": "a.jpg", "lat": 47.6, "lon": -122.3, "alt_rel_m": 40.0}]
        high = [{"seq": 1, "file": "a.jpg", "lat": 47.6, "lon": -122.3, "alt_rel_m": 120.0}]
        low_fp, _ = footprints_from_manifest(self.write(tmp_path, low), CAM)
        high_fp, _ = footprints_from_manifest(
            self.write(tmp_path / "sub", high) if (tmp_path / "sub").mkdir() or True else None,
            CAM,
        )
        assert high_fp[0].half_width > low_fp[0].half_width
