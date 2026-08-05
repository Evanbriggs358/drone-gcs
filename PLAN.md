# Rebuild Plan

Two tracks: **software** (buildable now, no drone) and **hardware bring-up** (needs the
aircraft in hand). The goal of this plan is to have the ground station essentially
finished and proven against a simulator by the time the drone comes back, so hardware
time is spent on calibration and flight testing — not debugging code.

Companion doc: [SPEC.md](SPEC.md).

---

## Project goals — these drive prioritisation

1. **Produce a demonstrable result**: a real orthomosaic and 3D model of real ground,
   proving the system works end to end. This is the primary goal.
2. **Serve as a portfolio piece.** The repository is part of the deliverable —
   commit history, documented engineering decisions, and test coverage carry weight
   alongside the code.
3. Replace the SSH-and-scripts workflow with something operable.

Survey areas are small, so throughput and coverage scale are not constraints.

### Consequences

- **Battery-aware mission splitting is cut.** Small sites fit in one pack.
- **The full browser UI moves later.** It does not produce evidence the system works.
- **The photogrammetry pipeline moves first.** It is the biggest unknown in the chain,
  and — critically — it can be validated on OpenDroneMap's public sample datasets
  *before the aircraft is available*. That produces a real 3D model in the app while
  the drone is still away, leaving only the imagery source unproven.
- A **minimal single-page UI** wraps the pipeline, so results are presentable rather
  than command-line only.

### Revised order

| Order | Work | Needs drone? |
|---|---|---|
| 1 | ODM pipeline + orthomosaic/3D viewer, validated on sample data | No |
| 2 | Minimal UI to drive it | No |
| 3 | Pi companion service — capture, geotag, offload | No (fake camera) |
| 4 | Finish MAVLink layer — arm, AUTO, fly the mission in SITL | No |
| 5 | Hardware bring-up and first real survey | Yes |
| 6 | Full ground-station UI — map, planning, live telemetry | No |

---

## The key unlock: ArduPilot SITL

**ArduPilot SITL** (Software In The Loop) runs the real ArduPilot flight code as a
process on the laptop. It speaks genuine MAVLink, accepts real mission uploads, flies
real AUTO missions with simulated GPS/baro/compass, and responds to failsafes.

This means the ground station can be built and verified end to end with no aircraft.

**SITL runs natively on Windows through Mission Planner** — no WSL, no Linux, no build
environment. Mission Planner downloads and runs the SITL binaries itself, using the same
physics models as the Linux `sim_vehicle.py` path, and exposes MAVLink over UDP, which is
all the ground station needs.

Mission Planner is **already installed** at `C:\Program Files (x86)\Mission Planner`.
It is also the tool required for the hardware track — flashing, calibration, and the
props-off motor test — so it earns its place twice.

### WSL is not required

An earlier draft of this plan claimed WSL2 was needed and already installed. Both were
wrong. Current position:

| Need | Windows-native option | Requires WSL? |
|---|---|---|
| ArduPilot SITL | Mission Planner built-in simulator | No |
| OpenDroneMap | Native installer, **$64 one-time** | No |
| OpenDroneMap | Docker image, free | Yes |
| OpenDroneMap | Manual source install, free | No, but laborious |

**Decision: WSL2 + Docker, the free route.** Chosen over the paid installer not to save
money but because a containerised pipeline is the better engineering story for a
portfolio project, and Docker is the canonical way ODM ships. Setup is roughly 20
minutes, mostly downloads.

Requires, in an Administrator PowerShell:

```
wsl --install -d Ubuntu
```

Reboot when prompted, then launch Docker Desktop once. **This now blocks the top
priority item** — it is no longer deferrable to a later sprint.

Combined with:
- **ODM sample datasets** — the whole photogrammetry stage is testable on real public
  imagery right now
- **A fake companion service** — emulates the Pi: synthetic camera frames, EXIF
  geotagging driven by SITL's simulated position, downlink streaming

…roughly **85% of the app can be finished before the drone is touched.** What genuinely
requires hardware: serial wiring to the Kakute, sensor calibration, real camera
focus/exposure, WiFi range behaviour, and flight testing.

---

## Does the Pi code need rewriting? Yes — about half of it.

Switching INAV → ArduPilot replaces **MSP with MAVLink**. Everything in
`capture_daemon.py` that talks to the flight controller is invalidated: the MSP request
loop, the GPS/attitude/battery parsing, the polling structure.

What carries over nearly unchanged:

| Keep | Why |
|---|---|
| Dual-stream camera (12 MP save / 1 MP downlink) | Correct design; don't waste WiFi on full-res |
| Focus locked to infinity, fast shutter | Right call for mapping |
| Haversine distance-based triggering | Right approach — but distance becomes computed, not a hardcoded 10 m |
| piexif EXIF injection | Extend it to write yaw as well as lat/lon/alt |
| Auto-reconnecting downlink | Keep the resilience; move from raw TCP to WebSocket |

What changes structurally: it becomes a **systemd service that starts on boot** with an
HTTP/WebSocket API, instead of a script you SSH in to launch. That alone removes most
of the terminal friction.

---

## Software track — buildable now

### Sprint 0 — Foundation ✅ **mostly done**
- ✅ Project scaffold, venv (Python 3.14 — 3.12 proved unnecessary, Sprint 1 needs no
  third-party packages), pytest, pyproject
- ⬜ ArduPilot SITL running via Mission Planner's built-in simulator
- ⬜ Prove a Python MAVLink connection to SITL: read telemetry, confirm heartbeat
- **Exit criteria:** a script prints live simulated GPS position

### Sprint 1 — Mission planning ✅ **DONE**
Pure logic, no hardware, no third-party dependencies. 34 tests passing.

- `gcs/planning/camera.py` — GSD, footprint, photo/line spacing, max ground speed,
  motion blur; Camera Module 3 standard and wide presets
- `gcs/planning/geo.py` — local tangent-plane projection, haversine, shoelace area
- `gcs/planning/grid.py` — polygon → serpentine flight lines, concave-polygon
  clipping, crosshatch, edge margin, flight-time estimation, parameter warnings
- `tools/preview_plan.py` — prints plans for a test area at various settings

Verified against hand calculation: 100 m altitude → 2.95 cm/px, 136 × 77 m footprint,
23 m photo spacing, 41 m line spacing at 70/70 overlap.

Deliberately built with **zero third-party dependencies** — polygon clipping is a
scanline implementation rather than Shapely — so no wheel availability problem on
Python 3.14 can block the foundation.

**Follow-up found while testing:** crosshatch at 60 m over 40 acres needs ~45 min of
flight, well beyond one 6S pack. Battery-aware mission splitting would address this,
but has been **cut from scope** — survey areas are small enough to fit one pack.
`tools/battery_budget.py` remains, so the limit is visible rather than surprising.

### Sprint 2 — MAVLink layer 🔶 **in progress**
- ✅ `gcs/link/state.py` — folds HEARTBEAT, GLOBAL_POSITION_INT, GPS_RAW_INT,
  ATTITUDE, VFR_HUD, SYS_STATUS, MISSION_CURRENT and STATUSTEXT into one
  `VehicleState` snapshot; ArduCopter mode table; sentinel handling; cell-count
  inference for per-cell voltage
- ✅ `gcs/link/preflight.py` — the launch gate. Blocking vs warning severities,
  plain-English failure text naming measured values, configurable thresholds
- ✅ `gcs/link/mission.py` — AUTO mission construction from a `SurveyPlan`,
  MAVLink upload, readback, and diff verification
- ✅ `tools/sitl_probe.py`, `tools/sitl_upload.py` — verified live against SITL
- ✅ Arm/disarm, mode changes, verified parameter writes
- ✅ `tools/sitl_fly.py` — flies a complete survey mission in SITL
- ⬜ Connection manager: threaded message pump, reconnect, heartbeat timeout
- **Exit criteria met:** a generated grid uploaded to SITL and flown end to end —
  takeoff, 16 survey waypoints, camera trigger on and off, RTL, landing, disarm.
  Peak altitude 60.0 m, peak speed 8.0 m/s, matching the plan.

#### Findings from getting SITL to fly — all apply to the real aircraft

1. **A ground station's HEARTBEAT was aborting missions.** Mission Planner shares the
   link and emits `HEARTBEAT` with `base_mode = 0`, which the state layer read as "the
   vehicle disarmed", ending monitoring at a random point each run. Fixed by ignoring
   heartbeats from non-vehicle MAV_TYPEs; regression tests in `test_state.py`. This
   would have occurred on the real aircraft with any second GCS or telemetry device.
2. **ArduCopter refuses to arm in AUTO** — `Arm: Auto mode not armable`. The launch
   sequence must arm in a normal mode (LOITER) and then switch to AUTO. Kept
   deliberately rather than bypassed, since it matches real operating practice.
3. **`AUTO_OPTIONS` bit 1 is required** for an autonomous launch, otherwise the aircraft
   sits armed waiting for throttle input that never comes. Bit 0 (arm in AUTO) is
   deliberately left off. **This is a required setup parameter on the real aircraft.**
4. **MAVLink parameter writes fail silently.** Setting a non-existent parameter produces
   no error and no ack. Every write is now verified by reading the echoed value.
5. **`WPNAV_SPEED` no longer exists in current firmware** — it is `WP_SPD`, and in m/s
   rather than cm/s (`WPNAV_RADIUS` → `WP_RADIUS_M`, `WPNAV_ACCEL` → `WP_ACC`). The
   code tries the modern name and falls back, since the aircraft's firmware version
   decides which exists.
6. **`MISSION_CURRENT` resets to 0 after landing**, so mission progress must be tracked
   by the furthest item reached, not the latest reported.

**Verified live against SITL:** a 15.4 acre survey planned, built into a 21-item
mission, uploaded, read back, and every item confirmed matching. Camera trigger
distance came from the planner (18.4 m at 80 m altitude) rather than a hardcoded
constant.

Unit tests use real `pymavlink` message objects rather than stubs, so wrong field
names fail in CI instead of on the flight line. Field names were verified against
the installed dialect.

Unit tests use real `pymavlink` message objects rather than stubs, so wrong field
names fail in CI instead of on the flight line. Field names were verified against
the installed dialect.

### Sprint 3 — Live ground station UI
- Browser UI, MapLibre GL, satellite basemap
- Polygon drawing, parameter panel with live GSD/time/battery feedback
- **Live 2D map at ≥5 s refresh** (target 2 Hz), planned vs flown track
- Telemetry HUD, pre-flight checklist gating launch
- **Exit criteria:** plan, upload, and watch a full SITL mission entirely from the browser

### Sprint 4 — Companion service
- FastAPI service, systemd unit, starts on boot
- MAVLink to FC, camera capture, EXIF geotag with attitude
- Computed trigger distance from mission parameters
- Photo store and offload endpoints
- Developed against SITL + fake camera; swapped to real hardware later
- **Exit criteria:** service runs a full simulated mission producing geotagged synthetic images

### Sprint 5 — Offload & validation
- Pull photos to laptop with resume-on-interrupt
- EXIF validation, blur detection
- **Coverage map** — photo footprints over the polygon, gaps highlighted
- **Exit criteria:** deliberately drop photos from a test set and see the gap appear

### Sprint 6 — Photogrammetry & 3D
- ODM in Docker against a flight folder, progress and log tail in UI
- Orthomosaic tiled onto the 2D map
- Point cloud and textured mesh in a three.js viewer
- Measurement tools
- Validated on an ODM sample dataset
- **Exit criteria:** a real orthomosaic and 3D model rendered in the app

### Sprint 7 — Hardware integration *(needs the drone)*
- Real serial link Pi ↔ Kakute, real camera
- Field WiFi range testing
- Progressive flight validation

---

## Hardware track — when the drone is back

**Order matters. Do not skip step 1 or step 7.**

### 1. Identify the board — before anything else
Read the silkscreen. `KakuteH7` (V1), `KakuteH7Mini` (v1.1/v1.5), and V2 are
**different ArduPilot targets**. Flashing the wrong one wastes a weekend.

### 2. Back up the existing INAV configuration
In INAV Configurator, CLI tab → `dump all` → save to a file. This is your revert path.
The switch to ArduPilot is fully reversible; this file is what makes it so.

Record from the existing config before wiping:
- Motor order and spin directions
- ESC protocol (DShot variant)
- Receiver protocol (FlySky → likely iBUS) and which UART it's on
- Which UART the GPS is on, and which one goes to the Pi

### 3. Flash ArduPilot
DFU: hold the bootloader button while connecting USB, load the `with_bl.hex` for the
correct target. Later updates use `.apj` through Mission Planner.

### 4. Frame and initial setup
Mission Planner setup wizard. Frame class Quad. **Frame type needs confirming for the
deadcat geometry** — ArduPilot has no dedicated deadcat type; X is the usual choice, and
the asymmetry is handled by the controller. Verify against current ArduPilot docs at the
time of setup rather than assuming.

### 5. Sensor calibration — this is your "GPS recalibration"
- Accelerometer calibration (6-position)
- **Compass calibration** — the magnetometer in the Holybro GPS module. This is what
  actually needs redoing; GPS itself doesn't calibrate
- Confirm satellite count and HDOP outdoors before trusting anything

### 6. Radio, ESC, battery
- Radio calibration, flight mode switch assignment
- **Reserve an RC switch as the abort/mode override** — this is the safety link
- ESC protocol and calibration
- Battery monitor calibration against a measured 6S voltage

### 7. Motor test — PROPS OFF
Verify every motor's position and spin direction through Mission Planner's motor test.
**ArduPilot's motor numbering differs from INAV's.** A wrong motor order means the
aircraft flips on takeoff. Props off, verify, then props on.

### 8. Failsafes
RC failsafe, battery failsafe, GCS failsafe, geofence, RTL altitude. Configure before
the first flight, not after.

### 9. Serial link to the Pi
Set the Pi's UART to MAVLink2 protocol at a sensible baud, confirm heartbeat from the Pi.

### 10. Progressive flight testing
Stabilize hover → AltHold → Loiter → Autotune → a small 4-waypoint AUTO mission in an
open area → full mapping missions. Do not jump to a mapping mission on day one.

---

## From simulator to real aircraft

SITL runs the actual ArduPilot flight code and speaks the same MAVLink the Kakute
will, so most of the stack moves across untouched. What follows is an honest
account of what does not.

### Unchanged

Survey planning, mission build/upload/verification, vehicle state, pre-flight
gating, trigger handling, geotagging, the capture manifest, and offload. All of it
was exercised against real ArduPilot, just running on a laptop.

### Configuration only

| | Simulator | Aircraft |
|---|---|---|
| `DRONE_FC_ENDPOINT` | `tcp:127.0.0.1:5762` | `/dev/serial0` |
| `DRONE_FC_BAUD` | n/a | `921600` |
| `DRONE_FAKE_CAMERA` | `1` | unset |

### Untested — expect problems here first

1. **`PiCameraModule3` has never run.** Written against picamera2's documentation
   and impossible to execute off a Pi. Most likely source of first-run breakage.
2. **Real capture duration is unknown.** `FakeCamera` writes a small JPEG in
   milliseconds; a 12 MP capture takes a substantial fraction of a second. Measure
   it and set `Camera.min_capture_interval_s` in `gcs/planning/camera.py` to the
   real figure, so `max_ground_speed_ms` stops being a guess.
3. **Sustained write speed.** ~5 MB per photo every ~1.7 s is roughly 3 MB/s to
   the microSD, continuously.
4. **The serial link.** Wiring, baud, and `SERIALn_PROTOCOL=2` on the Kakute.
   SITL used TCP, so none of this has been exercised.
5. **Real GNSS behaviour.** SITL reports `RTK_FIXED` with 10 satellites
   immediately. Expect a slower fix and worse HDOP outdoors; the pre-flight
   thresholds may need revisiting against reality rather than relaxing on sight.
6. **Thermal behaviour.** A Pi 4 capturing 12 MP frames inside a carbon frame in
   sunlight is a throttling candidate.

### Required flight-controller parameters

Confirmed necessary during SITL testing:

- `CAM1_TYPE` — a camera backend must exist or the autopilot never announces
  triggers. A servo backend with nothing wired to the output is correct: the
  trigger is a *decision*, broadcast over MAVLink, and the Pi acts on it.
- `AUTO_OPTIONS` bit 1 — otherwise an armed aircraft waits for throttle input
  that an autonomous launch never provides.
- `SERIALn_PROTOCOL = 2` on the UART going to the Pi.
- Waypoint speed is `WP_SPD` (m/s) on current firmware, `WPNAV_SPEED` (cm/s) on
  older. The code tries both.

### Software bring-up order on the Pi

1. Install, run `python -m gcs.companion` by hand, confirm `/health`.
2. Fix whatever `PiCameraModule3` gets wrong. Capture one frame to disk.
3. Time a full-resolution capture; update `min_capture_interval_s`.
4. Wire the UART, confirm `/health` reports the link connected.
5. Bench-test triggering: set `CAM1_TRIGG_DIST`, walk the aircraft around, and
   confirm photos appear with sensible geotags.
6. Only then install the systemd unit and fly.

## Flight-log diagnostics ✅ **DONE** (SPEC Phase E2)

`gcs/diagnostics/logs.py` and `tools/analyse_log.py`. Reads an ArduPilot `.BIN`
and reports vibration against ArduPilot's thresholds, compass field stability,
motor-current interference, GPS quality, persistent attitude bias in hover, and
whether the log can feed MAGFit.

Worth restating plainly: **none of the ground-station software fixes flight
stability.** Drift is a flight-controller and airframe problem. The firmware
switch, a correctly mounted FC, and AutoTune are the fixes; this tooling only
diagnoses and prevents flying into a known-bad state.

## What is left

Everything needed for a real mapping flight exists and is proven in simulation.
Remaining work is either hardware-dependent or polish:

- **Hardware bring-up** — the sequence below. Blocked on the aircraft.
- **`PiCameraModule3` has never run.** Written against picamera2's documentation
  and impossible to execute off a Pi.
- **Measured capture duration** — needed to replace the guessed
  `min_capture_interval_s` and make the speed limits real.
- Starting a reconstruction from the browser rather than a command.
- Saving and reloading named survey sites.
- GPU-accelerated ODM, worth setting up once real surveys make the hours matter.

## Immediate next steps

1. Confirm the project folder location (see SPEC §2.4 — currently
   `C:\Users\evan\Projects\drone-gcs`)
2. **Restart Claude Code with the project folder as the working directory**, otherwise
   every file write prompts for permission
3. Begin Sprint 0

## Still open

- Field networking: Pi joins a hotspot, a field AP, or hosts its own AP?
- Typical survey size — acres and photo count per flight
- Camera mount: can it be angled 15–30° for oblique capture, or must it stay nadir?
