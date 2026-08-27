#!/usr/bin/env python3
"""Build an M3U8 playlist from successfully applied plan destinations."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    with args.plan.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [row for row in csv.DictReader(handle) if row.get("newPath")]

    entries: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for row in rows:
        path = Path(row["newPath"])
        if not path.is_file():
            raise FileNotFoundError(f"Planned destination does not exist: {path}")
        key = str(path.resolve()).casefold()
        if key in seen:
            continue
        seen.add(key)
        label = f"{row.get('artist', '').strip()} - {row.get('title', '').strip()}".strip(" -")
        entries.append((label, path.resolve()))

    if not entries:
        raise ValueError("Plan contains no existing destination files")

    lines = ["#EXTM3U"]
    for label, path in entries:
        lines.append(f"#EXTINF:-1,{label}")
        lines.append(str(path))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {len(entries):,} entries: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
