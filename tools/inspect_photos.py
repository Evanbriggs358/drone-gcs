"""Inspect and validate a folder of survey photos.

Reports what the geotags say and flags anything that would hurt a
reconstruction. Run this before starting a photogrammetry job, and in the field
after offloading, while there is still time to re-fly.

    python tools/inspect_photos.py C:\\DroneData\\samples\\mygla\\images
"""

from __future__ import annotations

import argparse
import statistics
from pathlib import Path

from gcs.companion.geotag import read_geotag, validate_photos
from gcs.planning.geo import haversine_m

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".JPG", ".JPEG"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("folder")
    parser.add_argument("--sharpness", action="store_true", help="score blur (slower)")
    args = parser.parse_args()

    folder = Path(args.folder)
    photos = sorted(p for p in folder.iterdir() if p.suffix in IMAGE_SUFFIXES)
    if not photos:
        raise SystemExit(f"No JPEGs found in {folder}")

    print(f"{len(photos)} photos in {folder}\n")

    tags = [(p, read_geotag(p)) for p in photos]
    located = [(p, t) for p, t in tags if t is not None]

    print(f"Geotagged: {len(located)}/{len(photos)}")
    if not located:
        raise SystemExit("No geotags found — nothing further to report.")

    lats = [t.lat for _, t in located]
    lons = [t.lon for _, t in located]
    alts = [t.altitude_m for _, t in located]
    yaws = [t.yaw_deg for _, t in located if t.yaw_deg is not None]

    print(f"Centre:    {statistics.mean(lats):.6f}, {statistics.mean(lons):.6f}")
    print(f"Altitude:  {min(alts):.1f} to {max(alts):.1f} m "
          f"(mean {statistics.mean(alts):.1f})")
    print(f"Heading:   {len(yaws)}/{len(located)} photos carry camera heading")

    # Extent of the surveyed area, from the bounding box corners.
    width = haversine_m((statistics.mean(lats), min(lons)), (statistics.mean(lats), max(lons)))
    height = haversine_m((min(lats), statistics.mean(lons)), (max(lats), statistics.mean(lons)))
    print(f"Extent:    {width:.0f} m x {height:.0f} m")

    # Spacing between consecutive shots — the number that decides front overlap.
    steps = [
        haversine_m((a.lat, a.lon), (b.lat, b.lon))
        for (_, a), (_, b) in zip(located, located[1:])
    ]
    if steps:
        ordered = sorted(steps)
        print(f"Spacing:   median {statistics.median(steps):.1f} m, "
              f"min {ordered[0]:.1f}, max {ordered[-1]:.1f}")

    problems = validate_photos([p for p, _ in tags])
    print(f"\nValidation: {'no problems' if not problems else f'{len(problems)} problems'}")
    for problem in problems[:20]:
        print(f"  {Path(problem.path).name}: {problem.reason}")

    if args.sharpness:
        from gcs.companion.geotag import sharpness

        print("\nSharpness (lower = blurrier):")
        scored = sorted((sharpness(p), p) for p in photos)
        for score, path in scored[:3]:
            print(f"  {score:8.1f}  {path.name}   <- blurriest")
        for score, path in scored[-2:]:
            print(f"  {score:8.1f}  {path.name}")


if __name__ == "__main__":
    main()
