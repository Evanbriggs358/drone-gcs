"""Planning API tests.

The endpoint is a thin wrapper over well-tested planning code, so these cover
the wrapper's own responsibilities: validating input, mapping names to types,
and returning a shape the frontend can rely on.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from gcs.server.app import app

client = TestClient(app)

SQUARE = [
    [47.605, -122.335],
    [47.605, -122.330],
    [47.609, -122.330],
    [47.609, -122.335],
]


def plan(**overrides):
    body = {"polygon": SQUARE, "altitude_m": 80.0, **overrides}
    return client.post("/api/plan", json=body)


class TestPlanning:
    def test_returns_a_usable_plan(self):
        response = plan()
        assert response.status_code == 200

        data = response.json()
        assert data["waypoints"] and data["photo_points"]
        # Two waypoints per flight line, as the mission builder expects.
        assert len(data["waypoints"]) == 2 * data["stats"]["line_count"]

    def test_stats_contain_what_the_panel_displays(self):
        stats = plan().json()["stats"]
        for key in (
            "area_acres", "gsd_cm_per_px", "line_count", "photo_count",
            "path_length_m", "duration_min", "photo_spacing_m",
            "line_spacing_m", "max_ground_speed_ms",
        ):
            assert key in stats, f"missing {key}"

    def test_higher_altitude_gives_coarser_detail_and_fewer_photos(self):
        low = plan(altitude_m=40).json()["stats"]
        high = plan(altitude_m=120).json()["stats"]
        assert high["gsd_cm_per_px"] > low["gsd_cm_per_px"]
        assert high["photo_count"] < low["photo_count"]

    def test_crosshatch_costs_more_flying(self):
        nadir = plan(pattern="nadir").json()["stats"]
        cross = plan(pattern="crosshatch").json()["stats"]
        assert cross["line_count"] > nadir["line_count"]
        assert cross["duration_min"] > nadir["duration_min"]

    def test_coordinates_come_back_as_lat_lon_pairs(self):
        """The map plots these directly; a transposition would be invisible
        in the numbers but put the survey in the wrong hemisphere."""
        waypoint = plan().json()["waypoints"][0]
        assert 47.5 < waypoint[0] < 47.7
        assert -122.4 < waypoint[1] < -122.3


class TestWarnings:
    def test_nadir_warns_about_3d(self):
        assert any("3D" in w for w in plan(pattern="nadir").json()["warnings"])

    def test_outrunning_the_shutter_warns(self):
        warnings = plan(altitude_m=40, ground_speed_ms=25).json()["warnings"]
        assert any("outruns the shutter" in w for w in warnings)

    def test_low_overlap_warns(self):
        warnings = plan(front_overlap=0.4, side_overlap=0.4).json()["warnings"]
        assert any("Overlap below 60%" in w for w in warnings)


class TestValidation:
    def test_too_few_vertices_is_rejected(self):
        assert client.post(
            "/api/plan", json={"polygon": SQUARE[:2], "altitude_m": 80}
        ).status_code == 422

    @pytest.mark.parametrize("altitude", [0, -10, 900])
    def test_implausible_altitude_is_rejected(self, altitude):
        assert plan(altitude_m=altitude).status_code == 422

    @pytest.mark.parametrize("overlap", [-0.1, 1.0, 1.5])
    def test_out_of_range_overlap_is_rejected(self, overlap):
        assert plan(front_overlap=overlap).status_code == 422

    def test_unknown_pattern_is_rejected_with_a_readable_message(self):
        response = plan(pattern="spiral")
        assert response.status_code == 400
        assert "spiral" in response.json()["detail"]

    def test_unknown_camera_falls_back_rather_than_failing(self):
        """A stale camera name in the UI should not break planning."""
        assert plan(camera="Nonexistent Camera").status_code == 200


class TestCameras:
    def test_camera_list_is_offered(self):
        cameras = client.get("/api/cameras").json()
        assert cameras
        assert all({"name", "megapixels", "hfov_deg"} <= set(c) for c in cameras)
