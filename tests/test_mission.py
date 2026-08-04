"""Mission construction and verification tests.

Upload/download need a live autopilot and are exercised by tools/sitl_upload.py.
Everything here is the pure logic around them.
"""

from __future__ import annotations

import pytest

from gcs.link.mission import (
    CMD_CAM_TRIGG_DIST,
    CMD_RTL,
    CMD_TAKEOFF,
    CMD_WAYPOINT,
    MissionItem,
    build_mission,
    diff_missions,
)
from gcs.planning import PI_CAMERA_MODULE_3 as CAM
from gcs.planning import Pattern, SurveyParams, plan_survey
from gcs.planning.geo import LocalFrame

HOME = (-35.363262, 149.165237)


def a_plan(side_m: float = 250.0, **kwargs):
    frame = LocalFrame(*HOME)
    h = side_m / 2.0
    polygon = [frame.to_latlon(p) for p in ((-h, -h), (h, -h), (h, h), (-h, h))]
    params = SurveyParams(altitude_m=kwargs.pop("altitude_m", 80.0), **kwargs)
    return plan_survey(polygon, CAM, params)


class TestStructure:
    def test_mission_bookends(self):
        items = build_mission(a_plan(), home=HOME)
        assert items[0].command == CMD_WAYPOINT  # home placeholder
        assert items[1].command == CMD_TAKEOFF
        assert items[2].command == CMD_CAM_TRIGG_DIST
        assert items[-2].command == CMD_CAM_TRIGG_DIST
        assert items[-1].command == CMD_RTL

    def test_sequence_numbers_are_contiguous(self):
        items = build_mission(a_plan(), home=HOME)
        assert [i.seq for i in items] == list(range(len(items)))

    def test_every_survey_waypoint_is_present(self):
        plan = a_plan()
        items = build_mission(plan, home=HOME)
        waypoints = [i for i in items[3:-2] if i.command == CMD_WAYPOINT]
        assert len(waypoints) == len(plan.waypoints)

    def test_crosshatch_produces_a_longer_mission(self):
        nadir = build_mission(a_plan(), home=HOME)
        cross = build_mission(a_plan(pattern=Pattern.CROSSHATCH), home=HOME)
        assert len(cross) > len(nadir)


class TestCameraTrigger:
    def test_trigger_distance_defaults_to_computed_photo_spacing(self):
        plan = a_plan()
        items = build_mission(plan, home=HOME)
        assert items[2].param1 == pytest.approx(plan.photo_spacing_m)

    def test_trigger_distance_can_be_overridden(self):
        items = build_mission(a_plan(), home=HOME, trigger_distance_m=12.5)
        assert items[2].param1 == pytest.approx(12.5)

    def test_trigger_is_switched_off_before_returning(self):
        items = build_mission(a_plan(), home=HOME)
        assert items[-2].param1 == 0.0

    def test_trigger_distance_tracks_altitude(self):
        """Higher flight means a bigger footprint, so photos can be further apart."""
        low = build_mission(a_plan(altitude_m=50.0), home=HOME)[2].param1
        high = build_mission(a_plan(altitude_m=120.0), home=HOME)[2].param1
        assert high > low


class TestAltitude:
    def test_takeoff_climbs_to_survey_altitude(self):
        assert build_mission(a_plan(altitude_m=95.0), home=HOME)[1].z == pytest.approx(95.0)

    def test_takeoff_altitude_can_be_overridden(self):
        items = build_mission(a_plan(altitude_m=95.0), home=HOME, takeoff_alt_m=30.0)
        assert items[1].z == pytest.approx(30.0)
        # Survey waypoints keep the survey altitude regardless.
        assert items[3].z == pytest.approx(95.0)


class TestCoordinateEncoding:
    def test_positions_are_encoded_as_degrees_times_1e7(self):
        items = build_mission(a_plan(), home=HOME)
        assert items[0].x == int(HOME[0] * 1e7)
        assert items[0].lat == pytest.approx(HOME[0], abs=1e-6)

    def test_southern_and_western_hemispheres_survive_the_round_trip(self):
        item = MissionItem(seq=1, command=CMD_WAYPOINT, x=int(-35.5 * 1e7), y=int(-122.5 * 1e7))
        assert item.lat == pytest.approx(-35.5)
        assert item.lon == pytest.approx(-122.5)


class TestValidation:
    def test_empty_plan_is_rejected(self):
        plan = a_plan()
        plan.waypoints = []
        with pytest.raises(ValueError, match="no waypoints"):
            build_mission(plan, home=HOME)


class TestDiff:
    def test_identical_missions_have_no_differences(self):
        items = build_mission(a_plan(), home=HOME)
        assert diff_missions(items, items) == []

    def test_home_slot_difference_is_ignored(self):
        """ArduPilot legitimately overwrites seq 0 with the real home position."""
        sent = build_mission(a_plan(), home=HOME)
        stored = list(sent)
        stored[0] = MissionItem(seq=0, command=CMD_WAYPOINT, x=int(10.0 * 1e7), y=0)
        assert diff_missions(sent, stored) == []

    def test_count_mismatch_is_reported(self):
        items = build_mission(a_plan(), home=HOME)
        problems = diff_missions(items, items[:-1])
        assert any("Item count" in p for p in problems)

    def test_moved_waypoint_is_caught(self):
        sent = build_mission(a_plan(), home=HOME)
        stored = list(sent)
        stored[5] = MissionItem(
            seq=5, command=sent[5].command, x=sent[5].x + 5000, y=sent[5].y, z=sent[5].z
        )
        assert any("position" in p for p in diff_missions(sent, stored))

    def test_altitude_change_is_caught(self):
        sent = build_mission(a_plan(), home=HOME)
        stored = list(sent)
        stored[5] = MissionItem(
            seq=5, command=sent[5].command, x=sent[5].x, y=sent[5].y, z=sent[5].z + 10
        )
        assert any("altitude" in p for p in diff_missions(sent, stored))

    def test_dropped_camera_trigger_is_caught(self):
        """The failure that silently ruins a survey: no photos taken."""
        sent = build_mission(a_plan(), home=HOME)
        stored = list(sent)
        stored[2] = MissionItem(seq=2, command=CMD_CAM_TRIGG_DIST, param1=0.0)
        assert any("param1" in p for p in diff_missions(sent, stored))

    def test_float32_rounding_does_not_trip_the_diff(self):
        sent = build_mission(a_plan(), home=HOME)
        stored = [
            MissionItem(
                seq=i.seq, command=i.command, x=i.x + 1, y=i.y - 1,
                z=i.z + 0.01, param1=i.param1 + 0.001, frame=i.frame,
            )
            for i in sent
        ]
        assert diff_missions(sent, stored) == []
