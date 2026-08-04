"""EXIF geotagging tests.

Writes real JPEGs and reads them back, rather than mocking piexif — the round
trip through the actual EXIF format is the thing worth testing.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from PIL import Image

from gcs.companion.geotag import (
    GeoTag,
    read_geotag,
    sharpness,
    validate_photos,
    write_geotag,
)

SEATTLE = (47.6062, -122.3321)


@pytest.fixture
def jpeg(tmp_path):
    """A small plain JPEG with no EXIF, as picamera2 would hand us."""

    def _make(name: str = "frame.jpg", colour: str = "grey") -> str:
        path = tmp_path / name
        Image.new("RGB", (64, 48), color=colour).save(path, "JPEG")
        return str(path)

    return _make


class TestRoundTrip:
    def test_position_survives_the_round_trip(self, jpeg):
        path = jpeg()
        write_geotag(path, GeoTag(lat=SEATTLE[0], lon=SEATTLE[1], altitude_m=123.4))

        tag = read_geotag(path)
        assert tag is not None
        assert tag.lat == pytest.approx(SEATTLE[0], abs=1e-7)
        assert tag.lon == pytest.approx(SEATTLE[1], abs=1e-7)
        assert tag.altitude_m == pytest.approx(123.4, abs=0.01)

    @pytest.mark.parametrize(
        "lat,lon",
        [
            (47.6062, -122.3321),   # northern, western
            (-35.363262, 149.1652),  # southern, eastern
            (-33.8688, -151.2093),   # southern, western
            (51.5074, 0.1278),       # northern, eastern
        ],
    )
    def test_all_four_hemispheres(self, jpeg, lat, lon):
        """Sign handling via EXIF ref bytes is the classic geotagging bug."""
        path = jpeg()
        write_geotag(path, GeoTag(lat=lat, lon=lon, altitude_m=100.0))

        tag = read_geotag(path)
        assert tag.lat == pytest.approx(lat, abs=1e-7)
        assert tag.lon == pytest.approx(lon, abs=1e-7)

    def test_precision_is_finer_than_gps_accuracy(self, jpeg):
        """A metre is ~9e-6 degrees; the round trip must be far tighter."""
        path = jpeg()
        precise = 47.60621234
        write_geotag(path, GeoTag(lat=precise, lon=-122.0, altitude_m=50.0))
        assert read_geotag(path).lat == pytest.approx(precise, abs=1e-8)

    def test_negative_altitude_below_sea_level(self, jpeg):
        path = jpeg()
        write_geotag(path, GeoTag(lat=31.5, lon=35.5, altitude_m=-420.0))
        assert read_geotag(path).altitude_m == pytest.approx(-420.0, abs=0.01)


class TestHeading:
    def test_yaw_is_recorded(self, jpeg):
        """The old capture daemon read yaw from the FC and then discarded it."""
        path = jpeg()
        write_geotag(path, GeoTag(lat=47.0, lon=-122.0, altitude_m=80.0, yaw_deg=137.5))
        assert read_geotag(path).yaw_deg == pytest.approx(137.5, abs=0.01)

    def test_yaw_wraps_into_compass_range(self, jpeg):
        path = jpeg()
        write_geotag(path, GeoTag(lat=47.0, lon=-122.0, altitude_m=80.0, yaw_deg=-90.0))
        assert read_geotag(path).yaw_deg == pytest.approx(270.0, abs=0.01)

    def test_yaw_is_optional(self, jpeg):
        path = jpeg()
        write_geotag(path, GeoTag(lat=47.0, lon=-122.0, altitude_m=80.0))
        assert read_geotag(path).yaw_deg is None


class TestTimestamp:
    def test_capture_time_is_written(self, jpeg):
        import piexif

        path = jpeg()
        when = datetime(2026, 8, 3, 14, 30, 12, tzinfo=timezone.utc)
        write_geotag(path, GeoTag(lat=47.0, lon=-122.0, altitude_m=80.0, timestamp=when))

        exif = piexif.load(path)
        assert exif["GPS"][piexif.GPSIFD.GPSDateStamp] == b"2026:08:03"
        assert exif["Exif"][piexif.ExifIFD.DateTimeOriginal] == b"2026:08:03 14:30:12"


class TestValidation:
    def test_rejects_out_of_range_coordinates(self):
        with pytest.raises(ValueError, match="latitude"):
            GeoTag(lat=91.0, lon=0.0, altitude_m=10.0)
        with pytest.raises(ValueError, match="longitude"):
            GeoTag(lat=0.0, lon=181.0, altitude_m=10.0)

    def test_clean_set_reports_no_problems(self, jpeg):
        paths = []
        for i in range(3):
            path = jpeg(f"img{i}.jpg")
            write_geotag(path, GeoTag(lat=47.0 + i * 1e-4, lon=-122.0, altitude_m=80.0))
            paths.append(path)
        assert validate_photos(paths) == []

    def test_untagged_photo_is_flagged(self, jpeg):
        problems = validate_photos([jpeg("untagged.jpg")])
        assert len(problems) == 1
        assert "no GPS data" in problems[0].reason

    def test_null_island_is_flagged(self, jpeg):
        """0,0 means the shutter fired before GPS lock — a real failure mode."""
        path = jpeg()
        write_geotag(path, GeoTag(lat=0.0, lon=0.0, altitude_m=80.0))
        assert any("before GPS lock" in p.reason for p in validate_photos([path]))

    def test_implausible_altitude_is_flagged(self, jpeg):
        path = jpeg()
        write_geotag(path, GeoTag(lat=47.0, lon=-122.0, altitude_m=0.0))
        assert any("altitude" in p.reason for p in validate_photos([path]))

    def test_missing_file_is_flagged(self, tmp_path):
        problems = validate_photos([str(tmp_path / "nope.jpg")])
        assert "does not exist" in problems[0].reason

    def test_empty_file_is_flagged(self, tmp_path):
        path = tmp_path / "empty.jpg"
        path.write_bytes(b"")
        assert "empty" in validate_photos([str(path)])[0].reason


class TestSharpness:
    def test_detail_scores_higher_than_flat_colour(self, tmp_path):
        import random

        flat = tmp_path / "flat.jpg"
        Image.new("RGB", (256, 256), color="grey").save(flat, "JPEG")

        detailed = tmp_path / "detailed.jpg"
        img = Image.new("RGB", (256, 256))
        rng = random.Random(0)
        img.putdata([
            (rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255))
            for _ in range(256 * 256)
        ])
        img.save(detailed, "JPEG", quality=95)

        assert sharpness(str(detailed)) > sharpness(str(flat))

    def test_blurring_reduces_the_score(self, tmp_path):
        from PIL import ImageFilter

        base = Image.open(_checkerboard(tmp_path, "sharp.jpg"))
        blurred_path = tmp_path / "blurred.jpg"
        base.filter(ImageFilter.GaussianBlur(4)).save(blurred_path, "JPEG", quality=95)

        assert sharpness(blurred_path) < sharpness(tmp_path / "sharp.jpg")


def _checkerboard(tmp_path, name: str, size: int = 256, square: int = 8):
    img = Image.new("L", (size, size))
    img.putdata([
        255 if (((i // size) // square) + ((i % size) // square)) % 2 else 0
        for i in range(size * size)
    ])
    path = tmp_path / name
    img.convert("RGB").save(path, "JPEG", quality=95)
    return path
