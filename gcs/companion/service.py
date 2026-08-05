"""Companion computer service.

Runs on the Raspberry Pi as a systemd unit, started at boot. Replaces the
previous workflow of SSHing in and launching a script by hand.

Responsibilities:

- Hold the MAVLink connection to the flight controller.
- Capture and geotag a photo whenever the autopilot triggers the camera.
- Expose health, live status, and the captured photos over HTTP so the ground
  station can check readiness before flight and offload afterwards.

Designed for an intermittent link (HARDWARE.md finding 1): the capture loop is
entirely independent of whether the ground station is connected. Photos are
written to the Pi's own storage and served on request. Losing WiFi costs the
live status, never the imagery.
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..link.state import VehicleState, apply_message
from .camera import Camera, open_camera
from .capture import CameraFeedbackTrigger, CaptureService, CaptureSession, DistanceTrigger

#: Where captured flights are stored on the Pi.
PHOTO_ROOT = Path(os.environ.get("DRONE_PHOTO_ROOT", "/home/pi/flights"))

#: MAVLink endpoint for the flight controller. The Pi talks to the Kakute over
#: a serial UART; a TCP endpoint is used when testing against SITL.
FC_ENDPOINT = os.environ.get("DRONE_FC_ENDPOINT", "/dev/serial0")
FC_BAUD = int(os.environ.get("DRONE_FC_BAUD", "921600"))

#: Set when running off-hardware, so a laptop can exercise the service.
USE_FAKE_CAMERA = os.environ.get("DRONE_FAKE_CAMERA", "").lower() in {"1", "true", "yes"}


@dataclass
class LinkStatus:
    connected: bool = False
    endpoint: str = FC_ENDPOINT
    last_message_s: float | None = None
    messages_received: int = 0
    error: str | None = None


class CaptureWorker:
    """Owns the MAVLink connection and the capture loop, on its own thread.

    Kept deliberately separate from the web layer: an HTTP request never blocks
    capture, and the loop keeps running whether or not anything is listening.
    """

    def __init__(self, camera: Camera) -> None:
        self.camera = camera
        self.link_status = LinkStatus()
        self.vehicle = VehicleState()
        self.session: CaptureSession | None = None
        self.service: CaptureService | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._link = None

        # Only one thread may read the MAVLink connection at a time. The capture
        # pump reads continuously; request-driven protocols like mission upload
        # need exclusive access, or the pump consumes the MISSION_REQUEST
        # messages they are waiting for and the transfer hangs until timeout.
        self._io_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="capture", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)

    # -- sessions ----------------------------------------------------------

    def begin_session(self, name: str | None, trigger_distance_m: float | None) -> CaptureSession:
        """Start recording a flight. Any previous session is closed first."""
        with self._lock:
            session = CaptureSession(self.camera, PHOTO_ROOT, name=name)
            trigger = (
                DistanceTrigger(trigger_distance_m)
                if trigger_distance_m
                else CameraFeedbackTrigger()
            )
            self.session = session
            self.service = CaptureService(session, trigger)
            return session

    def end_session(self) -> CaptureSession | None:
        with self._lock:
            finished, self.session, self.service = self.session, None, None
            return finished

    # -- loop --------------------------------------------------------------

    def _connect(self):
        from pymavlink import mavutil

        if FC_ENDPOINT.startswith(("tcp:", "udp:", "tcpin:")):
            return mavutil.mavlink_connection(FC_ENDPOINT)
        return mavutil.mavlink_connection(FC_ENDPOINT, baud=FC_BAUD)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._link = self._connect()
                self._link.wait_heartbeat(timeout=10)
                self.link_status.connected = True
                self.link_status.error = None
                self._pump()
            except Exception as error:  # noqa: BLE001 — reconnect forever
                self.link_status.connected = False
                self.link_status.error = str(error)
                # A serial cable does not usually fail, but if it does the
                # service must recover on its own; nobody can SSH in mid-flight.
                self._stop.wait(3.0)

    @contextmanager
    def exclusive_link(self):
        """Take sole use of the MAVLink connection for a request/response exchange.

        The capture pump releases the lock between messages, so this is granted
        within a message interval. Telemetry and capture pause meanwhile, which
        is acceptable: these exchanges happen on the ground.
        """
        with self._io_lock:
            if self._link is None or not self.link_status.connected:
                raise RuntimeError("no link to the flight controller")
            yield self._link

    def _pump(self) -> None:
        while not self._stop.is_set():
            with self._io_lock:
                msg = self._link.recv_match(blocking=True, timeout=0.5)
            if msg is None:
                continue
            if msg.get_srcSystem() != self._link.target_system:
                continue

            self.link_status.messages_received += 1
            self.link_status.last_message_s = time.monotonic()
            apply_message(self.vehicle, msg)

            service = self.service
            if service is not None:
                service.handle_message(msg)


# -- web layer -------------------------------------------------------------

app = FastAPI(title="drone-gcs companion", docs_url=None, redoc_url=None)
worker = CaptureWorker(open_camera(fake=USE_FAKE_CAMERA))


@app.on_event("startup")
def _startup() -> None:
    PHOTO_ROOT.mkdir(parents=True, exist_ok=True)
    worker.start()


@app.on_event("shutdown")
def _shutdown() -> None:
    worker.stop()


class SessionRequest(BaseModel):
    name: str | None = None
    #: Set to trigger from the companion by distance instead of waiting for the
    #: flight controller. Only for manually flown surveys.
    trigger_distance_m: float | None = None


class MissionRequest(BaseModel):
    """A planned survey, ready to be written to the flight controller.

    The ground station plans; the companion uploads. Only the Pi has a wire to
    the flight controller, so every command reaches it through here.
    """

    #: Survey waypoints as [latitude, longitude] pairs.
    waypoints: list[tuple[float, float]]
    altitude_m: float
    #: Distance between shutter releases, from the planner rather than a guess.
    trigger_distance_m: float
    home: tuple[float, float] | None = None


def _simulated_reasons() -> list[str]:
    """Why this is not a real aircraft, in words a pilot can act on.

    A ground station reporting a healthy GPS lock is worthless — dangerous,
    even — if the healthy GPS belongs to a simulator. Nothing about the numbers
    themselves reveals that, so it has to be stated outright.
    """
    reasons = []
    if USE_FAKE_CAMERA or type(worker.camera).__name__ == "FakeCamera":
        reasons.append("the camera is simulated, not a real Pi Camera")
    endpoint = FC_ENDPOINT.lower()
    if endpoint.startswith(("tcp:", "udp:", "tcpin:")):
        if "127.0.0.1" in endpoint or "localhost" in endpoint:
            reasons.append(
                "the flight controller is a simulator on this computer, not an aircraft"
            )
        else:
            reasons.append("the flight controller is reached over the network, not a serial port")
    return reasons


@app.get("/health")
def health() -> dict:
    """Everything the pre-flight checklist needs to know about the Pi."""
    usage = shutil.disk_usage(PHOTO_ROOT if PHOTO_ROOT.exists() else Path.home())
    reasons = _simulated_reasons()
    return {
        "service": "ok",
        "camera": type(worker.camera).__name__,
        "camera_is_fake": USE_FAKE_CAMERA or type(worker.camera).__name__ == "FakeCamera",
        "simulated": bool(reasons),
        "simulated_reasons": reasons,
        "fc_endpoint": FC_ENDPOINT,
        "free_disk_gb": round(usage.free / 1024**3, 1),
        "total_disk_gb": round(usage.total / 1024**3, 1),
        "link": asdict(worker.link_status),
        "capturing": worker.session is not None,
    }


@app.get("/status")
def status() -> dict:
    """Live vehicle state and capture progress."""
    vehicle = worker.vehicle
    session = worker.session
    return {
        "link": asdict(worker.link_status),
        "vehicle": {
            "armed": vehicle.armed,
            "mode": vehicle.mode,
            "lat": vehicle.lat,
            "lon": vehicle.lon,
            "relative_alt_m": vehicle.relative_alt_m,
            "ground_speed_ms": vehicle.ground_speed_ms,
            "heading_deg": vehicle.heading_deg,
            "gps_fix": vehicle.gps_fix.name,
            "satellites": vehicle.satellites,
            "hdop": vehicle.hdop,
            "battery_v": vehicle.battery_v,
            "battery_remaining_pct": vehicle.battery_remaining_pct,
            "mission_seq": vehicle.mission_current_seq,
        },
        "session": None
        if session is None
        else {
            "name": session.directory.name,
            "captured": session.stats.captured,
            "failed": session.stats.failed,
            "triggers": session.stats.triggers,
            "elapsed_s": round(session.stats.elapsed_s, 1),
            "last_error": session.stats.last_error,
        },
    }


@app.post("/session/start")
def start_session(request: SessionRequest) -> dict:
    session = worker.begin_session(request.name, request.trigger_distance_m)
    return {"name": session.directory.name, "directory": str(session.directory)}


@app.post("/session/stop")
def stop_session() -> dict:
    session = worker.end_session()
    if session is None:
        raise HTTPException(409, "no session running")
    return {
        "name": session.directory.name,
        "captured": session.stats.captured,
        "failed": session.stats.failed,
    }


@app.get("/preflight")
def preflight(mission_waypoints: int = 0, estimated_photos: int = 0) -> dict:
    """Run the launch gate against live vehicle state and the Pi's own health."""
    from ..link.preflight import run_preflight

    usage = shutil.disk_usage(PHOTO_ROOT if PHOTO_ROOT.exists() else Path.home())
    camera_ok = not isinstance(worker.camera, type(None))

    report = run_preflight(
        worker.vehicle,
        mission_waypoint_count=mission_waypoints,
        companion_ready=worker.link_status.connected,
        companion_detail=(
            "Flight controller link up"
            if worker.link_status.connected
            else f"No link to the flight controller: {worker.link_status.error or 'not connected'}"
        ),
        camera_ready=camera_ok,
        camera_detail=f"{type(worker.camera).__name__} ready",
        free_disk_gb=usage.free / 1024**3,
        estimated_photos=estimated_photos,
    )

    reasons = _simulated_reasons()
    return {
        "ready_to_arm": report.ready_to_arm,
        "summary": report.summary(),
        "simulated": bool(reasons),
        "simulated_reasons": reasons,
        "checks": [
            {
                "name": c.name,
                "passed": c.passed,
                "detail": c.detail,
                "severity": c.severity.value,
                "blocking": c.blocks_launch,
            }
            for c in report.checks
        ],
    }


@app.post("/mission/upload")
def upload_mission_endpoint(request: MissionRequest) -> dict:
    """Build an AUTO mission from planned waypoints and write it to the FC.

    Verified by reading it back; a rejected or mismatched upload raises rather
    than reporting a success the aircraft would not honour.
    """
    from ..link.mission import build_mission, diff_missions, download_mission, upload_mission
    from ..planning.grid import SurveyPlan, Waypoint

    waypoints = [
        Waypoint(lat=lat, lon=lon, alt_m=request.altitude_m)
        for lat, lon in request.waypoints
    ]
    plan = SurveyPlan(
        waypoints=waypoints,
        photo_points=[],
        line_count=len(waypoints) // 2,
        path_length_m=0.0,
        duration_s=0.0,
        area_m2=0.0,
        gsd_cm_per_px=0.0,
        photo_spacing_m=request.trigger_distance_m,
        line_spacing_m=0.0,
    )

    items = build_mission(
        plan,
        trigger_distance_m=request.trigger_distance_m,
        home=tuple(request.home) if request.home else None,
    )

    try:
        with worker.exclusive_link() as link:
            upload_mission(link, items)
            stored = download_mission(link)
    except (RuntimeError, TimeoutError) as error:
        raise HTTPException(502, f"upload failed: {error}")

    problems = diff_missions(items, stored)
    if problems:
        raise HTTPException(502, "mission readback did not match: " + "; ".join(problems))

    return {
        "items": len(items),
        "trigger_distance_m": request.trigger_distance_m,
        "verified": True,
    }


@app.get("/sessions")
def list_sessions() -> list[dict]:
    if not PHOTO_ROOT.is_dir():
        return []
    sessions = []
    for directory in sorted(PHOTO_ROOT.iterdir(), reverse=True):
        if not directory.is_dir():
            continue
        images = directory / "images"
        photos = sorted(images.glob("*.jpg")) if images.is_dir() else []
        sessions.append({
            "name": directory.name,
            "photos": len(photos),
            "bytes": sum(p.stat().st_size for p in photos),
            "active": worker.session is not None
            and worker.session.directory.name == directory.name,
        })
    return sessions


@app.get("/sessions/{name}/photos")
def list_photos(name: str) -> list[dict]:
    """Names and sizes, so the ground station can plan and verify an offload."""
    images = _session_dir(name) / "images"
    if not images.is_dir():
        return []
    return [
        {"name": p.name, "bytes": p.stat().st_size}
        for p in sorted(images.glob("*.jpg"))
    ]


@app.get("/sessions/{name}/photos/{filename}")
def get_photo(name: str, filename: str) -> FileResponse:
    path = (_session_dir(name) / "images" / filename).resolve()
    if not str(path).startswith(str(_session_dir(name).resolve())):
        raise HTTPException(400, "path escapes the session directory")
    if not path.is_file():
        raise HTTPException(404, "no such photo")
    return FileResponse(path)


@app.get("/sessions/{name}/manifest")
def get_manifest(name: str) -> FileResponse:
    path = _session_dir(name) / "captures.jsonl"
    if not path.is_file():
        raise HTTPException(404, "no manifest for this session")
    return FileResponse(path, media_type="application/x-ndjson")


def _session_dir(name: str) -> Path:
    directory = (PHOTO_ROOT / name).resolve()
    if not str(directory).startswith(str(PHOTO_ROOT.resolve())):
        raise HTTPException(400, "invalid session name")
    if not directory.is_dir():
        raise HTTPException(404, "no such session")
    return directory
