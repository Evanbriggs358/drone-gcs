# drone-gcs

Ground control station for a 7-inch Deadcat autonomous mapping platform —
Holybro Kakute H7 flight controller, Raspberry Pi 4B companion computer,
Pi Camera Module 3 payload.

Plans a survey, uploads it, monitors the flight live, offloads the imagery, and
produces an orthomosaic and 3D model. Replaces an SSH-and-scripts workflow.

- **[SPEC.md](SPEC.md)** — what it does, architecture, and the reasoning behind the
  design decisions
- **[PLAN.md](PLAN.md)** — build order and the hardware bring-up sequence

## Status

Sprint 1 complete: mission planning math and survey grid generation, 34 tests passing.
Everything else is unbuilt.

## Setup

```
py -3.14 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Run the tests:

```
.venv\Scripts\python.exe -m pytest
```

See planner output for a sample survey area:

```
.venv\Scripts\python.exe tools\preview_plan.py
```

## Layout

```
gcs/planning/    survey geometry — camera model, projection, grid generation
tools/           developer utilities
tests/           test suite
```

Flight imagery and photogrammetry outputs live in `C:\DroneData`, never in this
repository — they run to gigabytes per flight.
