"""Offload imagery from the companion computer to the ground station.

Runs on the laptop and talks to the Pi's HTTP service. Built around the
assumption that the WiFi link is unreliable (HARDWARE.md finding 1): every
transfer can be interrupted and resumed, and re-running an offload only fetches
what is missing rather than starting again.

Uses only the standard library, so the ground station gains no dependency for
what is essentially a file copy.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

#: Hostnames worth trying before asking the user for an address. The Pi is
#: normally reachable by mDNS name; a fixed IP is the fallback in the field.
DEFAULT_CANDIDATES = (
    "http://raspberrypi.local:8001",
    "http://drone.local:8001",
    "http://192.168.4.1:8001",
)


class CompanionError(RuntimeError):
    """The companion service could not be reached or returned an error."""


@dataclass
class PhotoTransfer:
    name: str
    expected_bytes: int
    transferred_bytes: int = 0
    resumed: bool = False
    skipped: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class OffloadResult:
    session: str
    destination: Path
    transfers: list[PhotoTransfer] = field(default_factory=list)

    @property
    def downloaded(self) -> int:
        return sum(1 for t in self.transfers if t.ok and not t.skipped)

    @property
    def skipped(self) -> int:
        return sum(1 for t in self.transfers if t.skipped)

    @property
    def failed(self) -> list[PhotoTransfer]:
        return [t for t in self.transfers if not t.ok]

    @property
    def bytes_transferred(self) -> int:
        return sum(t.transferred_bytes for t in self.transfers)

    @property
    def complete(self) -> bool:
        return not self.failed

    def summary(self) -> str:
        megabytes = self.bytes_transferred / 1024**2
        parts = [f"{self.downloaded} downloaded"]
        if self.skipped:
            parts.append(f"{self.skipped} already present")
        if self.failed:
            parts.append(f"{len(self.failed)} FAILED")
        return f"{', '.join(parts)} ({megabytes:.1f} MB)"


class CompanionClient:
    """HTTP client for the Pi's companion service."""

    def __init__(self, base_url: str, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # -- discovery ---------------------------------------------------------

    @classmethod
    def discover(
        cls, candidates: tuple[str, ...] = DEFAULT_CANDIDATES, timeout: float = 2.0
    ) -> CompanionClient | None:
        """Return a client for the first candidate that answers /health."""
        for url in candidates:
            client = cls(url, timeout=timeout)
            try:
                client.health()
                return client
            except CompanionError:
                continue
        return None

    # -- api ---------------------------------------------------------------

    def health(self) -> dict:
        return self._get_json("/health")

    def status(self) -> dict:
        return self._get_json("/status")

    def sessions(self) -> list[dict]:
        return self._get_json("/sessions")

    def photos(self, session: str) -> list[dict]:
        return self._get_json(f"/sessions/{session}/photos")

    def start_session(self, name: str | None = None, trigger_distance_m: float | None = None) -> dict:
        payload = json.dumps(
            {"name": name, "trigger_distance_m": trigger_distance_m}
        ).encode()
        request = urllib.request.Request(
            f"{self.base_url}/session/start",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self._read_json(request)

    def stop_session(self) -> dict:
        request = urllib.request.Request(f"{self.base_url}/session/stop", method="POST")
        return self._read_json(request)

    def preflight(self, mission_waypoints: int = 0, estimated_photos: int = 0) -> dict:
        return self._get_json(
            f"/preflight?mission_waypoints={mission_waypoints}"
            f"&estimated_photos={estimated_photos}"
        )

    def upload_mission(
        self,
        waypoints: list[list[float]],
        altitude_m: float,
        trigger_distance_m: float,
        home: list[float] | None = None,
    ) -> dict:
        """Ask the companion to write a mission to the flight controller."""
        payload = json.dumps({
            "waypoints": waypoints,
            "altitude_m": altitude_m,
            "trigger_distance_m": trigger_distance_m,
            "home": home,
        }).encode()
        request = urllib.request.Request(
            f"{self.base_url}/mission/upload",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        return self._read_json(request)

    # -- transfer ----------------------------------------------------------

    def download_session(
        self,
        session: str,
        destination_root: str | Path,
        on_progress=None,
        attempts: int = 3,
    ) -> OffloadResult:
        """Copy a flight's imagery and manifest to the ground station.

        Safe to re-run: files already present at the expected size are skipped,
        and partial files resume from where they stopped.
        """
        destination = Path(destination_root) / session
        images_dir = destination / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        result = OffloadResult(session=session, destination=destination)

        try:
            self._download_file(
                f"/sessions/{session}/manifest", destination / "captures.jsonl"
            )
        except CompanionError:
            # A missing manifest is not fatal; the images are what matter.
            pass

        photos = self.photos(session)
        for index, photo in enumerate(photos, start=1):
            transfer = self._transfer_photo(session, photo, images_dir, attempts)
            result.transfers.append(transfer)
            if on_progress is not None:
                on_progress(index, len(photos), transfer)

        return result

    def _transfer_photo(
        self, session: str, photo: dict, images_dir: Path, attempts: int
    ) -> PhotoTransfer:
        name = photo["name"]
        expected = int(photo["bytes"])
        target = images_dir / name
        transfer = PhotoTransfer(name=name, expected_bytes=expected)

        if target.exists() and target.stat().st_size == expected:
            transfer.skipped = True
            return transfer

        for attempt in range(1, attempts + 1):
            have = target.stat().st_size if target.exists() else 0
            if have > expected:
                target.unlink()  # corrupt or stale; start over
                have = 0

            try:
                written = self._download_file(
                    f"/sessions/{session}/photos/{name}", target, resume_from=have
                )
                transfer.transferred_bytes = written
                transfer.resumed = have > 0
                if target.stat().st_size == expected:
                    transfer.error = None
                    return transfer
                transfer.error = (
                    f"size mismatch: got {target.stat().st_size}, expected {expected}"
                )
            except CompanionError as error:
                transfer.error = str(error)

            if attempt == attempts:
                break

        return transfer

    def _download_file(self, path: str, target: Path, resume_from: int = 0) -> int:
        """Fetch a file, optionally resuming. Returns bytes written this call."""
        request = urllib.request.Request(f"{self.base_url}{path}")
        if resume_from:
            request.add_header("Range", f"bytes={resume_from}-")

        target.parent.mkdir(parents=True, exist_ok=True)

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                # 206 means the server honoured the range and we may append.
                # 200 means it sent the whole file, so the partial is discarded.
                append = resume_from > 0 and response.status == 206
                mode = "ab" if append else "wb"
                written = 0
                with target.open(mode) as handle:
                    while True:
                        chunk = response.read(256 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
                        written += len(chunk)
                return written
        except (urllib.error.URLError, socket.timeout, OSError) as error:
            raise CompanionError(f"{path}: {error}") from error

    # -- plumbing ----------------------------------------------------------

    def _get_json(self, path: str):
        return self._read_json(urllib.request.Request(f"{self.base_url}{path}"))

    def _read_json(self, request: urllib.request.Request):
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode())
        except (urllib.error.URLError, socket.timeout, OSError, ValueError) as error:
            raise CompanionError(f"{request.full_url}: {error}") from error
