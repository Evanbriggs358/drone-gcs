"""Pull a flight's imagery off the companion computer.

    python tools/offload.py --list
    python tools/offload.py <session-name>
    python tools/offload.py <session-name> --url http://192.168.1.50:8001

Safe to interrupt and re-run: files already present are skipped and partial
transfers resume, which matters on a WiFi link that drops.
"""

from __future__ import annotations

import argparse
import sys

from gcs.offload import CompanionClient, CompanionError, PhotoTransfer


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("session", nargs="?", help="session name, or omit with --list")
    parser.add_argument("--url", help="companion service URL; discovered if omitted")
    parser.add_argument("--list", action="store_true", help="list flights on the Pi")
    parser.add_argument("--output", default=r"C:\DroneData\flights")
    args = parser.parse_args()

    client = CompanionClient(args.url) if args.url else CompanionClient.discover()
    if client is None:
        raise SystemExit(
            "Could not find the companion service. Check the Pi is powered and on "
            "the same network, then pass its address with --url."
        )

    try:
        health = client.health()
    except CompanionError as error:
        raise SystemExit(f"Companion service unreachable: {error}")

    print(f"Companion at {client.base_url}")
    print(f"  camera {health['camera']}, {health['free_disk_gb']} GB free")
    link = health["link"]
    print(f"  flight controller: {'connected' if link['connected'] else 'NOT CONNECTED'}\n")

    sessions = client.sessions()
    if args.list or not args.session:
        if not sessions:
            print("No flights stored on the companion.")
            return
        print(f"{'flight':<28} {'photos':>7} {'size':>10}")
        print("-" * 48)
        for session in sessions:
            size_mb = session["bytes"] / 1024**2
            active = "  (recording)" if session["active"] else ""
            print(f"{session['name']:<28} {session['photos']:>7} {size_mb:>9.1f}M{active}")
        if not args.session:
            print("\nPass a flight name to download it.")
        return

    known = {s["name"] for s in sessions}
    if args.session not in known:
        raise SystemExit(f"No flight named {args.session!r}. Use --list to see them.")

    print(f"Downloading {args.session} to {args.output}\n")

    def progress(index: int, total: int, transfer: PhotoTransfer) -> None:
        if transfer.skipped:
            note = "already present"
        elif transfer.error:
            note = f"FAILED — {transfer.error}"
        elif transfer.resumed:
            note = f"resumed, {transfer.transferred_bytes / 1024:.0f} KB"
        else:
            note = f"{transfer.transferred_bytes / 1024:.0f} KB"
        print(f"\r  [{index:>4}/{total}] {transfer.name}  {note}", end="", flush=True)
        if transfer.error:
            print()

    result = client.download_session(args.session, args.output, on_progress=progress)

    print(f"\n\n{result.summary()}")
    print(f"  {result.destination}")

    if result.failed:
        print("\nFailed transfers:")
        for transfer in result.failed:
            print(f"  {transfer.name}: {transfer.error}")
        print("\nRe-run to retry — completed files are skipped.")
        sys.exit(1)

    print("\nNext: validate the imagery before processing")
    print(f"  python tools/inspect_photos.py {result.destination}\\images")


if __name__ == "__main__":
    main()
