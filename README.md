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

Every stage works and is proven end to end in simulation: a survey is drawn on a
map, uploaded to ArduPilot, flown start to finish, photographed and geotagged,
offloaded, reconstructed, and viewed as an orthomosaic and 3D model.

| Component | State |
|---|---|
| Survey planning — grids, GSD, spacing, endurance | ✅ Working |
| Ground station UI — map, planning, pre-flight, live tracking | ✅ Working |
| Mission build, upload, and readback verification | ✅ Working |
| Flying a full mission (ArduPilot SITL) | ✅ Verified |
| Companion service — capture, geotagging, systemd | ✅ Verified in SITL |
| Resumable photo offload | ✅ Verified |
| Coverage-gap detection | ✅ Working |
| Photogrammetry pipeline and 3D viewer | ✅ Verified on real imagery |
| Flight-log diagnostics | ✅ Working |
| Real aircraft | ⬜ Awaiting hardware |

265 tests passing.

**The one piece never executed** is the Raspberry Pi camera driver, which cannot run
off a Pi. See [PLAN.md](PLAN.md) for what else moves from simulator to aircraft, and
what does not.

### Proven in simulation

A 200 m survey planned, uploaded, flown, and captured:

```
Captured 151 photos (0 failed) from 151 triggers
Geotagged: 151/151      Heading: 151/151
Altitude:  644.1 m      (584 m home + 60 m survey altitude)
Spacing:   median 13.8 m   (planner asked for 13.8 m)
```

Offloaded, then deliberately corrupted and re-run: 3 re-fetched, 148 skipped,
0.1 MB transferred instead of 7.5 MB, all 151 revalidating clean afterwards.

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

**Endurance is modelled, not assumed.** Flying faster does not simply cost flight
time on a multirotor: most power goes into staying up, and forward flight gives the
rotors cleaner air, so endurance *rises* with speed before drag overwhelms it. The
planner computes that curve and recommends the speed covering the most ground per
battery. One timed hover calibrates the model to measured reality.

**Coverage gaps are found before you leave the site.** Every survey has a thin
perimeter, so only under-covered ground *surrounded by good coverage* is reported —
including ground photographed zero times, which is the case that matters most.

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

## Run the ground station

```
.venv\Scripts\python.exe -m gcs.server
```

Opens at http://127.0.0.1:8000 — draw a survey, watch the plan update as you move a
slider, connect to the aircraft, run the checklist, and upload.

## Try it without hardware

Planning and endurance, no simulator needed:

```
.venv\Scripts\python.exe tools\preview_plan.py
.venv\Scripts\python.exe tools\power_curve.py --hover-min 18
.venv\Scripts\python.exe tools\battery_budget.py
```

Against a simulated aircraft — start Mission Planner's **Simulation → Multirotor**,
or run `ArduCopter.exe` directly with `tools/sitl_keepalive.py` holding port 5760:

```
.venv\Scripts\python.exe tools\sitl_probe.py     # telemetry and pre-flight check
.venv\Scripts\python.exe tools\sitl_upload.py    # plan, upload, verify
.venv\Scripts\python.exe tools\sitl_fly.py       # fly the whole survey
.venv\Scripts\python.exe tools\sitl_capture.py   # fly it and capture photos
```

After a flight:

```
.venv\Scripts\python.exe tools\offload.py --list
.venv\Scripts\python.exe tools\inspect_photos.py <folder>\images
.venv\Scripts\python.exe tools\check_coverage.py <folder>
.venv\Scripts\python.exe tools\analyse_log.py <log>.BIN
```

## Layout

```
gcs/planning/     survey geometry, camera model, endurance
gcs/link/         MAVLink — vehicle state, pre-flight gating, missions
gcs/companion/    Raspberry Pi service — capture, geotagging, HTTP API
gcs/processing/   OpenDroneMap orchestration
gcs/diagnostics/  flight-log analysis
gcs/server/       ground station web app
gcs/coverage.py   coverage-gap detection
gcs/offload.py    resumable photo transfer
deploy/           systemd unit for the Pi
tools/            command-line utilities and simulator drivers
tests/            test suite
```

Flight imagery and photogrammetry outputs live outside the repository — they run to
gigabytes per flight.
