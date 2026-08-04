# Hardware Roster & Findings

Confirmed component list with the issues found while reviewing it. Companion to
[SPEC.md](SPEC.md) and [PLAN.md](PLAN.md).

---

## Roster

| Component | Part | Notes |
|---|---|---|
| Frame | [Pyrodrone Source One V3 7" Long Range, 6 mm deadcat arms](https://pyrodrone.com/products/pyrodrone-source-one-v3-7-long-range-frame-6mm-v0-2-deadcat-arms) | Carbon fibre — see RF findings below |
| Flight controller | [Holybro Kakute H7](https://holybro.com/products/kakute-h7) | Currently INAV; reflashing to ArduPilot |
| ESC | [Holybro Tekko32 F4 Metal 4in1 65A](https://holybro.com/products/tekko32-f4-metal-4in1-65a-esc-65a) | BLHeli32, DShot, built-in current sensor |
| Motors | [EMAX ECO II 2807 1300KV](https://www.readymaderc.com/products/details/emax-eco-ii-2807-brushless-motor-1300kv) ×4 | Standard 6S 7" long-range pairing |
| Props | [HQProp 7×4.5 2-blade](https://www.amazon.com/HQProp-7X4-5-2-Blade-Propeller-Set/dp/B09TDV17JY) | |
| Battery | [CNHL Black Series 5000 mAh 6S 65C, XT90](https://chinahobbyline.com/products/cnhl-black-series-5000mah-22-2v-6s-65c-lipo-battery-with-xt90-plug) | See endurance finding |
| Battery lead | [Amass XT90-S 10AWG pigtail, female](https://www.getfpv.com/amass-xt90-s-10awg-lipo-pigtail-female-3pcs.html) | Anti-spark |
| GPS/compass | [Holybro M10 GPS](https://holybro.com/products/m10-gps) | u-blox M10 + IST8310 compass, safety switch, buzzer |
| RC | [FlySky FS-i6X + FS-iA6B](https://www.amazon.com/Flysky-FS-i6X-Transmitter-FS-iA6B-Receiver/dp/B0744DPPL8) | 6 channels default — see finding |
| Companion | [Raspberry Pi 4 Model B](https://www.amazon.com/Raspberry-Model-2019-Quad-Bluetooth/dp/B07TC2BK1X) | |
| Payload | [Raspberry Pi Camera Module 3](https://www.microcenter.com/product/662016/raspberry-pi-camera-3) | IMX708, 4608×2592, 1.4 µm, 4.74 mm |
| Pi power | [SoloGood UBEC 5V 5A](https://www.amazon.com/SoloGood-Module-Quadcopter-Airplane-Servo/dp/B0C3GVGYDS) | 6S-safe — see finding |

---

## Findings

### 1. Pi WiFi will not work from inside a carbon-fibre frame — **blocking**

Carbon fibre is electrically conductive and heavily attenuates 2.4/5 GHz. The Pi 4's
onboard antenna is a chip antenna on the PCB. Mounted inside the Source One frame it
will manage tens of metres, not the hundreds needed to monitor a survey grid.

**Current approach: a phone hotspot**, with no external antenna on the Pi. Note that
cell coverage at the field is irrelevant to this link — the hotspot is a local network
and the Pi and laptop talk directly across it. Cell data only matters for fetching
satellite basemap tiles, which the app will cache offline for pre-planned sites.

Expect a good link on the ground and near takeoff, with dropouts out over the grid.

**Consequences for the software:** the companion service is designed for
**intermittent connectivity** — log all telemetry and imagery locally, stream
opportunistically, backfill gaps on reconnect. The live map goes live-then-catches-up
rather than failing. This is the right design regardless of radio quality.

**Test before the first field day:** some hotspots enable AP client isolation, which
blocks laptop↔Pi traffic entirely. Join both to the hotspot at home and ping the Pi.

**Upgrade path:** a USB WiFi adapter with an external antenna mounted clear of the
carbon, if live-map coverage over the full grid turns out to matter.

### 2. Pi WiFi on 2.4 GHz threatens the RC link — **safety**

The FlySky link is 2.4 GHz and is the abort path and only manual override. A WiFi
radio transmitting centimetres away can desense the FS-iA6B receiver.

**Fix:** run the Pi link on **5 GHz**, and separate the WiFi and RC antennas as far as
the airframe allows.

### 3. Compass interference from ESC currents — **likely cause of existing GPS trouble**

A 4-in-1 ESC passing tens of amps generates magnetic fields that corrupt the IST8310.
If the M10 sits close to the ESC or the battery leads, compass calibration fails or
passes and then drifts in flight — toilet-bowling, heading errors, bad position hold.

The M10 **is** mounted on its mast, so gross siting is already handled. Keep battery
leads twisted and routed away from the module.

#### Reported symptom

Position-hold drifts steadily in one direction. Attributed to inaccurate compass
calibration, because calibration requires rotating the airframe through each axis by
hand, which is hard to do precisely.

#### Diagnose before fixing — the symptom points away from the compass

The two failure modes look different in the air:

| Symptom | Usual cause |
|---|---|
| Circling a point, spiralling outward ("toilet-bowling") | Compass yaw error or interference |
| **Steady drift in one direction** | **Accelerometer / level calibration**, wind, or GPS glitching |

A constant one-way drift is more characteristic of the FC's idea of level being tilted
than of a compass error. ArduPilot's `Calibrate Level` addresses this directly, and its
accelerometer calibration is more rigorous than INAV's. A flight log would settle it.

#### ArduPilot removes the hand-rotation problem entirely

**[MAGFit](https://ardupilot.org/copter/docs/common-magfit.html)** calibrates the
compass *from a flight log* instead of from a calibration dance. Fly normally, download
the `.bin`, and drop it into the
[MAGFit web tool](https://firmware.ardupilot.org/Tools/WebTools/MAGFit/). It compares
logged magnetic field against the World Magnetic Model using GPS position and vehicle
attitude, then solves for offsets, diagonals, off-diagonals, scale, and **motor-current
compensation** — and it detects incorrect sensor orientation.

Calibration quality then comes from real flight data rather than from how steadily the
airframe was rotated by hand. This is the single strongest practical argument for the
INAV → ArduPilot switch.

#### Or drop the compass altogether

ArduPilot Copter can derive yaw from GPS velocity using the Gaussian Sum Filter:
`EK3_SRC1_YAW = 8`
([compass-less operation](https://ardupilot.org/copter/docs/common-compassless.html)).

Requires a u-blox M8-generation GPS or better — the M10 qualifies comfortably. Caveats
worth knowing before trying it:

- Copter must be told to use GSF explicitly; unlike Plane it will not do so automatically
- Arming needs `ARMING_SKIPCHK` for compass, a forced arm, or walking the vehicle in a
  small circle after GPS lock so GSF can acquire yaw alignment
- First movements in position-controlled modes may go the wrong way until yaw aligns

For a mapping aircraft that flies long straight lines at speed, GSF has plenty of
velocity signal to work with. Worth trialling once basic flight is proven — not on the
first flight after reflashing.

### 4. Endurance vs. mission duration — **affects planner design**

A 5000 mAh 6S pack on a 7" long-range build gives roughly 15–25 minutes depending on
cruise speed and all-up weight. Measured planner output over a 400 m square:

| Mission | Duration | Batteries |
|---|---|---|
| Nadir @100 m | 10.7 min | 1 |
| Crosshatch @100 m | 22.6 min | 1–2 |
| Crosshatch @60 m | 45.0 min | 3 |

Inverting the question — the largest square area coverable on **one** battery,
assuming 15 min usable minus 3 min for climb, transit, and landing
(`tools/battery_budget.py`):

| Setting | GSD | Max area | Photos |
|---|---|---|---|
| Nadir @120 m | 3.54 cm/px | 66.8 ac | 231 |
| Nadir @100 m | 2.95 cm/px | 45.4 ac | 210 |
| Nadir @60 m | 1.77 cm/px | 19.6 ac | 264 |
| Crosshatch @120 m | 3.54 cm/px | 31.7 ac | 224 |
| Crosshatch @100 m | 2.95 cm/px | 19.1 ac | 210 |
| Crosshatch @60 m | 1.77 cm/px | 8.3 ac | 238 |

So a single pack is genuinely sufficient for sites under roughly 20 acres, even in
crosshatch. It becomes limiting for large sites or high-detail 3D work.

The 15-minute endurance figure is an **assumption until measured** — fly a timed hover
at real all-up weight and update it. Wind reduces it further.

The planner should still gain **battery-aware mission splitting with resume points**,
but it is not urgent at these site sizes. Not yet built.

### 5. FS-i6X has 6 channels by default — **check before setup**

ArduPilot wants 4 stick channels plus a 6-position flight mode channel, and ideally a
dedicated emergency/RTL switch. Six channels is workable — two switches can be mixed to
synthesise six mode positions — but leaves no margin.

The i6X is commonly unlocked to 10 channels via a firmware modification. Worth doing
before setup rather than after. Use **iBUS** from the iA6B into a Kakute UART.

### 6. UBEC is 6S-safe despite its listing title — **resolved, verify on unit**

The Amazon title says "2S 3S 4S", which would be a serious problem on a 6S pack. The
actual specification is **5.5–35 V input**, comfortably covering 6S at 25.2 V.

Confirm against the printing on the physical unit. Also verify under load: the Pi 4
draws up to ~3 A peak and browns out ungracefully. A mid-flight Pi reset costs the
imagery for that flight.

### 7. ArduPilot flash target — likely resolved

Holybro sells a "Kakute H7 v1.5 Stack", confirming **v1.5 is a revision of the
full-size Kakute H7 V1 family** — not the Mini. So:

- ArduPilot target: **`KakuteH7`**
- Blackbox/logging storage: **microSD card slot**

Version differences across the family:

| Board | Logging storage |
|---|---|
| Kakute H7 **V1.x** (incl. v1.5) | MicroSD card slot |
| Kakute H7 **V2.x** | Onboard 128 MB flash |
| Kakute H7 **Mini** | Onboard 128 Mbit flash |

Still worth a glance at the silkscreen before flashing — if it reads V2, the target is
`KakuteH7v2`.

---

## Supporting hardware — status

| Item | Status |
|---|---|
| microSD for the Pi | ✅ Have |
| GPS mast | ✅ Came with the M10 — confirm it is actually mounted on it |
| Nadir camera mount | ✅ 3D printed |
| Vibration damping | ✅ FC soft-mount grommets + TPU prints |
| Spare batteries | ❌ None. Acceptable for sites under ~20 acres — see Finding 4 |
| USB WiFi adapter | ❌ None. Using a phone hotspot instead — see Finding 1 |

**Free upgrade available:** since the camera mount is 3D printed, printing a second
mount angled 15–30° costs nothing but filament and would substantially improve 3D
reconstruction of vertical surfaces (SPEC §2.3). Worth having both and choosing per
mission — nadir for orthomosaics, angled for 3D models.
