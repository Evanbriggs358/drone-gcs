"""Survey grid generation.

Turns a survey polygon plus camera and overlap parameters into the flight lines,
waypoints, and predicted photo positions for a mapping mission.

The mission uploaded to the flight controller contains only the **endpoints of
each flight line** — two waypoints per line. Shutter releases are handled by
ArduPilot's ``CAM_TRIGG_DIST``, which fires every N metres of travel. This keeps
the waypoint count low enough to fit comfortably in flight-controller storage
even for large surveys, where one waypoint per photo would not.

Predicted photo positions are still computed here, because they drive flight-time
estimates and the pre-flight coverage preview.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from math import atan2, ceil, cos, degrees, hypot, pi, radians, sin

from .camera import Camera
from .geo import LatLon, LocalFrame, polygon_area_m2, rotate
from .power import Airframe, Battery

Point = tuple[float, float]


class Pattern(str, Enum):
    """Flight pattern. Drives reconstruction quality — see SPEC.md §2.3."""

    #: Single set of parallel lines. Best orthomosaic per minute of flight.
    NADIR = "nadir"
    #: Two perpendicular passes. Roughly double the flight time, dramatically
    #: better 3D reconstruction of vertical surfaces.
    CROSSHATCH = "crosshatch"


@dataclass(frozen=True)
class SurveyParams:
    altitude_m: float
    front_overlap: float = 0.70
    side_overlap: float = 0.70
    ground_speed_ms: float = 8.0
    #: Compass bearing of the flight lines, degrees clockwise from north.
    #: Ignored when ``auto_heading`` is set.
    heading_deg: float = 0.0
    #: Automatically choose the heading that aligns lines with the polygon's
    #: longest axis, minimising the weak edge strips.
    auto_heading: bool = False
    pattern: Pattern = Pattern.NADIR

    #: Keep the entire flight inside the drawn boundary, including the overshoot
    #: when the aircraft decelerates into a turn.
    #:
    #: Default because a drawn boundary usually means something physical — trees,
    #: wires, a property line — and flying outside it is not a data-quality
    #: question but a crash. The cost is thinner coverage at the very edge, since
    #: the outermost photos then have neighbours on one side only.
    #:
    #: Set False only when the boundary marks an area of interest with open space
    #: around it, and better edge reconstruction is worth flying beyond it.
    keep_inside: bool = True

    #: How far to extend each line past the boundary. Ignored when
    #: ``keep_inside`` is set, which insets instead.
    edge_margin_m: float = 15.0

    #: Deceleration the aircraft achieves entering a turn. ArduPilot's default
    #: waypoint acceleration is around 2.5 m/s^2.
    turn_accel_ms2: float = 2.5
    #: Radius at which the autopilot considers a waypoint reached and begins
    #: turning, so the aircraft carries on this far past it.
    waypoint_radius_m: float = 2.0
    #: Seconds lost decelerating, turning, and accelerating at each line end.
    turn_penalty_s: float = 6.0

    @property
    def turn_overshoot_m(self) -> float:
        """How far past a line-end waypoint the aircraft actually travels.

        Stopping distance at cruise plus the waypoint acceptance radius. At
        8 m/s this is around 15 m; at 17 m/s nearer 60. Flight lines are held
        this far inside the boundary so the overshoot lands on it rather than
        beyond it.
        """
        return (
            self.ground_speed_ms**2 / (2.0 * self.turn_accel_ms2)
            + self.waypoint_radius_m
        )

    @property
    def effective_margin_m(self) -> float:
        """Signed distance applied to each line end. Negative shortens."""
        return -self.turn_overshoot_m if self.keep_inside else self.edge_margin_m


@dataclass(frozen=True)
class Waypoint:
    lat: float
    lon: float
    alt_m: float


@dataclass
class SurveyPlan:
    waypoints: list[Waypoint]
    photo_points: list[LatLon]
    line_count: int
    path_length_m: float
    duration_s: float
    area_m2: float
    gsd_cm_per_px: float
    photo_spacing_m: float
    line_spacing_m: float
    #: The heading actually used, which may differ from the requested one when
    #: auto_heading chose it.
    resolved_heading_deg: float = 0.0
    warnings: list[str] = field(default_factory=list)

    #: Endurance at the planned survey speed, when an aircraft and battery were
    #: supplied. None means no endurance analysis was requested.
    endurance_min: float | None = None
    #: Batteries the mission needs. Above 1.0 means a pack change mid-survey.
    batteries_needed: float | None = None
    #: Speed that would cover the most ground per battery, camera permitting.
    best_survey_speed_ms: float | None = None

    @property
    def photo_count(self) -> int:
        return len(self.photo_points)

    @property
    def area_acres(self) -> float:
        return self.area_m2 / 4046.856

    @property
    def duration_min(self) -> float:
        return self.duration_s / 60.0


def plan_survey(
    polygon: list[LatLon],
    camera: Camera,
    params: SurveyParams,
    airframe: Airframe | None = None,
    battery: Battery | None = None,
) -> SurveyPlan:
    """Build a complete survey plan for a polygon.

    ``polygon`` is an unclosed ring of (lat, lon) vertices, in any winding order.

    Supplying ``airframe`` and ``battery`` adds endurance analysis: how many
    packs the mission needs, and what speed would cover the most ground per
    pack. Without them the plan still reports duration, just not whether the
    aircraft can actually stay up that long.
    """
    if len(polygon) < 3:
        raise ValueError("a survey polygon needs at least 3 vertices")

    frame = LocalFrame.centred_on(polygon)
    local = [frame.to_local(p) for p in polygon]

    line_spacing = camera.line_spacing_m(params.altitude_m, params.side_overlap)
    photo_spacing = camera.photo_spacing_m(params.altitude_m, params.front_overlap)

    heading = (
        optimal_heading_deg(local, line_spacing, params.effective_margin_m)
        if params.auto_heading
        else params.heading_deg
    )
    headings = [heading]
    if params.pattern is Pattern.CROSSHATCH:
        headings.append(heading + 90.0)

    all_lines: list[tuple[Point, Point]] = []
    for heading in headings:
        all_lines.extend(
            _flight_lines(local, heading, line_spacing, params.effective_margin_m)
        )

    waypoints: list[Waypoint] = []
    photo_points: list[LatLon] = []
    for start, end in all_lines:
        for pt in (start, end):
            lat, lon = frame.to_latlon(pt)
            waypoints.append(Waypoint(lat, lon, params.altitude_m))
        photo_points.extend(
            frame.to_latlon(p) for p in _points_along(start, end, photo_spacing)
        )

    path_length = _path_length(all_lines)
    duration = (
        path_length / params.ground_speed_ms
        + len(all_lines) * params.turn_penalty_s
    )

    warnings = _check(camera, params, photo_spacing)

    endurance = batteries = best_speed = None
    if airframe is not None and battery is not None:
        endurance = airframe.endurance_min(battery, params.ground_speed_ms)
        batteries = (duration / 60.0) / endurance

        shutter_limit = camera.max_ground_speed_ms(
            params.altitude_m, params.front_overlap
        )
        best_speed = min(airframe.best_range_speed_ms(), shutter_limit)

        warnings.extend(
            _check_endurance(params, endurance, batteries, best_speed, airframe, battery)
        )

    return SurveyPlan(
        waypoints=waypoints,
        photo_points=photo_points,
        line_count=len(all_lines),
        path_length_m=path_length,
        duration_s=duration,
        area_m2=polygon_area_m2(local),
        gsd_cm_per_px=camera.gsd_cm_per_px(params.altitude_m),
        photo_spacing_m=photo_spacing,
        line_spacing_m=line_spacing,
        resolved_heading_deg=heading,
        warnings=warnings,
        endurance_min=endurance,
        batteries_needed=batteries,
        best_survey_speed_ms=best_speed,
    )


def _check_endurance(
    params: SurveyParams,
    endurance_min: float,
    batteries: float,
    best_speed: float,
    airframe: Airframe,
    battery: Battery,
) -> list[str]:
    """Flag missions the aircraft cannot actually complete, and speeds that
    waste endurance."""
    warnings: list[str] = []

    if batteries > 1.0:
        warnings.append(
            f"Mission needs about {batteries:.1f} batteries: "
            f"{params.ground_speed_ms:.0f} m/s gives {endurance_min:.0f} min of "
            f"flight and the survey takes longer. Shrink the area, raise the "
            f"altitude, or plan a pack change."
        )
    elif batteries > 0.8:
        warnings.append(
            f"Little margin: the survey uses about {batteries:.0%} of a battery. "
            "Wind or a cold pack could leave you short."
        )

    # Only worth mentioning if the gain is real rather than rounding.
    gain = airframe.range_km(battery, best_speed) / max(
        airframe.range_km(battery, params.ground_speed_ms), 1e-6
    )
    if gain > 1.10:
        warnings.append(
            f"Flying at {best_speed:.0f} m/s instead of "
            f"{params.ground_speed_ms:.0f} m/s would cover about {gain - 1:.0%} "
            "more ground per battery."
        )

    return warnings


def _check(camera: Camera, params: SurveyParams, photo_spacing: float) -> list[str]:
    """Flag parameter combinations that will quietly produce a bad survey."""
    warnings: list[str] = []

    max_speed = camera.max_ground_speed_ms(params.altitude_m, params.front_overlap)
    if params.ground_speed_ms > max_speed:
        warnings.append(
            f"Ground speed {params.ground_speed_ms:.1f} m/s outruns the shutter "
            f"(max {max_speed:.1f} m/s at this altitude and overlap). Front overlap "
            f"will fall below the requested {params.front_overlap:.0%}."
        )

    if params.pattern is Pattern.NADIR:
        warnings.append(
            "Nadir-only grid: good orthomosaic, weak 3D reconstruction of vertical "
            "surfaces. Use crosshatch if a 3D model is the goal."
        )

    if params.front_overlap < 0.60 or params.side_overlap < 0.60:
        warnings.append(
            "Overlap below 60% frequently causes reconstruction holes over "
            "low-texture ground such as grass, water, or fresh asphalt."
        )

    if params.keep_inside:
        warnings.append(
            f"Staying inside the boundary: flight lines stop "
            f"{params.turn_overshoot_m:.0f} m short of the edge so the turn "
            f"overshoot lands inside it. Coverage thins at the very perimeter."
        )
        # The inset grows with the square of speed, so a fast survey of a small
        # area can lose most of it.
        if params.turn_overshoot_m > 40.0:
            warnings.append(
                f"At {params.ground_speed_ms:.0f} m/s the aircraft needs "
                f"{params.turn_overshoot_m:.0f} m to turn around, so a wide "
                "strip of the boundary goes unflown. Slow down to reclaim it."
            )
    elif params.edge_margin_m > 0:
        warnings.append(
            f"Flight lines extend {params.edge_margin_m:.0f} m BEYOND the drawn "
            "boundary for better edge coverage. Only safe if there is clear "
            "space around the area."
        )

    return warnings


def _flight_lines(
    polygon: list[Point], heading_deg: float, spacing: float, margin: float
) -> list[tuple[Point, Point]]:
    """Parallel lines at ``heading_deg`` clipped to the polygon, in serpentine order.

    Works by rotating the polygon so the lines become horizontal, sweeping
    horizontal scanlines across it, then rotating the results back.
    """
    angle = radians(heading_deg)
    rotated = [rotate(p, -angle) for p in polygon]

    ys = [p[1] for p in rotated]
    y_min, y_max = min(ys), max(ys)

    lines: list[tuple[Point, Point]] = []
    # Start half a spacing in, so the covered strip is centred on the polygon.
    y = y_min + spacing / 2.0
    flip = False
    while y <= y_max:
        for x0, x1 in _scanline_spans(rotated, y):
            start, end = x0 - margin, x1 + margin
            # A negative margin shortens the line. Where the polygon is narrower
            # than twice the inset the line disappears entirely — correct, since
            # there is no room to fly it and still turn inside the boundary.
            if end - start < 1.0:
                continue
            a, b = (start, y), (end, y)
            if flip:
                a, b = b, a
            lines.append((rotate(a, angle), rotate(b, angle)))
        flip = not flip
        y += spacing

    return lines


def _scanline_spans(polygon: list[Point], y: float) -> list[tuple[float, float]]:
    """X-intervals where the horizontal line at ``y`` lies inside the polygon.

    Handles concave polygons, which produce more than one span per scanline.
    """
    crossings: list[float] = []
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if y1 == y2:
            continue  # horizontal edge contributes no crossing
        # Half-open test, so a vertex shared by two edges is counted once.
        if (y1 <= y < y2) or (y2 <= y < y1):
            crossings.append(x1 + (y - y1) / (y2 - y1) * (x2 - x1))

    crossings.sort()
    return [
        (crossings[i], crossings[i + 1])
        for i in range(0, len(crossings) - 1, 2)
        if crossings[i + 1] > crossings[i]
    ]


def _points_along(start: Point, end: Point, spacing: float) -> list[Point]:
    """Evenly spaced points from ``start`` to ``end`` inclusive of the start."""
    length = hypot(end[0] - start[0], end[1] - start[1])
    if length == 0 or spacing <= 0:
        return [start]

    steps = int(ceil(length / spacing))
    bearing = atan2(end[1] - start[1], end[0] - start[0])
    dx, dy = cos(bearing) * spacing, sin(bearing) * spacing
    return [(start[0] + dx * i, start[1] + dy * i) for i in range(steps + 1)]


def _path_length(lines: list[tuple[Point, Point]]) -> float:
    """Total flown distance: along every line, plus the transits between them."""
    total = 0.0
    previous_end: Point | None = None
    for start, end in lines:
        if previous_end is not None:
            total += hypot(start[0] - previous_end[0], start[1] - previous_end[1])
        total += hypot(end[0] - start[0], end[1] - start[1])
        previous_end = end
    return total


def optimal_heading_deg(
    polygon: list[Point], line_spacing: float, margin: float
) -> float:
    """Heading that maximises total flight-line coverage after the inset.

    Tries every integer heading from 0 to 179 and picks the one where the
    actual flight lines — clipped to the polygon and shortened by the margin —
    cover the most total length.  Total covered area is proportional to total
    line length times line spacing, so maximising line length maximises
    coverage.
    """
    best_heading = 0.0
    best_length = -1.0

    for deg in range(180):
        lines = _flight_lines(polygon, float(deg), line_spacing, margin)
        total = sum(
            hypot(b[0] - a[0], b[1] - a[1]) for a, b in lines
        )
        if total > best_length:
            best_length = total
            best_heading = float(deg)

    return best_heading
