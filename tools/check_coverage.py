"""Check a flight for coverage gaps, before leaving the site.

    python tools/check_coverage.py C:\\DroneData\\flights\\2026-08-05_071804
    python tools/check_coverage.py <folder> --cell 5 --min-photos 4

Works from the capture manifest when there is one, and falls back to the
photos' own geotags. Answers the question that OpenDroneMap only answers hours
later: is anything under-photographed, and where.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from gcs.coverage import analyse_coverage, footprints_from_manifest, footprints_from_photos
from gcs.planning import CAMERAS
from gcs.planning import PI_CAMERA_MODULE_3 as DEFAULT_CAMERA


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("flight", help="flight folder, or a folder of photos")
    parser.add_argument("--cell", type=float, default=10.0, help="grid size in metres")
    parser.add_argument("--min-photos", type=int, default=3,
                        help="photos needed to reconstruct a point")
    parser.add_argument("--camera", choices=sorted(CAMERAS), help="camera used")
    parser.add_argument("--ground-altitude", type=float,
                        help="site elevation above sea level, when using EXIF")
    args = parser.parse_args()

    camera = CAMERAS.get(args.camera or "", DEFAULT_CAMERA)
    folder = Path(args.flight)
    manifest = folder / "captures.jsonl"
    images = folder / "images" if (folder / "images").is_dir() else folder

    if manifest.is_file():
        print(f"Using capture manifest: {manifest}")
        footprints, frame = footprints_from_manifest(manifest, camera)
    else:
        photos = sorted(
            p for p in images.iterdir() if p.suffix.lower() in {".jpg", ".jpeg"}
        )
        if not photos:
            raise SystemExit(f"No manifest and no photos found in {folder}")
        print(f"No manifest; using EXIF from {len(photos)} photos")
        footprints, frame = footprints_from_photos(
            photos, camera, ground_altitude_m=args.ground_altitude
        )

    if not footprints:
        raise SystemExit(
            "No usable photo positions. Without height above ground the ground "
            "footprint cannot be computed."
        )

    report = analyse_coverage(
        footprints, frame, cell_size_m=args.cell, min_required=args.min_photos
    )

    print(f"\n{report.summary()}\n")

    counts = [c.count for c in report.cells if c.count > 0]
    if counts:
        print(f"Photos per point: {min(counts)} worst, "
              f"{sorted(counts)[len(counts) // 2]} median, {max(counts)} best")
        print(f"Reconstructable area: {len(report.covered_cells) * args.cell ** 2:,.0f} m2")

    if report.gap_cells:
        print(f"\nInterior gaps ({len(report.gap_cells)} cells, "
              f"{args.cell:g} m grid) — worst first:")
        for cell in sorted(report.gap_cells, key=lambda c: c.count)[:15]:
            print(f"  {cell.count} photo(s)  {cell.lat:.6f}, {cell.lon:.6f}")
        if len(report.gap_cells) > 15:
            print(f"  ... and {len(report.gap_cells) - 15} more")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
