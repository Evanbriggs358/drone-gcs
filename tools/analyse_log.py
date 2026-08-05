"""Analyse an ArduPilot dataflash log.

    python tools/analyse_log.py "C:\\path\\to\\00000012.BIN"

Reports on vibration, compass health, motor interference, GPS quality, and
persistent attitude bias — the things that actually cause poor position hold.
"""

from __future__ import annotations

import argparse
import time

from gcs.diagnostics import analyse_log

MARK = {"ok": "ok  ", "warning": "WARN", "problem": "FAIL", "unknown": "  ? "}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("log")
    parser.add_argument("--max-records", type=int,
                        help="stop after N messages, for a quick look at a huge log")
    args = parser.parse_args()

    print(f"Reading {args.log} ...")
    started = time.monotonic()
    report = analyse_log(args.log, max_records=args.max_records)
    print(f"Parsed in {time.monotonic() - started:.1f} s\n")

    if report.duration_s:
        print(f"Flight duration : {report.duration_s / 60:.1f} min")
    if report.modes:
        print(f"Modes flown     : {' → '.join(report.modes[:12])}")
    counts = ", ".join(f"{k} {v}" for k, v in sorted(report.message_counts.items()))
    print(f"Messages        : {counts}\n")

    print(f"=== {report.summary()} ===\n")

    for finding in report.sorted_findings():
        print(f"[{MARK.get(finding.severity, '  ? ')}] {finding.headline}")
        for line in _wrap(finding.detail, 76):
            print(f"        {line}")
        print()


def _wrap(text: str, width: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        if len(current) + len(word) + 1 > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


if __name__ == "__main__":
    main()
