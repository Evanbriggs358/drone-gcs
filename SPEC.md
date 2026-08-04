# Drone Ground Control Station — v1 Specification

Ground station for a 7-inch Deadcat autonomous mapping platform. Replaces an
SSH/terminal workflow with a laptop app that plans a mission, uploads it, monitors
the flight live, offloads the imagery, and produces an orthomosaic and 3D model.

Status: **draft spec, pre-implementation.** Nothing here is built yet.

---

## 1. Hardware

| Component | Role |
|---|---|
| 7" Deadcat carbon frame | Airframe; props out of camera FOV |
| 6S LiPo | Power |
| 4-in-1 ESC | Motor drive |
| EMAX ECO II 2807 1300KV ×4 | Propulsion |
| 7" props | Thrust |
| FlySky RC receiver | **Manual override / abort — the safety link** |
| Holybro GPS + magnetometer | Position and heading |
| Holybro Kakute H7 v1.5 | Flight controller; owns stabilization and the mission. **Currently INAV; being reflashed to ArduPilot — see §2.5** |
| Raspberry Pi 4B | Companion computer; mission relay, camera, geotagging, file store |
| Pi Camera Module 3 (IMX708, 12 MP) | Mapping payload |

Kakute H7 ↔ Pi 4B over serial (UART, MAVLink). Pi 4B ↔ laptop over WiFi.

Confirmed part numbers, and the issues found reviewing them — carbon-fibre RF
attenuation, RC-link interference, compass siting, and endurance limits — are in
[HARDWARE.md](HARDWARE.md).

---

## 2. Key design decisions

### 2.1 AUTO mission, not GUIDED streaming

The previous system flew by streaming `SET_POSITION_TARGET_GLOBAL_INT` from the Pi,
which puts the companion computer in the loop for every waypoint (ArduPilot GUIDED
mode). If WiFi drops mid-flight, the aircraft loiters until a failsafe fires.

**v1 instead writes a true AUTO mission into the Kakute's own mission storage.**
Camera triggering is done by ArduPilot's `CAM_TRIGG_DIST` (shutter every N metres).

Consequences:
- The flight controller owns the mission end to end. Link loss is a *monitoring*
  outage, not a flight problem — the grid completes and the aircraft returns home.
- The Pi's job shrinks to: accept mission, relay telemetry, capture and geotag
  photos, hold files for offload.
- Matches how Mission Planner and QGroundControl behave, so behaviour is
  predictable and debuggable against known-good tools.

### 2.2 No live retasking in v1

The app uploads a mission and monitors it. It does not issue in-flight commands
that move the aircraft. **Abort is via the FlySky RC transmitter**, which remains
the pilot's authority at all times. Pause/RTL/land buttons are deferred to v2,
after the link and failsafe behaviour are proven in real flights.

### 2.3 Nadir vs crosshatch — orthomosaic vs 3D

A single-pass nadir lawnmower grid produces an excellent orthomosaic and DEM but a
poor textured 3D model: vertical surfaces are barely observed, so walls smear and
tall objects lose their edges.

The planner therefore supports three patterns, chosen by mission goal:

| Pattern | Passes | Flight time | Best for |
|---|---|---|---|
| Nadir grid | 1 | 1× | Orthomosaic, DEM, area/distance measurement |
| Crosshatch | 2 (perpendicular) | ~2× | **Default when 3D model is the goal** |
| Crosshatch + oblique | 2 + tilted camera | ~2× | Best 3D; needs a fixed camera tilt (~15–30°) |

The Camera Module 3 is fixed-mount, so oblique requires physically angling the
mount. Noted as a hardware follow-up, not a v1 blocker.

### 2.4 Firmware: INAV → ArduPilot

The original blueprint described the Kakute H7 as an ArduPilot controller, but the
existing `capture_daemon.py` talks **MSP** and shares its serial port with **INAV
Configurator**. MSP is INAV/Betaflight's protocol. The aircraft has therefore been
flying **INAV**, and the blueprint's MAVLink/ArduPilot description was inaccurate.

**Decision: reflash to ArduPilot.** This makes §2.1's AUTO-mission design valid —
ArduPilot has mature waypoint missions and `CAM_TRIGG_DIST`, neither of which INAV
offers in an equivalent form. It also means the entire ecosystem of Mission Planner,
QGroundControl, pymavlink, and SITL becomes available for development and debugging.

Cost: a full reflash, complete recalibration, and re-tune of a flying aircraft. It is
reversible — an INAV `dump all` taken beforehand restores the old setup.

**Board target must be confirmed from the silkscreen before flashing.** ArduPilot's
`KakuteH7` target covers the H7 **V1**; `KakuteH7Mini` covers the Mini **v1.1 and
v1.5**; V2 is separate again. "Kakute H7 v1.5" most likely indicates a **Mini v1.5**,
but this must be verified against the physical board, not assumed.

Consequence for the Pi: the flight-controller half of `capture_daemon.py` is
invalidated (MSP → MAVLink). The camera half largely survives. See
[PLAN.md](PLAN.md).

### 2.5 Data lives outside OneDrive

Code lives in `C:\Users\evan\Projects\drone-gcs`. Flight imagery and ODM outputs
are multi-gigabyte per flight and must **not** sit in a synced OneDrive folder.
Default data root: `C:\DroneData\flights\`.

---

## 3. Architecture

```
┌─────────────────────────── Laptop (Dell XPS 15 9520) ──────────────────────────┐
│                                                                                │
│   Browser UI  ──HTTP/WebSocket──▶  Local backend (Python, FastAPI)             │
│   MapLibre GL (2D)                   • flight records (SQLite)                 │
│   three.js (3D)                      • file store                              │
│   no build step                      • ODM orchestration (Docker)              │
│                                                                                │
└────────────────────────────────────┬───────────────────────────────────────────┘
                                     │ WiFi (HTTP + WebSocket)
┌────────────────────────────────────▼───────────────────────────────────────────┐
│  Raspberry Pi 4B — companion service (Python, FastAPI, systemd, starts on boot)│
│    • pymavlink ↔ Kakute H7 over serial                                         │
│    • picamera2 capture                                                          │
│    • EXIF geotag injection                                                      │
│    • telemetry stream, photo store, offload endpoints                          │
└────────────────────────────────────┬───────────────────────────────────────────┘
                                     │ UART / MAVLink
                          ┌──────────▼──────────┐
                          │  Kakute H7 (ArduPilot) │ ──▶ ESC ──▶ motors
                          └─────────────────────┘
```

### Stack rationale

- **Python on both ends.** Mandatory on the Pi (`pymavlink`, `picamera2`); reusing
  it on the laptop means one language and shared mission/telemetry model code.
- **No Node, no bundler.** Node isn't installed and isn't needed. Plain ES modules,
  vendored libraries, served by FastAPI. Nothing to rebuild after an edit.
- **Docker for ODM only.** `opendronemap/odm` runs as a one-shot container against a
  flight folder. Docker Desktop is already installed.
- Laptop Python is 3.14; use a dedicated **3.12 venv** to avoid bleeding-edge
  package gaps in the geospatial stack.

---

## 4. Features

### Phase A — Connect & pre-flight

*This is where the old terminal workflow hurt most. One screen, one verdict.*

- Auto-discover the Pi on the LAN (mDNS, fallback to saved IP); persistent link
  status with round-trip latency
- Pi health: service up, camera detected, free disk space, clock sync
- Vehicle health from MAVLink: GPS fix type, satellite count, HDOP, battery voltage
  and per-cell, EKF status, compass/accel calibration, home position set, armable
- **Pre-flight checklist that gates the launch button.** Every item green or no
  arming. Failures state the reason in plain language ("GPS: 6 sats, need 10+")
- Camera test shot — fire the shutter, pull the thumbnail back, confirm focus and
  exposure before committing to a 15-minute flight

### Phase B — Mission planning

- Satellite basemap; draw the survey polygon by clicking
- Inputs: altitude, front overlap, side overlap, ground speed, grid heading,
  pattern (nadir / crosshatch / oblique)
- Live-computed as parameters change:
  - **Ground sample distance** in cm/px
  - photo count, total path distance, estimated flight time
  - battery margin verdict (green/amber/red against a configured reserve)
- Generated grid drawn over the map before upload
- Takeoff/land point, RTL altitude, geofence polygon, max altitude
- Save, name, reload missions; re-fly a site from a list

**GSD math** (Camera Module 3, IMX708 — 4608×2592, 1.4 µm pixel, 4.74 mm focal):

```
GSD (mm/px)      = altitude_mm × 0.0014 / 4.74
footprint_width  = 4608 × GSD
footprint_height = 2592 × GSD
```

Sanity check at 100 m AGL: **≈ 2.95 cm/px**, footprint ≈ 136 m × 76 m.

Photo spacing for a given front overlap:
`spacing = footprint_height × (1 − front_overlap)`
Line spacing: `footprint_width × (1 − side_overlap)`

Defaults: 70% front / 70% side, matching the previous system.

### Phase C — Upload & launch

- Write waypoints + `CAM_TRIGG_DIST` to the flight controller
- **Read the mission back off the FC and diff it against what was sent** — never
  trust a silent success
- Two-step arm confirmation
- Explicit state display: mission loaded → armed → in progress

### Phase D — In-flight monitoring

- **Live 2D map, aircraft position refreshed at least every 5 s** (design target
  2 Hz; 5 s is the guaranteed floor on a degraded link)
- Track flown drawn over track planned
- HUD: altitude AGL, groundspeed, battery, distance to home, satellites, flight
  mode, waypoint *n* of *m*, estimated time remaining
- **Photo counter with thumbnails streaming back** — confirm the shutter is firing
  during the flight, not on the SD card afterwards
- Unmissable link-loss banner that also reassures: the aircraft is flying the
  mission itself and will return home
- Alerts: low battery, GPS degradation, geofence/altitude breach
- Full telemetry stream recorded to a flight log for later review

### Phase E — Offload & validation

- One click: pull photos from Pi to laptop, with progress and resume-on-interrupt
- Validate every image carries EXIF GPS; flag missing or blurry frames
- **Coverage map — plot every photo footprint over the survey polygon and show the
  gaps while you are still on site and can re-fly.** Highest-value feature in the
  rebuild
- Write a per-flight folder with imagery plus mission and telemetry metadata

### Phase E2 — Vehicle health from flight logs

Directly targets the reported position-hold drift (HARDWARE.md Finding 3). The app
already pulls the flight log; analysing it costs one more step and closes a loop that
previously required guesswork.

- **Compass health check** — compare logged magnetic field magnitude against the World
  Magnetic Model expectation for the flight location, and flag drift or interference
- **Motor-current correlation** — flag compass error that scales with throttle, the
  signature of ESC interference
- **One-click log export prepared for the
  [MAGFit web tool](https://firmware.ardupilot.org/Tools/WebTools/MAGFit/)**, so
  calibration comes from real flight data rather than hand-rotating the airframe
- Vibration levels and EKF innovation summaries, flagged against ArduPilot's thresholds

Running the full MAGFit solve in-app is a possible later step; v1 surfaces the
diagnosis and hands off to the official tool.

### Phase F — Processing & output

- One click kicks off `opendronemap/odm` in Docker against the flight folder
- Live progress and log tail in the UI; job survives a UI reload
- Outputs surfaced in-app:
  - **Orthomosaic** (GeoTIFF) tiled onto the 2D map
  - **Dense point cloud** and **textured mesh** in a browser 3D viewer (three.js)
  - **DEM/DSM**
- Measurement tools on the orthomosaic: distance, area, and volume

---

## 5. Flight folder layout

```
C:\DroneData\flights\2026-08-02_143012_north-field\
├── mission.json          plan: polygon, params, generated waypoints
├── telemetry.jsonl       full recorded flight log
├── photos\               geotagged JPEGs pulled from the Pi
├── coverage.json         computed photo footprints
├── odm\                  ODM working directory and outputs
│   ├── odm_orthophoto\
│   ├── odm_texturing\
│   └── odm_dem\
└── flight.json           summary: times, battery, photo count, validation results
```

---

## 6. Explicitly out of scope for v1

- In-flight retasking, fly-to-point, manual control from the app
- Live video downlink
- Multi-aircraft
- Cloud hosting / client-facing map sharing (architecture keeps this open for v2)
- Automatic re-fly of detected coverage gaps (v1 shows gaps; you re-plan manually)

---

## 7. Open questions

1. **Exact board variant** — resolves the ArduPilot flash target. Requires reading
   the physical board. Blocking for the hardware track only.
2. **Pi network setup in the field** — does the Pi join a phone hotspot, a
   dedicated field AP, or run its own AP that the laptop joins? Affects discovery
   and expected range.
3. **Typical survey size** — drives whether ODM runs comfortably (a few hundred
   images) or needs tiled/split processing (thousands).
4. **Camera mount tilt** — is angling the Camera Module 3 for oblique capture
   acceptable, or must it stay nadir?

Resolved: the old Pi code is `capture_daemon.py` — a passive MSP geotagging trigger
with no flight control. Its camera design is worth keeping; its FC interface is not.
