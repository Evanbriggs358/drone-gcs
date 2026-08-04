"""Survey grid generation tests."""

from __future__ import annotations

import pytest

from gcs.planning.camera import PI_CAMERA_MODULE_3 as CAM
from gcs.planning.geo import LocalFrame, haversine_m
from gcs.planning.grid import Pattern, SurveyParams, plan_survey

ORIGIN_LAT, ORIGIN_LON = 47.6062, -122.3321


def square(side_m: float) -> list[tuple[float, float]]:
    """A square survey polygon of the given side length, centred on the origin."""
    frame = LocalFrame(ORIGIN_LAT, ORIGIN_LON)
    h = side_m / 2.0
    return [frame.to_latlon(p) for p in ((-h, -h), (h, -h), (h, h), (-h, h))]


def l_shape(side_m: float) -> list[tuple[float, float]]:
    """A concave polygon — scanlines across the notch produce two spans."""
    frame = LocalFrame(ORIGIN_LAT, ORIGIN_LON)
    s, h = side_m, side_m / 2.0
    pts = [(0, 0), (s, 0), (s, h), (h, h), (h, s), (0, s)]
    return [frame.to_latlon(p) for p in pts]


BASE = SurveyParams(altitude_m=100.0, ground_speed_ms=8.0)


class TestGeometry:
    def test_square_area_is_recovered(self):
        plan = plan_survey(square(200.0), CAM, BASE)
        assert plan.area_m2 == pytest.approx(40_000, rel=0.01)

    def test_line_count_follows_line_spacing(self):
        plan = plan_survey(square(200.0), CAM, BASE)
        # 200 m of polygon at ~40.8 m line spacing -> 4-5 lines.
        assert plan.line_count == pytest.approx(200 / plan.line_spacing_m, abs=1)

    def test_two_waypoints_per_line(self):
        plan = plan_survey(square(200.0), CAM, BASE)
        assert len(plan.waypoints) == 2 * plan.line_count

    def test_all_waypoints_hold_the_requested_altitude(self):
        plan = plan_survey(square(200.0), CAM, BASE)
        assert {w.alt_m for w in plan.waypoints} == {BASE.altitude_m}

    def test_lines_alternate_direction(self):
        """Serpentine ordering: the end of one line is near the start of the next."""
        plan = plan_survey(square(400.0), CAM, BASE)
        wps = plan.waypoints
        for i in range(0, len(wps) - 2, 4):
            end_of_line = (wps[i + 1].lat, wps[i + 1].lon)
            start_of_next = (wps[i + 2].lat, wps[i + 2].lon)
            assert haversine_m(end_of_line, start_of_next) < plan.line_spacing_m * 1.5

    def test_edge_margin_extends_lines_past_the_polygon(self):
        without = plan_survey(square(200.0), CAM, replace_margin(BASE, 0.0))
        with_margin = plan_survey(square(200.0), CAM, replace_margin(BASE, 25.0))
        assert with_margin.path_length_m > without.path_length_m

    def test_concave_polygon_is_clipped_not_bridged(self):
        """An L-shape must not have lines flying across the missing quadrant."""
        square_plan = plan_survey(square(200.0), CAM, BASE)
        l_plan = plan_survey(l_shape(200.0), CAM, BASE)
        assert l_plan.area_m2 == pytest.approx(0.75 * square_plan.area_m2, rel=0.02)
        assert l_plan.photo_count < square_plan.photo_count


class TestPattern:
    def test_crosshatch_roughly_doubles_the_work(self):
        nadir = plan_survey(square(300.0), CAM, BASE)
        cross = plan_survey(square(300.0), CAM, replace_pattern(BASE, Pattern.CROSSHATCH))
        assert cross.line_count == pytest.approx(2 * nadir.line_count, rel=0.2)
        assert cross.duration_s > 1.5 * nadir.duration_s

    def test_heading_rotates_the_grid(self):
        north = plan_survey(square(300.0), CAM, BASE)
        east = plan_survey(square(300.0), CAM, replace_heading(BASE, 90.0))
        # Same area covered, so comparable effort, but different waypoints.
        assert north.line_count == pytest.approx(east.line_count, abs=1)
        assert (north.waypoints[0].lat, north.waypoints[0].lon) != (
            east.waypoints[0].lat,
            east.waypoints[0].lon,
        )


class TestEstimates:
    def test_duration_includes_turn_overhead(self):
        plan = plan_survey(square(200.0), CAM, BASE)
        pure_flight = plan.path_length_m / BASE.ground_speed_ms
        assert plan.duration_s > pure_flight

    def test_higher_altitude_needs_fewer_photos(self):
        low = plan_survey(square(400.0), CAM, BASE)
        high = plan_survey(square(400.0), CAM, replace_altitude(BASE, 200.0))
        assert high.photo_count < low.photo_count
        assert high.gsd_cm_per_px > low.gsd_cm_per_px

    def test_acreage_conversion(self):
        plan = plan_survey(square(202.34), CAM, BASE)  # ~1 hectare
        assert plan.area_acres == pytest.approx(10.1, rel=0.05)


class TestWarnings:
    def test_nadir_pattern_warns_about_3d_quality(self):
        plan = plan_survey(square(200.0), CAM, BASE)
        assert any("3D" in w for w in plan.warnings)

    def test_crosshatch_does_not_warn_about_3d_quality(self):
        plan = plan_survey(square(200.0), CAM, replace_pattern(BASE, Pattern.CROSSHATCH))
        assert not any("3D" in w for w in plan.warnings)

    def test_outrunning_the_shutter_warns(self):
        fast = SurveyParams(altitude_m=40.0, ground_speed_ms=25.0)
        plan = plan_survey(square(200.0), CAM, fast)
        assert any("outruns the shutter" in w for w in plan.warnings)

    def test_low_overlap_warns(self):
        thin = SurveyParams(altitude_m=100.0, front_overlap=0.4, side_overlap=0.4)
        plan = plan_survey(square(200.0), CAM, thin)
        assert any("Overlap below 60%" in w for w in plan.warnings)


class TestValidation:
    def test_rejects_degenerate_polygon(self):
        with pytest.raises(ValueError, match="at least 3 vertices"):
            plan_survey([(47.0, -122.0), (47.001, -122.0)], CAM, BASE)


# -- helpers ---------------------------------------------------------------
# SurveyParams is frozen, so variants are built by replacement.

def replace_margin(p: SurveyParams, margin: float) -> SurveyParams:
    return SurveyParams(**{**vars(p), "edge_margin_m": margin})


def replace_pattern(p: SurveyParams, pattern: Pattern) -> SurveyParams:
    return SurveyParams(**{**vars(p), "pattern": pattern})


def replace_heading(p: SurveyParams, heading: float) -> SurveyParams:
    return SurveyParams(**{**vars(p), "heading_deg": heading})


def replace_altitude(p: SurveyParams, altitude: float) -> SurveyParams:
    return SurveyParams(**{**vars(p), "altitude_m": altitude})
