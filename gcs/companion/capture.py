"""Capture service: take a photo when the flight controller says to, and geotag it.

The flight controller owns the survey (SPEC §2.1). It flies the grid and decides
when photos happen, via ``CAM_TRIGG_DIST`` in the uploaded mission. This service
listens for those decisions and does the actual capturing.

Trigger sources are pluggable:

- :class:`CameraFeedbackTrigger` — ArduPilot announces each trigger in a
  ``CAMERA_FEEDBACK`` message that **carries the position and attitude at the
  moment of the trigger**. The geotag therefore comes from the trigger event
  itself rather than a separate position query made afterwards, which is
  strictly more accurate than polling. No extra wiring.
- :class:`DistanceTrigger` — the companion computes distance travelled and fires
  independently, as the previous ``capture_daemon.py`` did. Kept as a fallback
  for a flight controller not configured to trigger, and because it is the only
  option if the mission is flown manually.

A capture failure never aborts a mission: it is counted, logged, and the next
trigger is handled normally. Losing one frame costs some overlap; aborting costs
the whole flight.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from ..planning.geo import haversine_m
from .camera import Camera
from .geotag import GeoTag, write_geotag


@dataclass(frozen=True)
class TriggerEvent:
    """A decision to capture, with the vehicle state at that instant."""

    lat: float
    lon: float
    #: Metres above mean sea level — what goes into EXIF.
    alt_msl_m: float
    #: Metres above the launch point, kept for flight review.
    alt_rel_m: float | None = None
    yaw_deg: float | None = None
    roll_deg: float | None = None
    pitch_deg: float | None = None
    #: The autopilot's own image counter, where it provides one.
    image_index: int | None = None
    source: str = "unknown"


def _yaw_from_quaternion(q) -> float | None:
    """Compass heading in degrees from a MAVLink attitude quaternion [w,x,y,z]."""
    from math import atan2, degrees

    if q is None or len(q) < 4:
        return None
    w, x, y, z = (float(v) for v in q[:4])
    if w == 0.0 and x == 0.0 and y == 0.0 and z == 0.0:
        return None
    yaw = atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z))
    return degrees(yaw) % 360.0


class TriggerSource(Protocol):
    """Turns a stream of MAVLink messages into capture decisions."""

    def feed(self, msg) -> TriggerEvent | None:
        """Return a :class:`TriggerEvent` if this message means "capture now"."""


class CameraFeedbackTrigger:
    """Fires when the autopilot reports having triggered the camera.

    Requires ``CAM1_TYPE`` set so the autopilot has a camera backend, and
    ``CAM_TRIGG_DIST`` set by the mission.

    Handles **both** announcement messages, because which one an aircraft emits
    depends on its firmware version and camera backend:

    - ``CAMERA_FEEDBACK`` — ArduPilot's own message, with Euler attitude.
    - ``CAMERA_IMAGE_CAPTURED`` — the standard MAVLink message used by newer
      firmware, with attitude as a quaternion and altitudes in millimetres.

    Supporting only one would work on the bench and then silently capture
    nothing on an aircraft running the other.
    """

    source = "camera_feedback"

    def feed(self, msg) -> TriggerEvent | None:
        kind = msg.get_type()

        if kind == "CAMERA_FEEDBACK":
            return TriggerEvent(
                # Note the field is `lng` here, not `lon` as elsewhere.
                lat=msg.lat / 1e7,
                lon=msg.lng / 1e7,
                alt_msl_m=float(msg.alt_msl),
                alt_rel_m=float(msg.alt_rel),
                yaw_deg=float(msg.yaw) % 360.0,
                roll_deg=float(msg.roll),
                pitch_deg=float(msg.pitch),
                image_index=int(msg.img_idx),
                source=self.source,
            )

        if kind == "CAMERA_IMAGE_CAPTURED":
            # capture_result < 0 means the camera reported a failed capture.
            if getattr(msg, "capture_result", 1) < 0:
                return None
            return TriggerEvent(
                lat=msg.lat / 1e7,
                lon=msg.lon / 1e7,
                alt_msl_m=msg.alt / 1000.0,
                alt_rel_m=msg.relative_alt / 1000.0,
                yaw_deg=_yaw_from_quaternion(msg.q),
                image_index=int(msg.image_index),
                source="camera_image_captured",
            )

        return None


class DistanceTrigger:
    """Fires every ``interval_m`` of travel, computed on the companion.

    Position and attitude are accumulated from the telemetry stream, so unlike
    :class:`CameraFeedbackTrigger` the geotag is assembled from the most recent
    messages rather than from a single event. Good enough at survey speeds, but
    less exact.
    """

    source = "distance"

    def __init__(self, interval_m: float) -> None:
        if interval_m <= 0:
            raise ValueError(f"interval_m must be positive, got {interval_m}")
        self.interval_m = interval_m
        self._last_fix: tuple[float, float] | None = None
        self._yaw: float | None = None

    def feed(self, msg) -> TriggerEvent | None:
        kind = msg.get_type()

        if kind == "ATTITUDE":
            from math import degrees

            self._yaw = degrees(msg.yaw) % 360.0
            return None

        if kind != "GLOBAL_POSITION_INT":
            return None

        position = (msg.lat / 1e7, msg.lon / 1e7)

        if self._last_fix is not None:
            if haversine_m(self._last_fix, position) < self.interval_m:
                return None

        self._last_fix = position
        return TriggerEvent(
            lat=position[0],
            lon=position[1],
            alt_msl_m=msg.alt / 1000.0,
            alt_rel_m=msg.relative_alt / 1000.0,
            yaw_deg=self._yaw,
            source=self.source,
        )


@dataclass
class CaptureStats:
    captured: int = 0
    failed: int = 0
    triggers: int = 0
    started_at: float = field(default_factory=time.monotonic)
    last_error: str | None = None

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.started_at


class CaptureSession:
    """One flight's worth of captures, written to a timestamped folder.

    Alongside the images it writes ``captures.jsonl`` — one record per trigger,
    including failures. That manifest is what the ground station uses afterwards
    to check coverage and to notice photos the autopilot asked for but which
    never made it to disk.
    """

    def __init__(
        self,
        camera: Camera,
        output_root: str | Path,
        name: str | None = None,
        filename_prefix: str = "IMG",
    ) -> None:
        self.camera = camera
        stamp = name or datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
        self.directory = Path(output_root) / stamp
        self.images_dir = self.directory / "images"
        self.images_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.directory / "captures.jsonl"
        self.filename_prefix = filename_prefix
        self.stats = CaptureStats()

    def capture(self, event: TriggerEvent) -> Path | None:
        """Take one photo for a trigger. Returns the path, or None on failure."""
        self.stats.triggers += 1
        sequence = self.stats.triggers
        path = self.images_dir / f"{self.filename_prefix}_{sequence:04d}.jpg"
        captured_at = datetime.now(timezone.utc)

        try:
            self.camera.capture(path)
            write_geotag(
                path,
                GeoTag(
                    lat=event.lat,
                    lon=event.lon,
                    altitude_m=event.alt_msl_m,
                    yaw_deg=event.yaw_deg,
                    timestamp=captured_at,
                    relative_alt_m=event.alt_rel_m,
                ),
            )
        except Exception as error:  # noqa: BLE001 — one bad frame must not end a flight
            self.stats.failed += 1
            self.stats.last_error = str(error)
            self._record(sequence, event, captured_at, None, str(error))
            return None

        self.stats.captured += 1
        self._record(sequence, event, captured_at, path, None)
        return path

    def _record(
        self,
        sequence: int,
        event: TriggerEvent,
        captured_at: datetime,
        path: Path | None,
        error: str | None,
    ) -> None:
        record = {
            "seq": sequence,
            "time": captured_at.isoformat(),
            "file": path.name if path else None,
            "error": error,
            **asdict(event),
        }
        with self.manifest_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def close(self) -> None:
        self.camera.close()


class CaptureService:
    """Feeds MAVLink messages to a trigger source and captures on demand."""

    def __init__(self, session: CaptureSession, trigger: TriggerSource) -> None:
        self.session = session
        self.trigger = trigger

    def handle_message(self, msg) -> Path | None:
        """Process one MAVLink message. Returns a path if a photo was taken."""
        event = self.trigger.feed(msg)
        return None if event is None else self.session.capture(event)

    def run(self, link, stop_after_s: float | None = None, on_capture=None) -> CaptureStats:
        """Read from a MAVLink connection until stopped.

        ``link`` is a pymavlink connection. Messages from other systems sharing
        the link are ignored — a second ground station's heartbeat is not the
        aircraft's (see gcs/link/state.py for the bug that taught us this).
        """
        deadline = None if stop_after_s is None else time.monotonic() + stop_after_s

        while deadline is None or time.monotonic() < deadline:
            msg = link.recv_match(blocking=True, timeout=1.0)
            if msg is None:
                continue
            if msg.get_srcSystem() != link.target_system:
                continue

            path = self.handle_message(msg)
            if path is not None and on_capture is not None:
                on_capture(path, self.session.stats)

        return self.session.stats
