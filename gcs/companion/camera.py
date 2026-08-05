"""Camera abstraction for the companion computer.

The real camera is a Pi Camera Module 3 driven by picamera2, which only exists
on a Raspberry Pi. Everything above this module works against the :class:`Camera`
protocol, so the whole capture pipeline can be developed and tested on a laptop
against :class:`FakeCamera`, then swapped for real hardware unchanged.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class Camera(Protocol):
    """Anything that can put a JPEG on disk when asked."""

    def capture(self, path: str | Path) -> None:
        """Capture a full-resolution frame and write it to ``path``."""

    def close(self) -> None:
        """Release the device."""


class FakeCamera:
    """Writes synthetic JPEGs. For development and simulator testing.

    Frames carry a visible sequence number so a run can be checked by eye, and
    enough random texture that they compress to a realistic size rather than a
    few hundred bytes of flat colour.
    """

    def __init__(
        self,
        width: int = 640,
        height: int = 480,
        capture_delay_s: float = 0.0,
        fail_every: int | None = None,
    ) -> None:
        self.width = width
        self.height = height
        self.capture_delay_s = capture_delay_s
        #: Fail every Nth capture, to exercise error handling.
        self.fail_every = fail_every
        self.captures = 0
        self.closed = False

    def capture(self, path: str | Path) -> None:
        from PIL import Image, ImageDraw

        self.captures += 1
        if self.fail_every and self.captures % self.fail_every == 0:
            raise RuntimeError(f"simulated camera failure on capture {self.captures}")

        if self.capture_delay_s:
            time.sleep(self.capture_delay_s)

        image = Image.new("RGB", (self.width, self.height), (60, 70, 85))
        draw = ImageDraw.Draw(image)
        # Cheap deterministic texture so JPEG has something to compress.
        for i in range(0, self.width, 17):
            shade = (i * 7) % 200 + 30
            draw.line([(i, 0), (i + 40, self.height)], fill=(shade, shade // 2, 90))
        draw.text((10, 10), f"frame {self.captures}", fill=(255, 255, 255))

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        image.save(path, "JPEG", quality=90)

    def close(self) -> None:
        self.closed = True


class PiCameraModule3:
    """Pi Camera Module 3 via picamera2.

    Configuration carried over from the previous capture daemon, which had these
    right:

    - **Two streams.** Full resolution to disk, a small one for the downlink.
      Sending 12 MP frames over the WiFi link would saturate it.
    - **Focus locked to infinity.** Autofocus hunting mid-survey produces a few
      unusable frames and there is nothing near the camera to focus on anyway.
    - **Fast shutter.** At mapping speeds the exposure time, not the lens, sets
      sharpness. See ``Camera.motion_blur_px`` in gcs/planning/camera.py for the
      arithmetic behind choosing one.
    """

    #: Full sensor resolution of the IMX708.
    FULL_RESOLUTION = (4608, 2592)
    #: Small stream for streaming to the ground station.
    PREVIEW_RESOLUTION = (1280, 720)

    def __init__(
        self,
        exposure_time_us: int = 500,        # 1/2000 s
        jpeg_quality: int = 95,
        warmup_s: float = 2.0,
    ) -> None:
        from picamera2 import Picamera2  # only present on a Raspberry Pi

        self._camera = Picamera2()
        config = self._camera.create_still_configuration(
            main={"size": self.FULL_RESOLUTION},
            lores={"size": self.PREVIEW_RESOLUTION},
            display=None,
        )
        self._camera.configure(config)
        self._camera.options["quality"] = jpeg_quality

        self._camera.set_controls({
            "AfMode": 0,          # manual focus
            "LensPosition": 0.0,  # 0 dioptres = infinity
            "ExposureTime": exposure_time_us,
            "AeEnable": False,
        })

        self._camera.start()
        # The sensor needs a moment before the first frame is properly exposed.
        time.sleep(warmup_s)

    def capture(self, path: str | Path) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._camera.capture_file(str(path))

    def capture_preview(self, path: str | Path) -> None:
        """Grab the small stream, for the downlink rather than the archive."""
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._camera.capture_file(str(path), name="lores")

    def close(self) -> None:
        self._camera.stop()
        self._camera.close()


def open_camera(fake: bool = False, **kwargs) -> Camera:
    """Open the real camera, or a fake one when off-hardware.

    Falls back to :class:`FakeCamera` with a clear message if picamera2 is not
    installed, so running the service on a laptop is a warning rather than a
    crash.
    """
    if fake:
        return FakeCamera(**kwargs)
    try:
        return PiCameraModule3(**kwargs)
    except ImportError:
        print("picamera2 not available — using FakeCamera. Not for real flights.")
        return FakeCamera()
