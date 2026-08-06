"""Geofence: keep the aircraft inside the area you drew.

An inclusion fence written to the flight controller, so the limit is enforced by
the autopilot itself rather than by anything on the ground. If the aircraft
reaches the boundary it acts on its own, with no link required.

**The fence is the polygon you drew, exactly.** A boundary usually traces
something physical — a treeline, wires, a property edge — so it is reproduced
vertex for vertex, concavities included. Fitting a convex hull around it would
fill in any notch drawn to route around an obstacle and permit flying over the
very thing that was excluded.

Room for the turn is made by holding the *flight lines* inside the boundary
(``SurveyParams.keep_inside``), not by enlarging the fence. The aircraft
decelerates and turns short of the edge, so the overshoot lands inside the
boundary rather than beyond it.

:func:`build_fence_around_waypoints` remains for the opposite case — an area of
interest with open ground around it, where flying slightly outside is harmless.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from pymavlink import mavutil

from ..planning.geo import LatLon, LocalFrame

mavlink = mavutil.mavlink

#: Metres beyond the outermost waypoint. Has to cover the deceleration and turn
#: at the end of a flight line, which overshoots by roughly the stopping
#: distance at cruise speed.
DEFAULT_MARGIN_M = 40.0

#: FENCE_ACTION. 1 is RTL-or-land, the sane default: come home rather than
#: continue outside the boundary.
FENCE_ACTION_RTL = 1
#: FENCE_TYPE bit 2 enables polygon fences; bit 0 enables the altitude ceiling.
FENCE_TYPE_ALT_AND_POLYGON = 1 | 4

#: ArduPilot's fence point storage is finite. Well above any hand-drawn boundary,
#: but worth failing clearly rather than having the autopilot silently truncate.
MAX_FENCE_VERTICES = 70


@dataclass(frozen=True)
class Fence:
    """An inclusion boundary, as latitude/longitude vertices."""

    vertices: list[LatLon]
    max_altitude_m: float
    margin_m: float

    @property
    def vertex_count(self) -> int:
        return len(self.vertices)


def convex_hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Andrew's monotone chain hull, counter-clockwise.

    A hull rather than the drawn outline: a concave survey area would otherwise
    need polygon offsetting, which is fiddly and easy to get subtly wrong. A
    hull is more permissive than the drawn shape but still a hard boundary, and
    being slightly generous is the right way to err for a fence.
    """
    unique = sorted(set(points))
    if len(unique) <= 2:
        return unique

    def cross(o, a, b) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list[tuple[float, float]] = []
    for point in unique:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)

    upper: list[tuple[float, float]] = []
    for point in reversed(unique):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)

    return lower[:-1] + upper[:-1]


def build_fence(polygon: list[LatLon], max_altitude_m: float) -> Fence:
    """Fence the drawn boundary exactly, vertex for vertex.

    No hull, no expansion. If the boundary was drawn around obstacles, altering
    its shape defeats the purpose — a hull would fill in the notch drawn to
    avoid a tree and then permit flying over it.

    Room for the turn comes from insetting the flight lines instead; see
    ``SurveyParams.keep_inside``.
    """
    if len(polygon) < 3:
        raise ValueError("a fence needs at least 3 vertices")

    # ArduPilot stores a bounded number of fence points.
    if len(polygon) > MAX_FENCE_VERTICES:
        raise ValueError(
            f"fence has {len(polygon)} vertices; the autopilot holds at most "
            f"{MAX_FENCE_VERTICES}. Simplify the boundary."
        )

    return Fence(
        vertices=list(polygon),
        max_altitude_m=max_altitude_m,
        margin_m=0.0,
    )


def build_fence_around_waypoints(
    waypoints: list[LatLon],
    max_altitude_m: float,
    margin_m: float = DEFAULT_MARGIN_M,
) -> Fence:
    """A permissive fence enclosing every waypoint plus a margin.

    Convex, so it may cover ground the drawn boundary excluded. Only appropriate
    when the boundary marks an area of interest with clear space around it —
    never when it traces obstacles.
    """
    if len(waypoints) < 3:
        raise ValueError("a fence needs at least 3 waypoints to enclose")

    frame = LocalFrame.centred_on(waypoints)
    local = [frame.to_local(w) for w in waypoints]
    hull = convex_hull(local)

    if len(hull) < 3:
        raise ValueError("waypoints are collinear; cannot build a fence around them")

    centre_x = sum(x for x, _ in hull) / len(hull)
    centre_y = sum(y for _, y in hull) / len(hull)

    expanded = []
    for x, y in hull:
        dx, dy = x - centre_x, y - centre_y
        distance = (dx * dx + dy * dy) ** 0.5
        if distance == 0:
            expanded.append((x, y))
            continue
        # Push each vertex directly away from the centre. For a convex hull this
        # always encloses the original, which is the property that matters.
        scale = (distance + margin_m) / distance
        expanded.append((centre_x + dx * scale, centre_y + dy * scale))

    return Fence(
        vertices=[frame.to_latlon(p) for p in expanded],
        max_altitude_m=max_altitude_m,
        margin_m=margin_m,
    )


def upload_fence(link, fence: Fence, timeout: float = 30.0) -> None:
    """Write the fence to the flight controller using the mission protocol."""
    count = fence.vertex_count
    link.mav.mission_count_send(
        link.target_system, link.target_component, count, mavlink.MAV_MISSION_TYPE_FENCE
    )

    deadline = time.monotonic() + timeout
    remaining = set(range(count))

    while time.monotonic() < deadline:
        msg = link.recv_match(
            type=["MISSION_REQUEST", "MISSION_REQUEST_INT", "MISSION_ACK"],
            blocking=True,
            timeout=2.0,
        )
        if msg is None:
            continue
        if getattr(msg, "mission_type", mavlink.MAV_MISSION_TYPE_FENCE) != mavlink.MAV_MISSION_TYPE_FENCE:
            continue

        if msg.get_type() == "MISSION_ACK":
            if msg.type != mavlink.MAV_MISSION_ACCEPTED:
                raise RuntimeError(f"Fence rejected by autopilot: type {msg.type}")
            if remaining:
                raise RuntimeError(f"Autopilot acked before requesting {sorted(remaining)}")
            return

        lat, lon = fence.vertices[msg.seq]
        link.mav.mission_item_int_send(
            link.target_system, link.target_component, msg.seq,
            mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT,
            mavlink.MAV_CMD_NAV_FENCE_POLYGON_VERTEX_INCLUSION,
            0, 1,
            float(count), 0, 0, 0,       # param1 is the vertex count
            int(lat * 1e7), int(lon * 1e7), 0.0,
            mavlink.MAV_MISSION_TYPE_FENCE,
        )
        remaining.discard(msg.seq)

    raise TimeoutError("Timed out uploading fence")


def download_fence(link, timeout: float = 30.0) -> list[LatLon]:
    """Read back the fence the flight controller is actually holding."""
    link.mav.mission_request_list_send(
        link.target_system, link.target_component, mavlink.MAV_MISSION_TYPE_FENCE
    )

    count_msg = link.recv_match(type="MISSION_COUNT", blocking=True, timeout=timeout)
    if count_msg is None:
        raise TimeoutError("No MISSION_COUNT for the fence")

    vertices: list[LatLon] = []
    deadline = time.monotonic() + timeout

    for seq in range(count_msg.count):
        link.mav.mission_request_int_send(
            link.target_system, link.target_component, seq,
            mavlink.MAV_MISSION_TYPE_FENCE,
        )
        while time.monotonic() < deadline:
            msg = link.recv_match(type="MISSION_ITEM_INT", blocking=True, timeout=2.0)
            if msg is not None and msg.seq == seq:
                vertices.append((msg.x / 1e7, msg.y / 1e7))
                break
        else:
            raise TimeoutError(f"Timed out waiting for fence vertex {seq}")

    link.mav.mission_ack_send(
        link.target_system, link.target_component,
        mavlink.MAV_MISSION_ACCEPTED, mavlink.MAV_MISSION_TYPE_FENCE,
    )
    return vertices


def diff_fence(
    sent: Fence, read_back: list[LatLon], tolerance_deg7: int = 2
) -> list[str]:
    """Compare an uploaded fence against what the autopilot stored."""
    problems: list[str] = []

    if len(read_back) != sent.vertex_count:
        problems.append(
            f"Vertex count: sent {sent.vertex_count}, autopilot has {len(read_back)}"
        )

    for index, (sent_point, stored) in enumerate(zip(sent.vertices, read_back)):
        if (
            abs(int(sent_point[0] * 1e7) - int(stored[0] * 1e7)) > tolerance_deg7
            or abs(int(sent_point[1] * 1e7) - int(stored[1] * 1e7)) > tolerance_deg7
        ):
            problems.append(
                f"Vertex {index}: sent {sent_point[0]:.7f},{sent_point[1]:.7f}, "
                f"stored {stored[0]:.7f},{stored[1]:.7f}"
            )

    return problems


def contains(fence: Fence, point: LatLon) -> bool:
    """Is a position inside the fence? Ray casting in the local frame."""
    frame = LocalFrame.centred_on(fence.vertices)
    polygon = [frame.to_local(v) for v in fence.vertices]
    x, y = frame.to_local(point)

    inside = False
    count = len(polygon)
    for i in range(count):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % count]
        if (y1 > y) != (y2 > y):
            crossing_x = x1 + (y - y1) / (y2 - y1) * (x2 - x1)
            if x < crossing_x:
                inside = not inside
    return inside
