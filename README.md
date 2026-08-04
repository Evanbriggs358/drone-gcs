# drone-gcs

Ground control station for a 7-inch Deadcat autonomous mapping drone. Plans a
photogrammetry survey, uploads it to the aircraft, monitors the flight, offloads the
imagery, and produces an orthomosaic and 3D model.

Built to replace an SSH-and-shell-scripts workflow, and to fly missions that the
flight controller owns end to end — so losing the radio link costs the live map, not
the aircraft.

**Hardware:** Holybro Kakute H7 (ArduPilot) · Raspberry Pi 4B companion computer ·
Pi Camera Module 3 · Holybro M10 GPS · 6S / 7-inch platform

## Status

Flight path proven in simulation. A generated survey grid uploads to ArduPilot and
flies start to finish — takeoff, 16 survey waypoints, camera triggering on and off,
return to launch, landing — with peak altitude and cruise speed matching the plan.

| Component | State |
|---|---|
| Survey planning — grids, GSD, spacing, estimates | ✅ Working |
| Vehicle state and pre-flight gating | ✅ Working |
| Mission build, upload, and verification | ✅ Working |
| Flying a full mission (ArduPilot SITL) | ✅ Verified |
| Camera capture and EXIF geotagging | 🔶 In progress |
| Photogrammetry pipeline and 3D viewer | ⬜ Next |
| Browser UI | ⬜ Planned |

108 tests passing.

## Design decisions worth knowing

**Missions are owned by the flight controller, not the companion computer.** The
previous system streamed position targets from the Raspberry Pi, which meant a WiFi
dropout left the aircraft loitering. Here the survey is written into the flight
controller as an AUTO mission with camera triggering handled by `CAM_TRIGG_DIST`, so
the radio link is for monitoring only.

**Every mission upload is verified by reading it back.** An accepted acknowledgement
is not evidence the autopilot stored what was meant. Uploads are diffed item by item,
including a test for the failure that silently ruins a survey: the mission flies
perfectly and the camera never fires.

**Photo spacing is computed, not assumed.** Trigger distance derives from camera
geometry, altitude, and requested overlap. The previous system fired every 10 m
regardless of altitude — over twice the density needed for 70% overlap at 100 m.

**The planner refuses to quietly produce a bad survey.** It warns when ground speed
outruns the shutter, when overlap is low enough to risk reconstruction holes over
grass or asphalt, and when a nadir-only grid is being used where a 3D model is the
goal.

**No third-party dependencies in the planning core.** Polygon clipping is a scanline
implementation rather than Shapely, so the foundation cannot be blocked by package
availability.

## Documentation

- **[SPEC.md](SPEC.md)** — what the system does, architecture, and the reasoning
- **[PLAN.md](PLAN.md)** — build order, plus findings from getting SITL to fly
- **[HARDWARE.md](HARDWARE.md)** — component analysis: RF attenuation from the carbon
  frame, RC-link interference, compass siting, endurance limits

## Setup

```
py -3.14 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pytest
```

## Try it without hardware

Survey plans for a sample area, and the largest area one battery covers:

```
.venv\Scripts\python.exe tools\preview_plan.py
.venv\Scripts\python.exe tools\battery_budget.py
```

Against a simulated aircraft — start Mission Planner's **Simulation → Multirotor**,
then:

```
.venv\Scripts\python.exe tools\sitl_probe.py     # live telemetry and pre-flight check
.venv\Scripts\python.exe tools\sitl_upload.py    # plan, upload, verify
.venv\Scripts\python.exe tools\sitl_fly.py       # fly the whole survey
```

## Layout

```
gcs/planning/    survey geometry — camera model, projection, grid generation
gcs/link/        MAVLink — vehicle state, pre-flight gating, mission upload
tools/           developer utilities and simulator drivers
tests/           test suite
```

Flight imagery and photogrammetry outputs live outside the repository — they run to
gigabytes per flight.
