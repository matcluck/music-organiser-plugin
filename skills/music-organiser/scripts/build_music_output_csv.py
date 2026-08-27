#!/usr/bin/env python3
"""Build a CSV of planned music paths and final output tags from an LLM manifest."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path


CSV_FIELDS = [
    "sourcePath",
    "newPath",
    "title",
    "artist",
    "albumArtist",
    "album",
    "date",
    "trackNumber",
    "discNumber",
    "genre",
    "compilation",
]

WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}

COPY_SUFFIX_PATTERN = re.compile(r"(?:\s*\(\d+\)|\s*-\s*\d+)$")

FORMAT_QUALITY = {
    ".flac": 8,
    ".wav": 7,
    ".aiff": 7,
    ".aif": 7,
    ".alac": 6,
    ".m4a": 5,
    ".aac": 4,
    ".ogg": 4,
    ".opus": 4,
    ".mp3": 3,
    ".wma": 2,
}

OUTPUT_EXTENSIONS = {
    ".aac",
    ".aif",
    ".aiff",
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".opus",
    ".wav",
    ".wma",
}


def clean_text(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split()).strip()


def sanitize_component(value: object, fallback: str, max_length: int = 120) -> str:
    text = clean_text(value) or fallback
    text = re.sub(r'(?<=\S)[:/\\|](?=\S)', "-", text)
    text = re.sub(r'\s*[:/\\|]\s*', " - ", text)
    text = text.translate(
        str.maketrans(
            {
                '"': "'",
                "<": "(",
                ">": ")",
                "?": "？",
                "*": "＊",
            }
        )
    )
    text = re.sub(r"[\x00-\x1f]", "", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    text = re.sub(r"(?:\s+-\s+){2,}", " - ", text)
    if not text:
        text = fallback
    if text.upper() in WINDOWS_RESERVED_NAMES:
        text = f"_{text}"
    return text[:max_length].rstrip(" .") or fallback


def positive_integer(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def is_ready(output: object) -> bool:
    return (
        isinstance(output, dict)
        and output.get("excluded") is not True
        and output.get("needsReview") is not True
        and bool(clean_text(output.get("title")))
        and bool(clean_text(output.get("artist")))
    )


def output_extension(value: object, fallback: str) -> str:
    extension = clean_text(value).casefold()
    if not extension:
        extension = fallback.casefold()
    if extension and not extension.startswith("."):
        extension = f".{extension}"
    if extension not in OUTPUT_EXTENSIONS:
        raise ValueError(f"Unsupported output extension: {extension or value}")
    return extension


def track_filename(
    source: Path,
    output: dict[str, object],
    extension: str | None = None,
) -> str:
    title = sanitize_component(output.get("title"), source.stem)
    track_number = positive_integer(output.get("trackNumber"))
    disc_number = positive_integer(output.get("discNumber"))
    prefix = ""
    if track_number is not None:
        prefix = f"{track_number:02d} - "
        if disc_number is not None and disc_number > 1:
            prefix = f"{disc_number}-{track_number:02d} - "

    if output.get("compilation") is True:
        artist = sanitize_component(output.get("artist"), "Unknown Artist")
        title = sanitize_component(f"{artist} - {title}", title)

    suffix = output_extension(extension, source.suffix)
    return f"{sanitize_component(prefix + title, source.stem)}{suffix}"


def planned_path(
    source: Path,
    destination_root: Path,
    output: dict[str, object],
    extension: str | None = None,
) -> Path | None:
    if not is_ready(output):
        return None

    artist = sanitize_component(output.get("artist"), "Unknown Artist")
    album = clean_text(output.get("album"))
    if not album:
        title = sanitize_component(output.get("title"), source.stem)
        filename = sanitize_component(f"{artist} - {title}", source.stem)
        suffix = output_extension(extension, source.suffix)
        return destination_root / artist / f"{filename}{suffix}"

    if output.get("compilation") is True:
        release_artist = "Various Artists"
    else:
        release_artist = sanitize_component(
            output.get("albumArtist") or output.get("artist"),
            artist,
        )
    album_folder = sanitize_component(album, "Unknown Album")
    return destination_root / release_artist / album_folder / track_filename(
        source, output, extension
    )


def copy_suffix_depth(stem: str) -> int:
    depth = 0
    remaining = stem.rstrip()
    while True:
        shortened = COPY_SUFFIX_PATTERN.sub("", remaining).rstrip()
        if shortened == remaining:
            return depth
        depth += 1
        remaining = shortened


def duplicate_preference(source: Path) -> tuple[int, int, int, str]:
    try:
        size = source.stat().st_size
    except OSError:
        size = 0
    return (
        copy_suffix_depth(source.stem),
        -FORMAT_QUALITY.get(source.suffix.casefold(), 0),
        -size,
        str(source).casefold(),
    )


def load_manifest(path: Path) -> list[dict[str, object]]:
    with path.open("r", encoding="utf-8-sig") as file:
        value = json.load(file)
    if not isinstance(value, list):
        raise ValueError("Manifest root must be a JSON array.")
    return value


def build_rows(
    manifest: list[dict[str, object]],
    destination_root: Path,
) -> tuple[list[dict[str, object]], int, int]:
    prepared: list[tuple[Path, dict[str, object], Path | None]] = []
    destination_groups: dict[str, list[int]] = {}

    for index, item in enumerate(manifest, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Manifest item {index} is not an object.")
        source = Path(str(item.get("source", "")))
        output = item.get("outputMetadata")
        if not isinstance(output, dict):
            output = {}

        destination = planned_path(
            source,
            destination_root,
            output,
            item.get("outputExtension"),
        )
        prepared.append((source, output, destination))
        if destination is not None:
            destination_identity = destination.parent / destination.stem
            destination_groups.setdefault(
                str(destination_identity).casefold(), []
            ).append(len(prepared) - 1)

    duplicate_losers: set[int] = set()
    for indexes in destination_groups.values():
        if len(indexes) < 2:
            continue
        winner = min(indexes, key=lambda row_index: duplicate_preference(prepared[row_index][0]))
        duplicate_losers.update(row_index for row_index in indexes if row_index != winner)

    rows: list[dict[str, object]] = []
    review_blanks = 0
    for row_index, (source, output, destination) in enumerate(prepared):
        if destination is None:
            review_blanks += 1
            new_path = ""
        elif row_index in duplicate_losers:
            new_path = ""
        else:
            new_path = str(destination)
        rows.append(
            {
                "sourcePath": str(source),
                "newPath": new_path,
                "title": clean_text(output.get("title")),
                "artist": clean_text(output.get("artist")),
                "albumArtist": clean_text(output.get("albumArtist")),
                "album": clean_text(output.get("album")),
                "date": clean_text(output.get("date")),
                "trackNumber": positive_integer(output.get("trackNumber")) or "",
                "discNumber": positive_integer(output.get("discNumber")) or "",
                "genre": clean_text(output.get("genre")),
                "compilation": "true" if output.get("compilation") is True else "false",
            }
        )
    return rows, review_blanks, len(duplicate_losers)


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a CSV of planned output paths and final audio metadata."
    )
    parser.add_argument("manifest", type=Path, help="LLM-processed music manifest JSON.")
    parser.add_argument("destination", type=Path, help="Root of the future music library.")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("music-output-plan.csv"),
        help="CSV output path. Default: music-output-plan.csv",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest_path = args.manifest.expanduser().resolve()
    destination_root = args.destination.expanduser().resolve()
    output_path = args.out.expanduser().resolve()

    if not manifest_path.is_file():
        print(f"Manifest does not exist: {manifest_path}", file=sys.stderr)
        return 1

    try:
        manifest = load_manifest(manifest_path)
        rows, review_blanks, duplicate_blanks = build_rows(manifest, destination_root)
        write_csv(output_path, rows)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Cannot build CSV: {exc}", file=sys.stderr)
        return 1

    print(f"Tracks:          {len(rows):,}")
    print(f"Planned paths:   {len(rows) - review_blanks - duplicate_blanks:,}")
    print(f"Blank/review:    {review_blanks:,}")
    print(f"Duplicate skips: {duplicate_blanks:,}")
    print(f"CSV:             {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
