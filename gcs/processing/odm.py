"""Running OpenDroneMap reconstructions.

Wraps the ``opendronemap/odm`` container so the app can start a job, follow its
progress, and find the products afterwards — instead of the operator composing
docker commands and hunting through output folders.

ODM has no progress API. It reports stage transitions on stdout, so progress is
derived by matching those against the known pipeline order. That is approximate
by nature: stages take wildly different amounts of time, and the percentage is
there to show the job is alive and roughly how far along, not to predict a
finish time.
"""

from __future__ import annotations

import re
import subprocess
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

#: ODM's pipeline, in execution order, with the share of total runtime each
#: stage typically consumes. Weights come from observed runs; dense
#: reconstruction and texturing dominate, and the early stages are near-instant.
PIPELINE: list[tuple[str, float]] = [
    ("dataset", 0.01),
    ("split", 0.01),
    ("merge", 0.01),
    ("opensfm", 0.25),
    ("openmvs", 0.30),
    ("odm_filterpoints", 0.03),
    ("odm_meshing", 0.10),
    ("mvs_texturing", 0.15),
    ("odm_georeferencing", 0.04),
    ("odm_dem", 0.04),
    ("odm_orthophoto", 0.05),
    ("odm_report", 0.01),
    ("odm_postprocess", 0.00),
]

_STAGE_NAMES = [name for name, _ in PIPELINE]
_TOTAL_WEIGHT = sum(weight for _, weight in PIPELINE)

_RUNNING_STAGE = re.compile(r"Running (\w+) stage")
_FINISHED_STAGE = re.compile(r"Finished (\w+) stage")
_ERROR_LINE = re.compile(r"\b(ERROR|Traceback|MemoryError|Killed)\b")

#: Plain-English descriptions, so the UI can say what is happening rather than
#: showing an internal stage name.
STAGE_DESCRIPTIONS = {
    "dataset": "Reading photos and GPS data",
    "split": "Checking whether the job needs splitting",
    "merge": "Merging submodels",
    "opensfm": "Matching features and solving camera positions",
    "openmvs": "Building the dense point cloud",
    "odm_filterpoints": "Removing noise from the point cloud",
    "odm_meshing": "Building a 3D surface",
    "mvs_texturing": "Painting photos onto the 3D model",
    "odm_georeferencing": "Anchoring the model to real-world coordinates",
    "odm_dem": "Generating the elevation model",
    "odm_orthophoto": "Producing the orthomosaic",
    "odm_report": "Writing the quality report",
    "odm_postprocess": "Finishing up",
}


@dataclass
class Progress:
    """A snapshot of a running reconstruction."""

    stage: str | None = None
    completed_stages: list[str] = field(default_factory=list)
    #: Approximate completion, 0.0 to 1.0. See the module docstring.
    fraction: float = 0.0
    message: str = ""

    @property
    def description(self) -> str:
        if self.stage is None:
            return "Starting"
        return STAGE_DESCRIPTIONS.get(self.stage, self.stage)

    @property
    def percent(self) -> int:
        return int(round(self.fraction * 100))


@dataclass(frozen=True)
class Products:
    """Paths to the outputs of a finished reconstruction.

    Every field may be ``None``: ODM skips products depending on the flags used
    and the nature of the imagery.
    """

    project_dir: Path
    orthophoto: Path | None = None
    orthophoto_preview: Path | None = None
    point_cloud: Path | None = None
    textured_model: Path | None = None
    dsm: Path | None = None
    dtm: Path | None = None
    report: Path | None = None

    # Rendered diagnostic layers. ODM generates these for its own report; they
    # answer questions an operator actually has — how high is the ground, was
    # anything under-photographed, where was the aircraft when it shot.
    dsm_preview: Path | None = None
    dsm_legend: Path | None = None
    overlap_preview: Path | None = None
    overlap_legend: Path | None = None
    camera_positions_preview: Path | None = None

    @property
    def has_map(self) -> bool:
        return self.orthophoto is not None

    @property
    def has_3d_model(self) -> bool:
        return self.textured_model is not None


def find_products(project_dir: str | Path) -> Products:
    """Locate reconstruction outputs in a finished project directory."""
    root = Path(project_dir)

    def maybe(*parts: str) -> Path | None:
        path = root.joinpath(*parts)
        return path if path.exists() else None

    return Products(
        project_dir=root,
        orthophoto=maybe("odm_orthophoto", "odm_orthophoto.tif"),
        # ODM renders this for its own report; it is the cheapest way to show a
        # map preview without a GeoTIFF-capable viewer.
        orthophoto_preview=maybe("opensfm", "stats", "ortho.png"),
        point_cloud=maybe("odm_georeferencing", "odm_georeferenced_model.laz"),
        textured_model=maybe("odm_texturing", "odm_textured_model_geo.obj"),
        dsm=maybe("odm_dem", "dsm.tif"),
        dtm=maybe("odm_dem", "dtm.tif"),
        report=maybe("odm_report", "report.pdf"),
        dsm_preview=maybe("opensfm", "stats", "dsm.png"),
        dsm_legend=maybe("opensfm", "stats", "dsm_gradient.png"),
        overlap_preview=maybe("opensfm", "stats", "overlap.png"),
        overlap_legend=maybe("opensfm", "stats", "overlap_diagram_legend.png"),
        camera_positions_preview=maybe("opensfm", "stats", "topview.png"),
    )


def build_command(
    datasets_dir: str | Path,
    project_name: str,
    *,
    image: str = "opendronemap/odm:latest",
    dsm: bool = True,
    dtm: bool = False,
    fast_orthophoto: bool = False,
    max_concurrency: int | None = None,
    extra_args: list[str] | None = None,
) -> list[str]:
    """Compose the docker command for a reconstruction.

    ``datasets_dir`` holds one folder per project, each containing an ``images``
    subfolder — the layout ODM expects.
    """
    command = [
        "docker", "run", "--rm",
        "-v", f"{Path(datasets_dir)}:/datasets",
        image,
        "--project-path", "/datasets", project_name,
    ]
    if dsm:
        command.append("--dsm")
    if dtm:
        command.append("--dtm")
    if fast_orthophoto:
        command.append("--fast-orthophoto")
    if max_concurrency:
        command += ["--max-concurrency", str(max_concurrency)]
    command += extra_args or []
    return command


def parse_progress(line: str, progress: Progress) -> bool:
    """Fold one line of ODM output into ``progress``.

    Returns True when the line changed the progress state, so callers can avoid
    re-rendering on every one of the thousands of debug lines ODM emits.
    """
    finished = _FINISHED_STAGE.search(line)
    if finished:
        stage = finished.group(1)
        if stage in _STAGE_NAMES and stage not in progress.completed_stages:
            progress.completed_stages.append(stage)
            progress.fraction = _fraction_for(progress.completed_stages)
        return True

    running = _RUNNING_STAGE.search(line)
    if running and running.group(1) != progress.stage:
        progress.stage = running.group(1)
        progress.message = progress.description
        return True

    if _ERROR_LINE.search(line):
        progress.message = line.strip()[:200]
        return True

    return False


def _fraction_for(completed: list[str]) -> float:
    done = sum(weight for name, weight in PIPELINE if name in completed)
    return min(done / _TOTAL_WEIGHT, 1.0)


class ReconstructionError(RuntimeError):
    """The reconstruction exited non-zero."""


def run_reconstruction(
    datasets_dir: str | Path,
    project_name: str,
    *,
    on_progress: Callable[[Progress], None] | None = None,
    log_path: str | Path | None = None,
    **command_kwargs,
) -> Products:
    """Run a reconstruction to completion, reporting progress as it goes.

    Blocks until finished. Raises :class:`ReconstructionError` on failure —
    which matters, because ODM can produce partial output and still have failed,
    and a half-built model presented as a result is worse than an error.
    """
    command = build_command(datasets_dir, project_name, **command_kwargs)
    progress = Progress()
    log_file = open(log_path, "w", encoding="utf-8") if log_path else None

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
        )

        for line in process.stdout:  # type: ignore[union-attr]
            if log_file:
                log_file.write(line)
            if parse_progress(line, progress) and on_progress:
                on_progress(progress)

        code = process.wait()
    finally:
        if log_file:
            log_file.close()

    if code != 0:
        raise ReconstructionError(
            f"OpenDroneMap exited with code {code}. Last message: {progress.message or 'none'}"
        )

    progress.fraction = 1.0
    progress.stage = None
    if on_progress:
        on_progress(progress)

    return find_products(Path(datasets_dir) / project_name)


def stream_reconstruction(
    datasets_dir: str | Path, project_name: str, **command_kwargs
) -> Iterator[Progress]:
    """Run a reconstruction, yielding progress updates as they occur.

    Convenient for driving a UI: iterate and render each update.
    """
    updates: list[Progress] = []
    event = threading.Event()
    finished: list[Exception | Products] = []

    def collect(progress: Progress) -> None:
        updates.append(
            Progress(
                stage=progress.stage,
                completed_stages=list(progress.completed_stages),
                fraction=progress.fraction,
                message=progress.message,
            )
        )
        event.set()

    def work() -> None:
        try:
            finished.append(
                run_reconstruction(
                    datasets_dir, project_name, on_progress=collect, **command_kwargs
                )
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller below
            finished.append(exc)
        event.set()

    thread = threading.Thread(target=work, daemon=True)
    thread.start()

    emitted = 0
    while not finished or emitted < len(updates):
        event.wait(timeout=1.0)
        event.clear()
        while emitted < len(updates):
            yield updates[emitted]
            emitted += 1

    thread.join()
    if isinstance(finished[0], Exception):
        raise finished[0]
