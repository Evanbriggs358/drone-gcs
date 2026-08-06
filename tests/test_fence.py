"""Geofence tests.

The property that matters is enclosure: every waypoint the aircraft will fly
must sit inside the fence, with room for the overshoot at the end of each line.
A fence that clips a normal survey is worse than none, because it aborts good
missions and teaches the pilot to switch it off.
"""

from __future__ import annotations

import pytest

from gcs.link.fence import (
    DEFAULT_MARGIN_M,
    build_fence,
    build_fence_around_waypoints,
    contains,
    convex_hull,
    diff_fence,
)
from gcs.planning import PI_CAMERA_MODULE_3 as CAM
from gcs.planning import SurveyParams, plan_survey
from gcs.planning.geo import LocalFrame, haversine_m

ORIGIN = (47.6062, -122.3321)


def square(side_m: float) -> list[tuple[float, float]]:
    frame = LocalFrame(*ORIGIN)
    h = side_m / 2.0
    return [frame.to_latlon(p) for p in ((-h, -h), (h, -h), (h, h), (-h, h))]


class TestConvexHull:
    def test_hull_of_a_square_is_its_corners(self):
        hull = convex_hull([(0, 0), (10, 0), (10, 10), (0, 10)])
        assert len(hull) == 4

    def test_interior_points_are_discarded(self):
        hull = convex_hull([(0, 0), (10, 0), (10, 10), (0, 10), (5, 5), (6, 4)])
        assert len(hull) == 4
        assert (5, 5) not in hull

    def test_duplicates_collapse(self):
        hull = convex_hull([(0, 0), (0, 0), (10, 0), (10, 10), (0, 10)])
        assert len(hull) == 4


def l_shape(side_m: float) -> list[tuple[float, float]]:
    """A concave boundary — the shape you draw to route around an obstacle."""
    frame = LocalFrame(*ORIGIN)
    s, h = side_m, side_m / 2.0
    return [frame.to_latlon(p) for p in ((0, 0), (s, 0), (s, h), (h, h), (h, s), (0, s))]


class TestStayInsideTheBoundary:
    """The requirement when a boundary traces trees or wires: every part of the
    flight, turn overshoot included, must remain within the drawn shape."""

    def test_all_waypoints_land_inside_the_drawn_boundary(self):
        polygon = square(300.0)
        plan = plan_survey(
            polygon, CAM, SurveyParams(altitude_m=80.0, ground_speed_ms=8.0)
        )
        fence = build_fence(polygon, max_altitude_m=110.0)

        outside = [w for w in plan.waypoints if not contains(fence, (w.lat, w.lon))]
        assert not outside, f"{len(outside)} waypoints outside the drawn boundary"

    def test_photo_positions_stay_inside_too(self):
        polygon = square(300.0)
        plan = plan_survey(polygon, CAM, SurveyParams(altitude_m=80.0))
        fence = build_fence(polygon, max_altitude_m=110.0)
        assert all(contains(fence, p) for p in plan.photo_points)

    def test_lines_stop_short_by_the_turn_overshoot(self):
        """Waypoints on the edge would still overshoot past it while turning."""
        params = SurveyParams(altitude_m=80.0, ground_speed_ms=8.0)
        assert params.turn_overshoot_m > 10.0

        polygon = square(300.0)
        plan = plan_survey(polygon, CAM, params)
        fence = build_fence(polygon, max_altitude_m=110.0)

        # Every waypoint should clear the boundary by roughly the overshoot.
        clearance = min(
            haversine_m((w.lat, w.lon), v)
            for w in plan.waypoints
            for v in fence.vertices
        )
        assert clearance > 5.0

    def test_faster_flight_is_held_further_from_the_edge(self):
        """Stopping distance goes as the square of speed."""
        slow = SurveyParams(altitude_m=80.0, ground_speed_ms=5.0)
        fast = SurveyParams(altitude_m=80.0, ground_speed_ms=15.0)
        assert fast.turn_overshoot_m > 3 * slow.turn_overshoot_m

    def test_a_fast_survey_of_a_small_area_is_warned_about(self):
        plan = plan_survey(
            square(400.0), CAM,
            SurveyParams(altitude_m=80.0, ground_speed_ms=17.0),
        )
        assert any("turn around" in w for w in plan.warnings)

    def test_concave_boundary_keeps_its_notch(self):
        """A hull would fill in the gap drawn to avoid an obstacle."""
        polygon = l_shape(300.0)
        fence = build_fence(polygon, max_altitude_m=110.0)
        assert fence.vertex_count == len(polygon)

        # The excluded quadrant must remain outside the fence.
        frame = LocalFrame(*ORIGIN)
        excluded = frame.to_latlon((220.0, 220.0))
        assert not contains(fence, excluded)

    def test_flight_avoids_the_excluded_quadrant(self):
        polygon = l_shape(300.0)
        plan = plan_survey(polygon, CAM, SurveyParams(altitude_m=80.0))
        fence = build_fence(polygon, max_altitude_m=110.0)
        assert all(contains(fence, (w.lat, w.lon)) for w in plan.waypoints)

    def test_opting_out_flies_beyond_the_boundary(self):
        """The old behaviour is still available, and demonstrably different."""
        polygon = square(300.0)
        outside_plan = plan_survey(
            polygon, CAM,
            SurveyParams(altitude_m=80.0, keep_inside=False, edge_margin_m=25.0),
        )
        fence = build_fence(polygon, max_altitude_m=110.0)
        assert any(
            not contains(fence, (w.lat, w.lon)) for w in outside_plan.waypoints
        )


class TestEnclosure:
    def test_every_waypoint_falls_inside(self):
        """The whole point. A waypoint outside the fence aborts the mission."""
        plan = plan_survey(square(300.0), CAM, SurveyParams(altitude_m=80.0))
        waypoints = [(w.lat, w.lon) for w in plan.waypoints]
        fence = build_fence_around_waypoints(waypoints, max_altitude_m=110.0)

        outside = [w for w in waypoints if not contains(fence, w)]
        assert not outside, f"{len(outside)} waypoints fell outside the fence"

    def test_the_fence_clears_the_waypoints_by_the_margin(self):
        """Turns overshoot the last waypoint, so the boundary must sit beyond it."""
        plan = plan_survey(square(300.0), CAM, SurveyParams(altitude_m=80.0))
        waypoints = [(w.lat, w.lon) for w in plan.waypoints]
        fence = build_fence_around_waypoints(waypoints, max_altitude_m=110.0, margin_m=40.0)

        # Nearest fence vertex to any waypoint should be roughly a margin away.
        closest = min(
            haversine_m(v, w) for v in fence.vertices for w in waypoints
        )
        assert closest > 20.0

    def test_a_survey_polygon_alone_would_not_enclose_the_flight(self):
        """Why the fence is built from waypoints rather than the drawn shape:
        the flight lines run past the polygon by the edge margin."""
        polygon = square(300.0)
        plan = plan_survey(
            polygon, CAM,
            SurveyParams(altitude_m=80.0, keep_inside=False, edge_margin_m=25.0),
        )
        waypoints = [(w.lat, w.lon) for w in plan.waypoints]

        tight = build_fence(polygon, max_altitude_m=110.0)
        assert any(not contains(tight, w) for w in waypoints)

    def test_crosshatch_is_enclosed_too(self):
        from gcs.planning import Pattern

        plan = plan_survey(
            square(300.0), CAM,
            SurveyParams(altitude_m=80.0, pattern=Pattern.CROSSHATCH),
        )
        waypoints = [(w.lat, w.lon) for w in plan.waypoints]
        fence = build_fence_around_waypoints(waypoints, max_altitude_m=110.0)
        assert all(contains(fence, w) for w in waypoints)


class TestGeometry:
    def test_a_bigger_margin_makes_a_bigger_fence(self):
        waypoints = square(200.0)
        small = build_fence_around_waypoints(waypoints, 100.0, margin_m=10.0)
        large = build_fence_around_waypoints(waypoints, 100.0, margin_m=80.0)
        centre = ORIGIN
        assert max(haversine_m(centre, v) for v in large.vertices) > max(
            haversine_m(centre, v) for v in small.vertices
        )

    def test_a_point_well_outside_is_not_contained(self):
        fence = build_fence_around_waypoints(square(200.0), 100.0, margin_m=20.0)
        frame = LocalFrame(*ORIGIN)
        assert not contains(fence, frame.to_latlon((5000.0, 5000.0)))

    def test_the_centre_is_contained(self):
        fence = build_fence(square(200.0), 100.0)
        assert contains(fence, ORIGIN)

    def test_altitude_ceiling_is_carried(self):
        assert build_fence(square(200.0), 137.0).max_altitude_m == 137.0


class TestValidation:
    def test_too_few_waypoints_is_rejected(self):
        with pytest.raises(ValueError, match="at least 3"):
            build_fence([ORIGIN, (47.61, -122.33)], 100.0)

    def test_collinear_waypoints_are_rejected(self):
        frame = LocalFrame(*ORIGIN)
        line = [frame.to_latlon((x, 0.0)) for x in (0.0, 50.0, 100.0, 150.0)]
        with pytest.raises(ValueError, match="collinear"):
            build_fence_around_waypoints(line, 100.0)


class TestReadback:
    def test_identical_fence_has_no_differences(self):
        fence = build_fence(square(200.0), 100.0)
        assert diff_fence(fence, fence.vertices) == []

    def test_missing_vertex_is_caught(self):
        fence = build_fence(square(200.0), 100.0)
        problems = diff_fence(fence, fence.vertices[:-1])
        assert any("Vertex count" in p for p in problems)

    def test_a_moved_vertex_is_caught(self):
        """A fence stored in the wrong place is a fence around the wrong ground."""
        fence = build_fence(square(200.0), 100.0)
        corrupted = list(fence.vertices)
        corrupted[0] = (corrupted[0][0] + 0.001, corrupted[0][1])
        assert any("Vertex 0" in p for p in diff_fence(fence, corrupted))

    def test_float_rounding_does_not_trip_the_diff(self):
        fence = build_fence(square(200.0), 100.0)
        jittered = [(lat + 1e-8, lon - 1e-8) for lat, lon in fence.vertices]
        assert diff_fence(fence, jittered) == []
