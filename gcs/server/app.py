"""Local web app: browse reconstructions, view maps and 3D models.

Serves a single-page frontend and a small JSON API over the flight data
directory. Runs on the operator's laptop; nothing here is exposed to a network
beyond localhost by default.

    python -m gcs.server            # then open http://127.0.0.1:8000
"""

from __future__ import annotations

import os
import statistics
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

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
        "products": {
            "orthophoto": relative(products.orthophoto),
            "orthophoto_preview": relative(products.orthophoto_preview),
            "textured_model": relative(products.textured_model),
            "point_cloud": relative(products.point_cloud),
            "dsm": relative(products.dsm),
            "report": relative(products.report),
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


@app.get("/files/{project}/{path:path}")
def get_file(project: str, path: str) -> FileResponse:
    """Serve a product file out of a project directory."""
    return FileResponse(_safe_path(project, path))


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
