"""Mission construction, upload, and verification.

Turns a :class:`~gcs.planning.grid.SurveyPlan` into an ArduPilot AUTO mission and
puts it on the flight controller.

The mission is deliberately shaped so the **flight controller owns the whole
flight** (SPEC §2.1). Camera triggering is a ``DO_SET_CAM_TRIGG_DIST`` command
inside the mission rather than something the companion computer drives, so losing
the WiFi link costs the live map and nothing else.

Uploads are always verified by reading the mission back and diffing it. A silent
"ack" is not evidence that the flight controller stored what you meant.
"""

from __future__ import annotations

from dataclasses import dataclass

from pymavlink import mavutil

from ..planning.grid import SurveyPlan

mavlink = mavutil.mavlink

#: Altitudes are relative to the launch point, not to sea level.
FRAME_RELATIVE = mavlink.MAV_FRAME_GLOBAL_RELATIVE_ALT_INT

CMD_WAYPOINT = mavlink.MAV_CMD_NAV_WAYPOINT
CMD_TAKEOFF = mavlink.MAV_CMD_NAV_TAKEOFF
CMD_RTL = mavlink.MAV_CMD_NAV_RETURN_TO_LAUNCH
CMD_CAM_TRIGG_DIST = mavlink.MAV_CMD_DO_SET_CAM_TRIGG_DIST


@dataclass(frozen=True)
class MissionItem:
    """One mission command, in the units MAVLink actually transmits."""

    seq: int
    command: int
    #: Degrees * 1e7, as MISSION_ITEM_INT carries them.
    x: int = 0
    y: int = 0
    #: Metres, relative to launch.
    z: float = 0.0
    param1: float = 0.0
    param2: float = 0.0
    param3: float = 0.0
    param4: float = 0.0
    frame: int = FRAME_RELATIVE

    @property
    def lat(self) -> float:
        return self.x / 1e7

    @property
    def lon(self) -> float:
        return self.y / 1e7

    def describe(self) -> str:
        name = _COMMAND_NAMES.get(self.command, f"CMD_{self.command}")
        if self.command == CMD_CAM_TRIGG_DIST:
            detail = f"every {self.param1:.1f} m" if self.param1 else "off"
            return f"{self.seq:3d} {name} ({detail})"
        if self.command == CMD_RTL:
            return f"{self.seq:3d} {name}"
        if self.command == CMD_TAKEOFF:
            return f"{self.seq:3d} {name} to {self.z:.0f} m"
        return f"{self.seq:3d} {name} {self.lat:.7f},{self.lon:.7f} {self.z:.0f} m"


_COMMAND_NAMES = {
    CMD_WAYPOINT: "WAYPOINT",
    CMD_TAKEOFF: "TAKEOFF",
    CMD_RTL: "RTL",
    CMD_CAM_TRIGG_DIST: "CAM_TRIGG_DIST",
}


def build_mission(
    plan: SurveyPlan,
    *,
    takeoff_alt_m: float | None = None,
    trigger_distance_m: float | None = None,
    home: tuple[float, float] | None = None,
) -> list[MissionItem]:
    """Assemble a complete AUTO mission from a survey plan.

    Structure::

        0  home placeholder      (ArduPilot overwrites seq 0 with actual home)
        1  TAKEOFF               climb to survey altitude
        2  CAM_TRIGG_DIST        start shooting every N metres
        3+ WAYPOINT ...          the survey grid, two per flight line
        n  CAM_TRIGG_DIST 0      stop shooting
        n+1 RTL                  return and land

    ``trigger_distance_m`` defaults to the plan's computed photo spacing, which is
    the entire point of computing it — the old system used a hardcoded 10 m.
    """
    if not plan.waypoints:
        raise ValueError("cannot build a mission from a plan with no waypoints")

    altitude = takeoff_alt_m if takeoff_alt_m is not None else plan.waypoints[0].alt_m
    trigger = (
        trigger_distance_m if trigger_distance_m is not None else plan.photo_spacing_m
    )
    home_lat, home_lon = home if home else (plan.waypoints[0].lat, plan.waypoints[0].lon)

    items: list[MissionItem] = []

    def add(command: int, **kwargs) -> None:
        items.append(MissionItem(seq=len(items), command=command, **kwargs))

    # Seq 0 is the home slot. ArduPilot replaces it with the real home position
    # on upload, but the protocol requires the entry to exist.
    add(CMD_WAYPOINT, x=int(home_lat * 1e7), y=int(home_lon * 1e7), z=0.0)

    add(CMD_TAKEOFF, x=int(home_lat * 1e7), y=int(home_lon * 1e7), z=altitude)
    add(CMD_CAM_TRIGG_DIST, param1=trigger)

    for wp in plan.waypoints:
        add(CMD_WAYPOINT, x=int(wp.lat * 1e7), y=int(wp.lon * 1e7), z=wp.alt_m)

    add(CMD_CAM_TRIGG_DIST, param1=0.0)
    add(CMD_RTL)

    return items


def upload_mission(link, items: list[MissionItem], timeout: float = 30.0) -> None:
    """Push a mission to the flight controller using the MAVLink mission protocol.

    Raises on timeout or a non-accepted ack. Does **not** verify contents — call
    :func:`verify_mission` for that.
    """
    import time

    link.mav.mission_count_send(
        link.target_system, link.target_component, len(items), mavlink.MAV_MISSION_TYPE_MISSION
    )

    deadline = time.monotonic() + timeout
    remaining = {item.seq for item in items}

    while time.monotonic() < deadline:
        msg = link.recv_match(
            type=["MISSION_REQUEST", "MISSION_REQUEST_INT", "MISSION_ACK"],
            blocking=True,
            timeout=2.0,
        )
        if msg is None:
            continue

        if msg.get_type() == "MISSION_ACK":
            if msg.type != mavlink.MAV_MISSION_ACCEPTED:
                raise RuntimeError(f"Mission rejected by autopilot: type {msg.type}")
            if remaining:
                raise RuntimeError(
                    f"Autopilot acked before requesting items {sorted(remaining)}"
                )
            return

        item = items[msg.seq]
        link.mav.mission_item_int_send(
            link.target_system, link.target_component, item.seq, item.frame,
            item.command, 0, 1,
            item.param1, item.param2, item.param3, item.param4,
            item.x, item.y, item.z, mavlink.MAV_MISSION_TYPE_MISSION,
        )
        remaining.discard(msg.seq)

    raise TimeoutError("Timed out uploading mission")


def download_mission(link, timeout: float = 30.0) -> list[MissionItem]:
    """Read the mission currently stored on the flight controller."""
    import time

    link.mav.mission_request_list_send(
        link.target_system, link.target_component, mavlink.MAV_MISSION_TYPE_MISSION
    )

    count_msg = link.recv_match(type="MISSION_COUNT", blocking=True, timeout=timeout)
    if count_msg is None:
        raise TimeoutError("No MISSION_COUNT from autopilot")

    items: list[MissionItem] = []
    deadline = time.monotonic() + timeout

    for seq in range(count_msg.count):
        link.mav.mission_request_int_send(
            link.target_system, link.target_component, seq,
            mavlink.MAV_MISSION_TYPE_MISSION,
        )
        while time.monotonic() < deadline:
            msg = link.recv_match(type="MISSION_ITEM_INT", blocking=True, timeout=2.0)
            if msg is not None and msg.seq == seq:
                items.append(
                    MissionItem(
                        seq=msg.seq, command=msg.command, frame=msg.frame,
                        x=msg.x, y=msg.y, z=msg.z,
                        param1=msg.param1, param2=msg.param2,
                        param3=msg.param3, param4=msg.param4,
                    )
                )
                break
        else:
            raise TimeoutError(f"Timed out waiting for mission item {seq}")

    link.mav.mission_ack_send(
        link.target_system, link.target_component,
        mavlink.MAV_MISSION_ACCEPTED, mavlink.MAV_MISSION_TYPE_MISSION,
    )
    return items


def diff_missions(
    sent: list[MissionItem],
    read_back: list[MissionItem],
    *,
    position_tolerance_deg7: int = 2,
    altitude_tolerance_m: float = 0.5,
) -> list[str]:
    """Compare an uploaded mission against what the autopilot reports storing.

    Returns human-readable differences; an empty list means the upload is
    verified. Small tolerances absorb the float32 round-trip in the protocol.

    Seq 0 is skipped: ArduPilot legitimately replaces the home placeholder with
    the vehicle's actual home position.
    """
    problems: list[str] = []

    if len(sent) != len(read_back):
        problems.append(f"Item count: sent {len(sent)}, autopilot has {len(read_back)}")

    for a, b in zip(sent, read_back):
        if a.seq == 0:
            continue
        if a.command != b.command:
            problems.append(
                f"Item {a.seq}: command sent {a.command}, stored {b.command}"
            )
            continue
        if abs(a.x - b.x) > position_tolerance_deg7 or abs(a.y - b.y) > position_tolerance_deg7:
            problems.append(
                f"Item {a.seq}: position sent {a.lat:.7f},{a.lon:.7f}, "
                f"stored {b.lat:.7f},{b.lon:.7f}"
            )
        if abs(a.z - b.z) > altitude_tolerance_m:
            problems.append(f"Item {a.seq}: altitude sent {a.z:.1f} m, stored {b.z:.1f} m")
        if abs(a.param1 - b.param1) > 0.05:
            problems.append(
                f"Item {a.seq}: param1 sent {a.param1:.2f}, stored {b.param1:.2f}"
            )

    return problems


def upload_and_verify(link, items: list[MissionItem]) -> list[MissionItem]:
    """Upload a mission and confirm the autopilot stored it correctly.

    Raises if the readback does not match. This is the only upload path the app
    should use.
    """
    upload_mission(link, items)
    stored = download_mission(link)
    problems = diff_missions(items, stored)
    if problems:
        raise RuntimeError(
            "Mission readback did not match what was uploaded:\n  "
            + "\n  ".join(problems)
        )
    return stored
