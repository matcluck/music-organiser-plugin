#!/usr/bin/env python3
"""Inventory short audio files without modifying them."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

from mutagen import File as MutagenFile


AUDIO_EXTENSIONS = {".aac", ".aif", ".aiff", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".wma"}


def first(tags: object, *keys: str) -> str:
    if not tags:
        return ""
    for key in keys:
        try:
            value = tags.get(key)
        except AttributeError:
            return ""
        if value:
            if isinstance(value, (list, tuple)):
                value = value[0] if value else ""
            text = getattr(value, "text", value)
            if isinstance(text, (list, tuple)):
                text = text[0] if text else ""
            return str(text)
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    rows: list[dict[str, object]] = []
    scanned = 0
    errors = 0
    for path in sorted(args.root.rglob("*"), key=lambda value: str(value).casefold()):
        if not path.is_file() or path.suffix.casefold() not in AUDIO_EXTENSIONS:
            continue
        scanned += 1
        try:
            audio = MutagenFile(path)
            if audio is None or not getattr(audio, "info", None):
                raise ValueError("Unsupported or unreadable audio")
            duration = float(audio.info.length)
            if duration < args.seconds:
                tags = audio.tags
                rows.append({
                    "path": str(path),
                    "durationSeconds": round(duration, 3),
                    "title": first(tags, "title", "TIT2", "\xa9nam"),
                    "artist": first(tags, "artist", "TPE1", "\xa9ART"),
                    "album": first(tags, "album", "TALB", "\xa9alb"),
                })
        except Exception as exc:
            errors += 1
            rows.append({
                "path": str(path),
                "durationSeconds": "",
                "title": "",
                "artist": "",
                "album": "",
                "error": f"{type(exc).__name__}: {exc}",
            })

    fieldnames = ["path", "durationSeconds", "title", "artist", "album", "error"]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)

    print(f"Scanned: {scanned:,}")
    print(f"Under {args.seconds:g}s: {sum(not row.get('error') for row in rows):,}")
    print(f"Read errors: {errors:,}")
    print(f"Report: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
