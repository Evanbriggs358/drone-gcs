"""Capture service tests.

Real pymavlink messages and real JPEGs on disk, so the geotag round trip is
exercised end to end without hardware.
"""

from __future__ import annotations

import json
from math import radians

import pytest
from pymavlink.dialects.v20 import ardupilotmega as m

from gcs.companion.camera import FakeCamera
from gcs.companion.capture import (
    CameraFeedbackTrigger,
    CaptureService,
    CaptureSession,
    DistanceTrigger,
    TriggerEvent,
)
from gcs.companion.geotag import read_geotag

SEATTLE = (47.6062, -122.3321)


def camera_feedback(lat=47.6062, lon=-122.3321, alt_msl=590.0, alt_rel=80.0, yaw=137.0, idx=1):
    return m.MAVLink_camera_feedback_message(
        time_usec=0, target_system=1, cam_idx=0, img_idx=idx,
        lat=int(lat * 1e7), lng=int(lon * 1e7),
        alt_msl=alt_msl, alt_rel=alt_rel,
        roll=1.5, pitch=-2.0, yaw=yaw, foc_len=4.74, flags=0, completed_captures=idx,
    )


def global_position(lat=47.6062, lon=-122.3321, alt_m=590.0, rel_m=80.0):
    return m.MAVLink_global_position_int_message(
        time_boot_ms=0, lat=int(lat * 1e7), lon=int(lon * 1e7),
        alt=int(alt_m * 1000), relative_alt=int(rel_m * 1000),
        vx=0, vy=0, vz=0, hdg=0,
    )


def attitude(yaw_deg=90.0):
    return m.MAVLink_attitude_message(
        time_boot_ms=0, roll=0.0, pitch=0.0, yaw=radians(yaw_deg),
        rollspeed=0, pitchspeed=0, yawspeed=0,
    )


def offset_north(lat, lon, metres):
    return lat + metres / 111_320.0, lon


@pytest.fixture
def session(tmp_path):
    return CaptureSession(FakeCamera(), tmp_path, name="flight")


class TestCameraFeedbackTrigger:
    def test_fires_on_camera_feedback(self):
        event = CameraFeedbackTrigger().feed(camera_feedback())
        assert event is not None
        assert event.lat == pytest.approx(47.6062, abs=1e-6)
        assert event.lon == pytest.approx(-122.3321, abs=1e-6)
        assert event.alt_msl_m == pytest.approx(590.0)
        assert event.alt_rel_m == pytest.approx(80.0)
        assert event.image_index == 1

    def test_reads_lng_not_lon(self):
        """CAMERA_FEEDBACK spells longitude `lng`; most other messages use `lon`."""
        event = CameraFeedbackTrigger().feed(camera_feedback(lon=18.873))
        assert event.lon == pytest.approx(18.873, abs=1e-6)

    def test_yaw_wraps_into_compass_range(self):
        assert CameraFeedbackTrigger().feed(camera_feedback(yaw=-90.0)).yaw_deg == pytest.approx(270.0)

    def test_ignores_other_messages(self):
        trigger = CameraFeedbackTrigger()
        assert trigger.feed(global_position()) is None
        assert trigger.feed(attitude()) is None


def camera_image_captured(lat=47.6062, lon=-122.3321, alt_mm=590_000,
                          rel_mm=80_000, yaw_deg=137.0, idx=1, result=1):
    from math import cos, radians, sin

    half = radians(yaw_deg) / 2.0
    return m.MAVLink_camera_image_captured_message(
        time_boot_ms=0, time_utc=0, camera_id=0,
        lat=int(lat * 1e7), lon=int(lon * 1e7),
        alt=alt_mm, relative_alt=rel_mm,
        q=[cos(half), 0.0, 0.0, sin(half)],
        image_index=idx, capture_result=result, file_url=b"",
    )


class TestCameraImageCaptured:
    """Newer firmware announces captures with the standard MAVLink message
    instead of ArduPilot's CAMERA_FEEDBACK. Supporting only one would work on
    the bench and silently capture nothing on an aircraft running the other."""

    def test_fires_on_camera_image_captured(self):
        event = CameraFeedbackTrigger().feed(camera_image_captured())
        assert event is not None
        assert event.lat == pytest.approx(47.6062, abs=1e-6)
        assert event.lon == pytest.approx(-122.3321, abs=1e-6)
        assert event.source == "camera_image_captured"

    def test_millimetre_altitudes_are_converted(self):
        event = CameraFeedbackTrigger().feed(camera_image_captured())
        assert event.alt_msl_m == pytest.approx(590.0)
        assert event.alt_rel_m == pytest.approx(80.0)

    @pytest.mark.parametrize("yaw", [0.0, 90.0, 137.0, 270.0, 359.0])
    def test_yaw_recovered_from_quaternion(self, yaw):
        event = CameraFeedbackTrigger().feed(camera_image_captured(yaw_deg=yaw))
        assert event.yaw_deg == pytest.approx(yaw, abs=0.01)

    def test_zero_quaternion_yields_no_heading(self):
        msg = camera_image_captured()
        msg.q = [0.0, 0.0, 0.0, 0.0]
        assert CameraFeedbackTrigger().feed(msg).yaw_deg is None

    def test_failed_capture_does_not_trigger(self):
        """A camera that reports failure must not produce a geotagged file."""
        assert CameraFeedbackTrigger().feed(camera_image_captured(result=-1)) is None

    def test_both_message_types_work_through_one_trigger(self, tmp_path):
        session = CaptureSession(FakeCamera(), tmp_path, name="flight")
        service = CaptureService(session, CameraFeedbackTrigger())
        service.handle_message(camera_feedback(idx=1))
        service.handle_message(camera_image_captured(idx=2))
        assert session.stats.captured == 2


class TestDistanceTrigger:
    def test_first_fix_fires_immediately(self):
        assert DistanceTrigger(20.0).feed(global_position()) is not None

    def test_does_not_fire_before_the_interval(self):
        trigger = DistanceTrigger(20.0)
        trigger.feed(global_position(*SEATTLE))
        near = offset_north(*SEATTLE, 5.0)
        assert trigger.feed(global_position(*near)) is None

    def test_fires_once_the_interval_is_covered(self):
        trigger = DistanceTrigger(20.0)
        trigger.feed(global_position(*SEATTLE))
        far = offset_north(*SEATTLE, 25.0)
        assert trigger.feed(global_position(*far)) is not None

    def test_distance_is_measured_from_the_last_photo(self):
        """Two 15 m hops should fire once, not zero times and not twice."""
        trigger = DistanceTrigger(20.0)
        trigger.feed(global_position(*SEATTLE))
        assert trigger.feed(global_position(*offset_north(*SEATTLE, 15.0))) is None
        assert trigger.feed(global_position(*offset_north(*SEATTLE, 30.0))) is not None

    def test_picks_up_yaw_from_attitude(self):
        trigger = DistanceTrigger(20.0)
        trigger.feed(attitude(yaw_deg=215.0))
        assert trigger.feed(global_position()).yaw_deg == pytest.approx(215.0)

    def test_rejects_nonpositive_interval(self):
        with pytest.raises(ValueError, match="interval_m"):
            DistanceTrigger(0)


class TestCaptureSession:
    def test_capture_writes_a_geotagged_photo(self, session):
        path = session.capture(
            TriggerEvent(lat=47.6062, lon=-122.3321, alt_msl_m=590.0, yaw_deg=137.0)
        )
        assert path is not None and path.exists()

        tag = read_geotag(path)
        assert tag.lat == pytest.approx(47.6062, abs=1e-6)
        assert tag.altitude_m == pytest.approx(590.0, abs=0.01)
        assert tag.yaw_deg == pytest.approx(137.0, abs=0.01)

    def test_files_are_numbered_in_order(self, session):
        paths = [
            session.capture(TriggerEvent(lat=47.0 + i * 1e-4, lon=-122.0, alt_msl_m=590.0))
            for i in range(3)
        ]
        assert [p.name for p in paths] == ["IMG_0001.jpg", "IMG_0002.jpg", "IMG_0003.jpg"]

    def test_stats_track_captures(self, session):
        for i in range(4):
            session.capture(TriggerEvent(lat=47.0, lon=-122.0, alt_msl_m=590.0))
        assert session.stats.captured == 4
        assert session.stats.failed == 0
        assert session.stats.triggers == 4


class TestFailureHandling:
    def test_a_failed_capture_does_not_stop_the_session(self, tmp_path):
        session = CaptureSession(FakeCamera(fail_every=2), tmp_path, name="flight")
        results = [
            session.capture(TriggerEvent(lat=47.0, lon=-122.0, alt_msl_m=590.0))
            for _ in range(4)
        ]
        assert [r is not None for r in results] == [True, False, True, False]
        assert session.stats.captured == 2
        assert session.stats.failed == 2
        assert "simulated camera failure" in session.stats.last_error

    def test_failures_are_recorded_in_the_manifest(self, tmp_path):
        session = CaptureSession(FakeCamera(fail_every=1), tmp_path, name="flight")
        session.capture(TriggerEvent(lat=47.0, lon=-122.0, alt_msl_m=590.0))

        records = [json.loads(line) for line in session.manifest_path.read_text().splitlines()]
        assert len(records) == 1
        assert records[0]["file"] is None
        assert "simulated camera failure" in records[0]["error"]


class TestManifest:
    def test_one_record_per_trigger(self, session):
        for _ in range(3):
            session.capture(TriggerEvent(lat=47.0, lon=-122.0, alt_msl_m=590.0))
        records = [json.loads(line) for line in session.manifest_path.read_text().splitlines()]
        assert len(records) == 3
        assert [r["seq"] for r in records] == [1, 2, 3]

    def test_record_carries_position_and_source(self, session):
        session.capture(
            TriggerEvent(
                lat=47.6062, lon=-122.3321, alt_msl_m=590.0,
                yaw_deg=42.0, source="camera_feedback",
            )
        )
        record = json.loads(session.manifest_path.read_text().splitlines()[0])
        assert record["lat"] == pytest.approx(47.6062)
        assert record["source"] == "camera_feedback"
        assert record["file"] == "IMG_0001.jpg"


class TestCaptureService:
    def test_captures_on_camera_feedback(self, session):
        service = CaptureService(session, CameraFeedbackTrigger())
        assert service.handle_message(global_position()) is None
        assert service.handle_message(camera_feedback()) is not None
        assert session.stats.captured == 1

    def test_a_full_pass_produces_one_photo_per_trigger(self, session):
        service = CaptureService(session, CameraFeedbackTrigger())
        for i in range(1, 6):
            service.handle_message(camera_feedback(lat=47.0 + i * 1e-4, idx=i))

        assert session.stats.captured == 5
        photos = sorted(session.images_dir.iterdir())
        assert len(photos) == 5
        # Every photo must carry a distinct position, or the survey is useless.
        latitudes = {round(read_geotag(p).lat, 6) for p in photos}
        assert len(latitudes) == 5

    def test_distance_trigger_drives_the_same_service(self, session):
        service = CaptureService(session, DistanceTrigger(20.0))
        service.handle_message(global_position(*SEATTLE))
        service.handle_message(global_position(*offset_north(*SEATTLE, 5.0)))
        service.handle_message(global_position(*offset_north(*SEATTLE, 45.0)))
        assert session.stats.captured == 2
