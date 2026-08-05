"""Local web app: browse reconstructions, view maps and 3D models.

Serves a single-page frontend and a small JSON API over the flight data
directory. Runs on the operator's laptop; nothing here is exposed to a network
beyond localhost by default.

    python -m gcs.server            # then open http://127.0.0.1:8000
"""

from __future__ import annotations

import os
import statistics
from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..planning import (
    CAMERAS,
    DEADCAT_7IN,
    PI_CAMERA_MODULE_3,
    Battery,
    Pattern,
    SurveyParams,
    plan_survey,
)
from ..offload import CompanionClient, CompanionError
from ..processing.odm import find_products

#: Root holding one folder per reconstruction. Override with DRONE_DATA_DIR.
DATA_ROOT = Path(os.environ.get("DRONE_DATA_DIR", r"C:\DroneData\samples"))

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="drone-gcs", docs_url=None, redoc_url=None)


def _safe_path(project: str, relative: str) -> Path:
    """Resolve a path inside a project, refusing anything that escapes it.

    Serving arbitrary user-supplied paths from disk is how a local viewer turns
    into a file-disclosure bug, so every request is resolved and checked to be
    within the project directory.
    """
    project_dir = (DATA_ROOT / project).resolve()
    if not str(project_dir).startswith(str(DATA_ROOT.resolve())):
        raise HTTPException(400, "invalid project")

    target = (project_dir / relative).resolve()
    if not str(target).startswith(str(project_dir)):
        raise HTTPException(400, "path escapes project directory")
    if not target.is_file():
        raise HTTPException(404, f"not found: {relative}")
    return target


def _describe(project_dir: Path) -> dict:
    products = find_products(project_dir)
    images_dir = project_dir / "images"
    photo_count = (
        len([p for p in images_dir.iterdir() if p.suffix.lower() in {".jpg", ".jpeg"}])
        if images_dir.is_dir()
        else 0
    )

    def relative(path: Path | None) -> str | None:
        return str(path.relative_to(project_dir)).replace("\\", "/") if path else None

    return {
        "name": project_dir.name,
        "photos": photo_count,
        "has_map": products.has_map,
        "has_3d_model": products.has_3d_model,
        # Downloadable data products.
        "products": {
            "orthophoto": relative(products.orthophoto),
            "textured_model": relative(products.textured_model),
            "point_cloud": relative(products.point_cloud),
            "dsm": relative(products.dsm),
            "report": relative(products.report),
        },
        # Rendered map layers, kept separate from products so the download list
        # stays data rather than pictures of data.
        "layers": {
            "ortho": relative(products.orthophoto_preview),
            "elevation": relative(products.dsm_preview),
            "elevation_legend": relative(products.dsm_legend),
            "overlap": relative(products.overlap_preview),
            "overlap_legend": relative(products.overlap_legend),
            "cameras": relative(products.camera_positions_preview),
        },
    }


@app.get("/api/projects")
def list_projects() -> list[dict]:
    """Every reconstruction found under the data root, newest first."""
    if not DATA_ROOT.is_dir():
        return []
    projects = [
        d for d in DATA_ROOT.iterdir()
        if d.is_dir() and ((d / "images").is_dir() or (d / "odm_orthophoto").is_dir())
    ]
    projects.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    return [_describe(d) for d in projects]


@app.get("/api/projects/{project}")
def get_project(project: str) -> dict:
    project_dir = (DATA_ROOT / project).resolve()
    if not project_dir.is_dir():
        raise HTTPException(404, "no such project")
    return _describe(project_dir)


@app.get("/api/projects/{project}/stats")
def get_stats(project: str) -> dict:
    """Survey statistics read from the photos' own geotags."""
    from ..companion.geotag import read_geotag
    from ..planning.geo import haversine_m

    images_dir = (DATA_ROOT / project).resolve() / "images"
    if not images_dir.is_dir():
        raise HTTPException(404, "no images folder")

    photos = sorted(
        p for p in images_dir.iterdir() if p.suffix.lower() in {".jpg", ".jpeg"}
    )
    tags = [t for t in (read_geotag(p) for p in photos) if t is not None]
    if not tags:
        return {"photos": len(photos), "geotagged": 0}

    lats = [t.lat for t in tags]
    lons = [t.lon for t in tags]
    alts = [t.altitude_m for t in tags]
    steps = [
        haversine_m((a.lat, a.lon), (b.lat, b.lon)) for a, b in zip(tags, tags[1:])
    ]

    return {
        "photos": len(photos),
        "geotagged": len(tags),
        "with_heading": sum(1 for t in tags if t.yaw_deg is not None),
        "centre": {"lat": sum(lats) / len(lats), "lon": sum(lons) / len(lons)},
        "bounds": {
            "north": max(lats), "south": min(lats),
            "east": max(lons), "west": min(lons),
        },
        "altitude": {
            "min": min(alts), "max": max(alts), "mean": sum(alts) / len(alts)
        },
        "extent_m": {
            "width": haversine_m(
                (sum(lats) / len(lats), min(lons)), (sum(lats) / len(lats), max(lons))
            ),
            "height": haversine_m(
                (min(lats), sum(lons) / len(lons)), (max(lats), sum(lons) / len(lons))
            ),
        },
        "spacing_m": {
            # statistics.median, not the upper-middle element, so this agrees
            # with tools/inspect_photos.py on the same data.
            "median": statistics.median(steps) if steps else None,
            "min": min(steps) if steps else None,
            "max": max(steps) if steps else None,
        },
        "camera_positions": [
            {"lat": t.lat, "lon": t.lon, "yaw": t.yaw_deg} for t in tags
        ],
    }


# -- mission planning ------------------------------------------------------


class PlanRequest(BaseModel):
    """A survey area plus the parameters that turn it into a flight."""

    #: Polygon vertices as [latitude, longitude] pairs, unclosed.
    polygon: list[tuple[float, float]] = Field(min_length=3)
    altitude_m: float = Field(gt=0, le=500)
    front_overlap: float = Field(default=0.70, ge=0, lt=1)
    side_overlap: float = Field(default=0.70, ge=0, lt=1)
    ground_speed_ms: float = Field(default=8.0, gt=0, le=30)
    heading_deg: float = 0.0
    pattern: str = "nadir"
    camera: str | None = None

    # -- aircraft, for endurance analysis --
    all_up_weight_kg: float = Field(default=1.6, gt=0.2, le=25)
    battery_capacity_mah: float = Field(default=5000, gt=0)
    battery_cells: int = Field(default=6, ge=1, le=14)
    #: A timed hover at real all-up weight. Scales the endurance model to
    #: measured reality; without it the numbers are estimates from component
    #: figures and carry real uncertainty.
    measured_hover_min: float | None = Field(default=None, gt=0, le=120)


@app.get("/api/cameras")
def list_cameras() -> list[dict]:
    return [
        {
            "name": camera.name,
            "megapixels": round(camera.image_width_px * camera.image_height_px / 1e6, 1),
            "hfov_deg": round(camera.hfov_deg, 1),
        }
        for camera in CAMERAS.values()
    ]


@app.post("/api/plan")
def make_plan(request: PlanRequest) -> dict:
    """Turn a drawn polygon into a flight plan, with the numbers that matter.

    Called on every parameter change, so the operator sees ground sample
    distance, photo count, and flight time update as they drag a slider rather
    than discovering them after upload.
    """
    camera = CAMERAS.get(request.camera or "", PI_CAMERA_MODULE_3)

    try:
        pattern = Pattern(request.pattern)
    except ValueError:
        raise HTTPException(400, f"unknown pattern {request.pattern!r}")

    params = SurveyParams(
        altitude_m=request.altitude_m,
        front_overlap=request.front_overlap,
        side_overlap=request.side_overlap,
        ground_speed_ms=request.ground_speed_ms,
        heading_deg=request.heading_deg,
        pattern=pattern,
    )

    airframe = replace(DEADCAT_7IN, all_up_weight_kg=request.all_up_weight_kg)
    battery = Battery(
        capacity_mah=request.battery_capacity_mah, cells=request.battery_cells
    )
    if request.measured_hover_min:
        airframe = airframe.calibrated_to(request.measured_hover_min, battery)

    try:
        plan = plan_survey(
            [tuple(p) for p in request.polygon],
            camera,
            params,
            airframe=airframe,
            battery=battery,
        )
    except ValueError as error:
        raise HTTPException(400, str(error))

    return {
        "waypoints": [[w.lat, w.lon] for w in plan.waypoints],
        "photo_points": [[lat, lon] for lat, lon in plan.photo_points],
        "stats": {
            "area_acres": round(plan.area_acres, 2),
            "gsd_cm_per_px": round(plan.gsd_cm_per_px, 2),
            "line_count": plan.line_count,
            "photo_count": plan.photo_count,
            "path_length_m": round(plan.path_length_m),
            "duration_min": round(plan.duration_min, 1),
            "photo_spacing_m": round(plan.photo_spacing_m, 1),
            "line_spacing_m": round(plan.line_spacing_m, 1),
            "max_ground_speed_ms": round(
                camera.max_ground_speed_ms(request.altitude_m, request.front_overlap), 1
            ),
            "endurance_min": round(plan.endurance_min, 1),
            "batteries_needed": round(plan.batteries_needed, 2),
            "best_survey_speed_ms": round(plan.best_survey_speed_ms, 1),
            "calibrated": request.measured_hover_min is not None,
        },
        "warnings": plan.warnings,
    }


# -- companion computer ----------------------------------------------------
#
# The laptop has no wire to the flight controller — only WiFi to the Pi. Every
# command and every scrap of telemetry therefore travels through the companion,
# and these endpoints are a thin proxy onto it. Keeping that boundary explicit
# stops the ground station quietly assuming a link it does not have.

_companion: dict[str, CompanionClient | None] = {"client": None}

if os.environ.get("DRONE_COMPANION_URL"):
    _companion["client"] = CompanionClient(os.environ["DRONE_COMPANION_URL"])


class CompanionConnect(BaseModel):
    url: str | None = None


class MissionUpload(BaseModel):
    waypoints: list[tuple[float, float]]
    altitude_m: float
    trigger_distance_m: float
    home: tuple[float, float] | None = None


def _require_companion() -> CompanionClient:
    client = _companion["client"]
    if client is None:
        raise HTTPException(503, "not connected to the companion computer")
    return client


@app.post("/api/companion/connect")
def companion_connect(request: CompanionConnect) -> dict:
    """Attach to the Pi, by address or by looking for it on the network."""
    client = CompanionClient(request.url) if request.url else CompanionClient.discover()
    if client is None:
        raise HTTPException(
            404,
            "Could not find the companion computer. Check the Pi is powered and "
            "on the same network, or give its address directly.",
        )
    try:
        health = client.health()
    except CompanionError as error:
        raise HTTPException(502, f"companion unreachable: {error}")

    _companion["client"] = client
    return {"url": client.base_url, "health": health}


@app.post("/api/companion/disconnect")
def companion_disconnect() -> dict:
    _companion["client"] = None
    return {"connected": False}


@app.get("/api/companion/status")
def companion_status() -> dict:
    """Live vehicle state. Polled by the map; a dropout is expected, not fatal."""
    client = _companion["client"]
    if client is None:
        return {"connected": False}
    try:
        status = client.status()
    except CompanionError as error:
        # A lost link is normal out at the far end of a survey. Report it
        # plainly rather than raising; the aircraft is flying the mission itself.
        return {"connected": False, "error": str(error), "url": client.base_url}
    return {"connected": True, "url": client.base_url, **status}


@app.get("/api/companion/preflight")
def companion_preflight(mission_waypoints: int = 0, estimated_photos: int = 0) -> dict:
    client = _require_companion()
    try:
        return client.preflight(mission_waypoints, estimated_photos)
    except CompanionError as error:
        raise HTTPException(502, str(error))


@app.post("/api/companion/mission")
def companion_mission(request: MissionUpload) -> dict:
    client = _require_companion()
    try:
        return client.upload_mission(
            [list(w) for w in request.waypoints],
            request.altitude_m,
            request.trigger_distance_m,
            list(request.home) if request.home else None,
        )
    except CompanionError as error:
        raise HTTPException(502, str(error))


@app.get("/files/{project}/{path:path}")
def get_file(project: str, path: str) -> FileResponse:
    """Serve a product file out of a project directory."""
    return FileResponse(_safe_path(project, path))


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
