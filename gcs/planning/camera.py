"""Camera model and photogrammetry spacing math.

Everything that turns "fly at 100 m with 70% overlap" into concrete numbers —
ground sample distance, ground footprint, distance between photos, distance
between flight lines — lives here.

Pure functions, no I/O, no hardware. See tests/test_camera.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import atan, degrees


@dataclass(frozen=True)
class Camera:
    """A fixed-lens camera pointed straight down.

    The sensor is described in pixels and pixel pitch rather than millimetres of
    sensor width, because that is how camera datasheets and picamera2 report it,
    and it avoids a rounding step.
    """

    name: str
    image_width_px: int
    image_height_px: int
    pixel_pitch_um: float
    focal_length_mm: float

    #: Whether the sensor's long axis lies across the direction of flight.
    #: True is the usual mapping mount: it maximises swath width, so fewer
    #: flight lines are needed to cover the survey area.
    long_axis_across_track: bool = True

    #: Slowest full-resolution capture the camera can sustain, in seconds.
    #: Caps ground speed — fly faster than this allows and photos get skipped.
    min_capture_interval_s: float = 1.0

    # -- sensor geometry ---------------------------------------------------

    @property
    def pixel_pitch_mm(self) -> float:
        return self.pixel_pitch_um / 1000.0

    @property
    def sensor_width_mm(self) -> float:
        return self.image_width_px * self.pixel_pitch_mm

    @property
    def sensor_height_mm(self) -> float:
        return self.image_height_px * self.pixel_pitch_mm

    @property
    def hfov_deg(self) -> float:
        """Field of view across the sensor's long axis."""
        return 2.0 * degrees(atan(self.sensor_width_mm / 2.0 / self.focal_length_mm))

    @property
    def vfov_deg(self) -> float:
        """Field of view across the sensor's short axis."""
        return 2.0 * degrees(atan(self.sensor_height_mm / 2.0 / self.focal_length_mm))

    @property
    def across_track_px(self) -> int:
        return self.image_width_px if self.long_axis_across_track else self.image_height_px

    @property
    def along_track_px(self) -> int:
        return self.image_height_px if self.long_axis_across_track else self.image_width_px

    # -- ground projection -------------------------------------------------

    def gsd_m_per_px(self, altitude_m: float) -> float:
        """Ground sample distance: how much ground one pixel covers, in metres.

        This is the number that decides whether the final map can resolve a crack
        in a slab or only a parking space.
        """
        _require_positive(altitude_m, "altitude_m")
        return altitude_m * self.pixel_pitch_mm / self.focal_length_mm

    def gsd_cm_per_px(self, altitude_m: float) -> float:
        return self.gsd_m_per_px(altitude_m) * 100.0

    def footprint_m(self, altitude_m: float) -> tuple[float, float]:
        """Ground area covered by one photo, as (across-track, along-track) metres."""
        gsd = self.gsd_m_per_px(altitude_m)
        return self.across_track_px * gsd, self.along_track_px * gsd

    def altitude_for_gsd(self, gsd_cm_per_px: float) -> float:
        """Inverse of :meth:`gsd_cm_per_px` — the altitude that achieves a target GSD.

        Lets the planner be driven by "I need 2 cm/px" instead of by altitude.
        """
        _require_positive(gsd_cm_per_px, "gsd_cm_per_px")
        return (gsd_cm_per_px / 100.0) * self.focal_length_mm / self.pixel_pitch_mm

    # -- survey spacing ----------------------------------------------------

    def photo_spacing_m(self, altitude_m: float, front_overlap: float) -> float:
        """Distance to fly between shutter releases."""
        _require_overlap(front_overlap, "front_overlap")
        _, along = self.footprint_m(altitude_m)
        return along * (1.0 - front_overlap)

    def line_spacing_m(self, altitude_m: float, side_overlap: float) -> float:
        """Distance between adjacent flight lines."""
        _require_overlap(side_overlap, "side_overlap")
        across, _ = self.footprint_m(altitude_m)
        return across * (1.0 - side_overlap)

    def max_ground_speed_ms(self, altitude_m: float, front_overlap: float) -> float:
        """Fastest the aircraft may fly without outrunning the shutter.

        Exceeding this silently drops front overlap below the requested value,
        which is a classic cause of reconstruction holes.
        """
        return self.photo_spacing_m(altitude_m, front_overlap) / self.min_capture_interval_s

    def motion_blur_px(
        self, altitude_m: float, ground_speed_ms: float, exposure_s: float
    ) -> float:
        """Smear, in pixels, caused by the aircraft moving during the exposure.

        Keep below roughly 1 px. This is why the capture daemon locks a fast
        shutter; at mapping speeds the exposure, not the lens, sets sharpness.
        """
        return (ground_speed_ms * exposure_s) / self.gsd_m_per_px(altitude_m)


def _require_positive(value: float, name: str) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value}")


def _require_overlap(value: float, name: str) -> None:
    if not 0.0 <= value < 1.0:
        raise ValueError(f"{name} must be a fraction in [0, 1), got {value}")


#: Raspberry Pi Camera Module 3, standard lens (Sony IMX708).
#: 4608x2592 at 1.4 um pitch, 4.74 mm focal length -> ~68 deg horizontal FOV.
PI_CAMERA_MODULE_3 = Camera(
    name="Pi Camera Module 3 (standard)",
    image_width_px=4608,
    image_height_px=2592,
    pixel_pitch_um=1.4,
    focal_length_mm=4.74,
)

#: Wide-angle variant. Shorter focal length means a bigger footprint and fewer
#: flight lines, at the cost of coarser GSD and more lens distortion for the
#: photogrammetry solver to model.
PI_CAMERA_MODULE_3_WIDE = Camera(
    name="Pi Camera Module 3 (wide)",
    image_width_px=4608,
    image_height_px=2592,
    pixel_pitch_um=1.4,
    focal_length_mm=2.75,
)

CAMERAS = {c.name: c for c in (PI_CAMERA_MODULE_3, PI_CAMERA_MODULE_3_WIDE)}
