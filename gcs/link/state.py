"""Vehicle state, assembled from the MAVLink message stream.

ArduPilot reports vehicle state across a dozen message types at different rates.
This module folds them into one snapshot the rest of the app can read, so nothing
downstream has to know which message carried which field.

Pure data and pure functions — no sockets, no threads. Tests feed synthetic
messages, so the whole layer is verifiable without SITL or an aircraft.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import IntEnum

#: MAV_MODE_FLAG_SAFETY_ARMED
_ARMED_FLAG = 0b1000_0000

#: MAV_TYPE values that are not the aircraft: ground station (6), gimbal (26),
#: ADSB receiver (27), onboard controller (18), antenna tracker (5).
#: Their heartbeats must never be mistaken for the vehicle's.
_NON_VEHICLE_TYPES = frozenset({5, 6, 18, 26, 27})


class GpsFix(IntEnum):
    NO_GPS = 0
    NO_FIX = 1
    FIX_2D = 2
    FIX_3D = 3
    DGPS = 4
    RTK_FLOAT = 5
    RTK_FIXED = 6

    @property
    def is_usable(self) -> bool:
        """3D fix or better. Below this, position hold and AUTO are unsafe."""
        return self >= GpsFix.FIX_3D


#: ArduCopter flight modes, keyed by HEARTBEAT.custom_mode.
COPTER_MODES = {
    0: "STABILIZE",
    1: "ACRO",
    2: "ALT_HOLD",
    3: "AUTO",
    4: "GUIDED",
    5: "LOITER",
    6: "RTL",
    7: "CIRCLE",
    9: "LAND",
    11: "DRIFT",
    13: "SPORT",
    14: "FLIP",
    15: "AUTOTUNE",
    16: "POSHOLD",
    17: "BRAKE",
    18: "THROW",
    19: "AVOID_ADSB",
    20: "GUIDED_NOGPS",
    21: "SMART_RTL",
    22: "FLOWHOLD",
    23: "FOLLOW",
    24: "ZIGZAG",
    25: "SYSTEMID",
    26: "AUTOROTATE",
    27: "AUTO_RTL",
}


@dataclass
class VehicleState:
    """Latest known state of the aircraft.

    Every field carries a sensible "unknown" default so the UI can render before
    the first packets arrive, rather than special-casing a null state.
    """

    # -- link ----------------------------------------------------------------
    connected: bool = False
    last_heartbeat_s: float | None = None

    # -- mode and arming -----------------------------------------------------
    armed: bool = False
    mode: str = "UNKNOWN"

    # -- position ------------------------------------------------------------
    lat: float | None = None
    lon: float | None = None
    #: Altitude above the launch point. This is what missions and the HUD use.
    relative_alt_m: float | None = None
    #: Altitude above mean sea level.
    amsl_alt_m: float | None = None
    heading_deg: float | None = None
    ground_speed_ms: float | None = None
    climb_rate_ms: float | None = None

    # -- attitude ------------------------------------------------------------
    roll_deg: float | None = None
    pitch_deg: float | None = None
    yaw_deg: float | None = None

    # -- gnss ----------------------------------------------------------------
    gps_fix: GpsFix = GpsFix.NO_GPS
    satellites: int = 0
    #: Horizontal dilution of precision. Below 1.5 is good, above 2.5 is poor.
    hdop: float | None = None

    # -- power ---------------------------------------------------------------
    battery_v: float | None = None
    battery_current_a: float | None = None
    battery_remaining_pct: int | None = None

    # -- mission -------------------------------------------------------------
    mission_current_seq: int | None = None
    mission_total: int | None = None

    #: Most recent STATUSTEXT lines from the autopilot, newest last.
    messages: list[str] = field(default_factory=list)

    @property
    def cells(self) -> int | None:
        """Best guess at LiPo cell count, for per-cell voltage display."""
        if self.battery_v is None:
            return None
        # Resting cell voltage sits between 3.2 and 4.25 V; 6S is the design
        # target but this keeps the display honest on a different pack.
        for cells in range(1, 13):
            if 3.2 <= self.battery_v / cells <= 4.25:
                return cells
        return None

    @property
    def battery_v_per_cell(self) -> float | None:
        cells = self.cells
        return None if cells is None else self.battery_v / cells

    def link_age_s(self, now: float | None = None) -> float | None:
        """Seconds since the last heartbeat, or None if none has arrived."""
        if self.last_heartbeat_s is None:
            return None
        return (time.monotonic() if now is None else now) - self.last_heartbeat_s


def apply_message(state: VehicleState, msg) -> VehicleState:
    """Fold one MAVLink message into ``state``, mutating and returning it.

    Unknown message types are ignored — ArduPilot streams far more than the
    ground station needs, and silently skipping the rest keeps this readable.
    """
    kind = msg.get_type()

    if kind == "HEARTBEAT":
        # Ground stations, companion computers, and antenna trackers all emit
        # HEARTBEAT. Theirs carry base_mode 0, which would read as "the vehicle
        # disarmed" and silently abort a mission. Only the autopilot's heartbeat
        # describes the aircraft.
        if msg.type in _NON_VEHICLE_TYPES:
            return state
        state.connected = True
        state.last_heartbeat_s = time.monotonic()
        state.armed = bool(msg.base_mode & _ARMED_FLAG)
        state.mode = COPTER_MODES.get(msg.custom_mode, f"MODE_{msg.custom_mode}")

    elif kind == "GLOBAL_POSITION_INT":
        state.lat = msg.lat / 1e7
        state.lon = msg.lon / 1e7
        state.amsl_alt_m = msg.alt / 1000.0
        state.relative_alt_m = msg.relative_alt / 1000.0
        # hdg is centidegrees; 65535 means "unknown".
        state.heading_deg = None if msg.hdg == 65535 else msg.hdg / 100.0

    elif kind == "GPS_RAW_INT":
        state.gps_fix = GpsFix(msg.fix_type) if msg.fix_type in _FIX_VALUES else GpsFix.NO_GPS
        state.satellites = msg.satellites_visible
        # eph is HDOP * 100; 65535 means "unknown".
        state.hdop = None if msg.eph in (0, 65535) else msg.eph / 100.0

    elif kind == "ATTITUDE":
        state.roll_deg = _degrees(msg.roll)
        state.pitch_deg = _degrees(msg.pitch)
        state.yaw_deg = _degrees(msg.yaw) % 360.0

    elif kind == "VFR_HUD":
        state.ground_speed_ms = msg.groundspeed
        state.climb_rate_ms = msg.climb

    elif kind == "SYS_STATUS":
        # voltage_battery is mV, current_battery is cA and -1 when unmeasured.
        state.battery_v = msg.voltage_battery / 1000.0
        state.battery_current_a = (
            None if msg.current_battery == -1 else msg.current_battery / 100.0
        )
        state.battery_remaining_pct = (
            None if msg.battery_remaining == -1 else msg.battery_remaining
        )

    elif kind == "MISSION_CURRENT":
        state.mission_current_seq = msg.seq

    elif kind == "MISSION_COUNT":
        state.mission_total = msg.count

    elif kind == "STATUSTEXT":
        text = msg.text.decode() if isinstance(msg.text, bytes) else msg.text
        state.messages.append(text)
        del state.messages[:-50]  # keep the tail bounded

    return state


_FIX_VALUES = {f.value for f in GpsFix}


def _degrees(radians: float) -> float:
    from math import degrees

    return degrees(radians)
