"""Flight-log analysis: find out why the aircraft misbehaved.

Reads an ArduPilot dataflash log (``.BIN``) and reports on the things that
actually cause poor position hold — vibration, compass interference, GPS
quality, EKF health, and persistent attitude bias.

This exists because "it drifts in position hold" has several possible causes
that look identical from the pilot's seat, and guessing between them wastes
flights. Each is distinguishable in the log:

- **Vibration** feeds noise into the accelerometers, which corrupts the EKF's
  velocity estimate and makes the aircraft wander.
- **Compass interference** scaling with motor current makes heading wrong in
  proportion to throttle. The signature is a correlation between magnetic field
  magnitude and current draw.
- **Persistent attitude bias** in slow flight suggests the flight controller's
  idea of level is tilted — a crooked mount or unevenly compressed damping. No
  amount of compass calibration fixes it.
- **GPS quality** simply being poor.

Nothing here changes how the aircraft flies. It tells you where to look.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from pathlib import Path

#: ArduPilot's own guidance: below 30 m/s/s is healthy, 30-60 is marginal, and
#: above 60 causes problems. Clipping events mean the accelerometer saturated.
VIBE_GOOD = 30.0
VIBE_MARGINAL = 60.0

#: Correlation between magnetic field magnitude and current draw. Above this,
#: motor current is measurably corrupting the compass.
MAG_CURRENT_CORRELATION_WARN = 0.5

#: Degrees of average roll/pitch in slow flight before a mount is suspected.
ATTITUDE_BIAS_WARN = 2.0

#: Ground speed below which the aircraft counts as hovering. A multirotor tilts
#: to move, so attitude bias is only meaningful when it is not going anywhere.
SLOW_FLIGHT_MS = 1.5

SEVERITY_ORDER = {"problem": 0, "warning": 1, "ok": 2, "unknown": 3}


@dataclass
class Finding:
    category: str
    severity: str
    headline: str
    detail: str

    @property
    def is_problem(self) -> bool:
        return self.severity == "problem"


@dataclass
class LogReport:
    path: str
    duration_s: float = 0.0
    message_counts: dict[str, int] = field(default_factory=dict)
    modes: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def problems(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "problem"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    def sorted_findings(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: SEVERITY_ORDER.get(f.severity, 9))

    def summary(self) -> str:
        if self.problems:
            return f"{len(self.problems)} problem(s), {len(self.warnings)} warning(s)"
        if self.warnings:
            return f"No problems found, {len(self.warnings)} warning(s)"
        return "Nothing wrong found"


#: Log message types the analysis needs. Filtering keeps a large log fast.
WANTED = ["VIBE", "MAG", "MAG2", "MAG3", "ATT", "BAT", "CURR", "GPS", "MODE",
          "XKF4", "NKF4", "POS", "CTUN"]


def read_log(path: str | Path, max_records: int | None = None) -> dict[str, list]:
    """Collect the messages the analysis needs, in one pass."""
    from pymavlink import mavutil

    log = mavutil.mavlink_connection(str(path))
    collected: dict[str, list] = {name: [] for name in WANTED}
    count = 0

    while True:
        msg = log.recv_match(type=WANTED)
        if msg is None:
            break
        collected[msg.get_type()].append(msg)
        count += 1
        if max_records and count >= max_records:
            break

    return collected


def analyse_log(path: str | Path, max_records: int | None = None) -> LogReport:
    """Analyse a dataflash log and report what is worth investigating."""
    records = read_log(path, max_records)
    report = LogReport(path=str(path))
    report.message_counts = {k: len(v) for k, v in records.items() if v}

    if not any(records.values()):
        report.findings.append(
            Finding("log", "problem", "Nothing readable in this log",
                    "No recognised messages. Is this an ArduPilot .BIN dataflash log?")
        )
        return report

    _summarise_flight(records, report)
    report.findings.append(_check_vibration(records))
    report.findings.append(_check_compass(records))
    report.findings.append(_check_compass_interference(records))
    report.findings.append(_check_gps(records))
    report.findings.append(_check_attitude_bias(records))
    report.findings.append(_check_magfit_readiness(records))

    return report


def _summarise_flight(records: dict[str, list], report: LogReport) -> None:
    times = [m.TimeUS for m in records.get("ATT", []) if hasattr(m, "TimeUS")]
    if times:
        report.duration_s = (max(times) - min(times)) / 1e6

    seen: list[str] = []
    for msg in records.get("MODE", []):
        name = getattr(msg, "Mode", None)
        name = str(name) if name is not None else None
        if name and (not seen or seen[-1] != name):
            seen.append(name)
    report.modes = seen


def _check_vibration(records: dict[str, list]) -> Finding:
    vibe = records.get("VIBE", [])
    if not vibe:
        return Finding("vibration", "unknown", "No vibration data",
                       "This log contains no VIBE messages.")

    axes = {}
    for axis in ("VibeX", "VibeY", "VibeZ"):
        values = [getattr(m, axis) for m in vibe if hasattr(m, axis)]
        if values:
            axes[axis] = (statistics.mean(values), max(values))

    if not axes:
        return Finding("vibration", "unknown", "No vibration data", "VIBE present but empty.")

    worst_mean = max(mean for mean, _ in axes.values())
    worst_peak = max(peak for _, peak in axes.values())

    clips = 0
    for name in ("Clip0", "Clip1", "Clip2"):
        values = [getattr(m, name) for m in vibe if hasattr(m, name)]
        if values:
            clips += max(values) - min(values)

    detail = ", ".join(
        f"{a.replace('Vibe', '')}: {mean:.1f} avg / {peak:.1f} peak"
        for a, (mean, peak) in axes.items()
    ) + f"; {clips} clipping events"

    if worst_mean > VIBE_MARGINAL or clips > 0:
        return Finding(
            "vibration", "problem", "Vibration is high enough to cause problems",
            detail + ". Above 60 m/s/s, or any clipping, corrupts the EKF's velocity "
            "estimate and makes the aircraft wander. Check prop balance, motor "
            "bearings, and that the flight controller's soft mounts are intact.",
        )
    if worst_mean > VIBE_GOOD or worst_peak > VIBE_MARGINAL:
        return Finding(
            "vibration", "warning", "Vibration is marginal",
            detail + ". Under 30 m/s/s is healthy; this is above that but not yet "
            "severe. Worth balancing props before it gets worse.",
        )
    return Finding("vibration", "ok", "Vibration is healthy", detail)


def _magnitudes(messages: list) -> list[float]:
    out = []
    for m in messages:
        try:
            out.append((m.MagX**2 + m.MagY**2 + m.MagZ**2) ** 0.5)
        except AttributeError:
            continue
    return out


def _check_compass(records: dict[str, list]) -> Finding:
    fields = {
        name: _magnitudes(records.get(name, []))
        for name in ("MAG", "MAG2", "MAG3")
        if records.get(name)
    }
    fields = {k: v for k, v in fields.items() if v}

    if not fields:
        return Finding("compass", "unknown", "No compass data",
                       "This log contains no MAG messages.")

    primary = next(iter(fields.values()))
    mean = statistics.mean(primary)
    spread = statistics.pstdev(primary) if len(primary) > 1 else 0.0
    variation = spread / mean if mean else 0.0

    detail = (
        f"Field magnitude {mean:.0f} mGauss, varying {variation:.0%} over the flight"
    )

    if len(fields) > 1:
        means = [statistics.mean(v) for v in fields.values()]
        disagreement = (max(means) - min(means)) / statistics.mean(means)
        detail += f"; {len(fields)} compasses disagree by {disagreement:.0%}"
        if disagreement > 0.25:
            return Finding(
                "compass", "problem", "Compasses disagree with each other",
                detail + ". One of them is badly calibrated or sitting in a "
                "magnetic field. Check which is primary and consider disabling "
                "the worst.",
            )

    # A well-calibrated compass in a clean installation sees a field magnitude
    # that barely changes: the Earth's field is constant, and only the vehicle's
    # own interference makes it move.
    if variation > 0.35:
        return Finding(
            "compass", "problem", "Compass field magnitude is unstable",
            detail + ". The Earth's field does not change during a flight, so this "
            "is interference or bad calibration. Run MAGFit on this log.",
        )
    if variation > 0.20:
        return Finding(
            "compass", "warning", "Compass field magnitude moves more than ideal",
            detail + ". Worth running MAGFit to refine the calibration.",
        )
    return Finding("compass", "ok", "Compass looks well calibrated", detail)


def _check_compass_interference(records: dict[str, list]) -> Finding:
    """Detect compass error that scales with motor current.

    The signature of ESC or power-wiring interference, and the reason a compass
    can calibrate perfectly on the bench and still be wrong in the air.
    """
    mags = records.get("MAG", [])
    currents = records.get("BAT", []) or records.get("CURR", [])
    if not mags or not currents:
        return Finding("interference", "unknown", "Cannot check motor interference",
                       "Needs both MAG and battery-current messages.")

    current_values = []
    for m in currents:
        value = getattr(m, "Curr", None)
        if value is not None:
            current_values.append((m.TimeUS, value))
    magnitudes = [(m.TimeUS, v) for m, v in zip(mags, _magnitudes(mags))]

    if len(current_values) < 20 or len(magnitudes) < 20:
        return Finding("interference", "unknown", "Not enough data to check interference",
                       f"{len(magnitudes)} compass and {len(current_values)} current samples.")

    # Resample the compass series onto the current timestamps, nearest-neighbour.
    paired_mag, paired_current = [], []
    index = 0
    for timestamp, current in current_values:
        while index + 1 < len(magnitudes) and magnitudes[index + 1][0] < timestamp:
            index += 1
        paired_mag.append(magnitudes[index][1])
        paired_current.append(current)

    if len(set(paired_current)) < 2 or len(set(paired_mag)) < 2:
        return Finding("interference", "unknown", "Cannot check motor interference",
                       "Current or field magnitude never varied.")

    correlation = abs(statistics.correlation(paired_current, paired_mag))
    detail = f"Correlation between current draw and field magnitude: {correlation:.2f}"

    if correlation > MAG_CURRENT_CORRELATION_WARN:
        return Finding(
            "interference", "problem", "Motor current is corrupting the compass",
            detail + ". Heading error will scale with throttle, which calibrating on "
            "the bench cannot fix. Move the compass further from the ESC and power "
            "leads, twist the battery leads, and run MAGFit with motor compensation.",
        )
    return Finding("interference", "ok", "No sign of motor interference", detail)


def _check_gps(records: dict[str, list]) -> Finding:
    gps = records.get("GPS", [])
    if not gps:
        return Finding("gps", "unknown", "No GPS data", "This log contains no GPS messages.")

    sats = [m.NSats for m in gps if hasattr(m, "NSats")]
    hdops = [m.HDop for m in gps if hasattr(m, "HDop")]
    if not sats:
        return Finding("gps", "unknown", "No satellite counts", "GPS present but incomplete.")

    detail = (
        f"Satellites {min(sats)}-{max(sats)} (median {statistics.median(sats):.0f})"
        + (f", HDOP median {statistics.median(hdops):.1f}, worst {max(hdops):.1f}"
           if hdops else "")
    )

    if min(sats) < 6 or (hdops and max(hdops) > 3.0):
        return Finding("gps", "problem", "GPS reception dropped to unusable levels", detail)
    if statistics.median(sats) < 10 or (hdops and statistics.median(hdops) > 1.5):
        return Finding("gps", "warning", "GPS reception is mediocre", detail)
    return Finding("gps", "ok", "GPS reception is good", detail)


def _check_attitude_bias(records: dict[str, list]) -> Finding:
    """Look for the aircraft persistently holding a tilt.

    A flight controller mounted a degree or two off level believes it is flat
    while the airframe is not, so the aircraft accelerates gently sideways and
    the pilot sees a steady drift in position hold. Calibration cannot correct
    a physically crooked board.
    """
    att = records.get("ATT", [])
    if not att:
        return Finding("attitude", "unknown", "No attitude data",
                       "This log contains no ATT messages.")

    # Only slow flight is meaningful. A multirotor pitches forward to move, so
    # averaging over a whole survey shows a large "bias" that is simply cruise.
    speeds = [
        (m.TimeUS, m.Spd) for m in records.get("GPS", []) if hasattr(m, "Spd")
    ]
    if not speeds:
        return Finding(
            "attitude", "unknown", "Cannot separate hover from cruise",
            "No GPS speed in this log, so forward flight cannot be excluded and "
            "any attitude average would be meaningless.",
        )

    slow: list[tuple[float, float]] = []
    index = 0
    for msg in att:
        if not (hasattr(msg, "Roll") and hasattr(msg, "Pitch")):
            continue
        while index + 1 < len(speeds) and speeds[index + 1][0] < msg.TimeUS:
            index += 1
        if speeds[index][1] < SLOW_FLIGHT_MS:
            slow.append((msg.Roll, msg.Pitch))

    if len(slow) < 50:
        return Finding(
            "attitude", "unknown", "Not enough slow flight to judge",
            f"Only {len(slow)} samples below {SLOW_FLIGHT_MS:g} m/s. Hover for "
            "30 seconds in still air to make this check meaningful.",
        )

    mean_roll = statistics.mean(r for r, _ in slow)
    mean_pitch = statistics.mean(p for _, p in slow)
    detail = (
        f"Average attitude below {SLOW_FLIGHT_MS:g} m/s: roll {mean_roll:+.1f}°, "
        f"pitch {mean_pitch:+.1f}° over {len(slow)} samples"
    )

    worst = max(abs(mean_roll), abs(mean_pitch))
    if worst > ATTITUDE_BIAS_WARN:
        return Finding(
            "attitude", "warning", "The aircraft held a consistent tilt",
            detail + ". Wind explains this too, so it is not conclusive. But if it "
            "recurs in still air, suspect the flight controller mount: check it sits "
            "flat and its damping is not compressed unevenly, then run Calibrate "
            "Level. A crooked board cannot be calibrated straight.",
        )
    return Finding("attitude", "ok", "No persistent attitude bias", detail)


def _check_magfit_readiness(records: dict[str, list]) -> Finding:
    """MAGFit needs compass, position, and attitude to fit against the World
    Magnetic Model. Say plainly whether this log can be used."""
    have_mag = bool(records.get("MAG"))
    have_pos = bool(records.get("POS") or records.get("GPS"))
    have_att = bool(records.get("ATT"))

    missing = [
        name
        for name, present in (("compass", have_mag), ("position", have_pos), ("attitude", have_att))
        if not present
    ]
    if missing:
        return Finding(
            "magfit", "warning", "This log cannot be used for MAGFit",
            f"Missing {', '.join(missing)} data.",
        )
    return Finding(
        "magfit", "ok", "This log can be used for MAGFit",
        "Upload it to https://firmware.ardupilot.org/Tools/WebTools/MAGFit/ to "
        "compute compass calibration from real flight data instead of rotating "
        "the airframe by hand. Fly with plenty of heading variation for the best fit.",
    )
