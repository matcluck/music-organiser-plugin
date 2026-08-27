#!/usr/bin/env python3
"""Apply conservative post-LLM cleanup to outputMetadata without touching source evidence."""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from collections import Counter
from pathlib import Path

from build_music_manifest import write_json_atomic
from populate_music_manifest import (
    PLACEHOLDER_TITLE_PATTERN,
    clean_artist,
    clean_output_metadata,
    clean_title,
    preserve_supported_title_context,
    repair_misplaced_artist_title,
)


PERFORMER_CONTEXT_PATTERN = re.compile(
    r"\bkaraoke\b|\boriginally performed by\b",
    flags=re.I,
)


def preserve_cover_performer_context(
    output: dict[str, object], item: dict[str, object]
) -> dict[str, object]:
    """Do not relabel a named karaoke/cover performer as the original artist."""
    source_stem = Path(str(item.get("source", ""))).stem.strip()
    parts = re.split(r"\s+[-–—]\s+", source_stem, maxsplit=1)
    if len(parts) != 2 or not PERFORMER_CONTEXT_PATTERN.search(source_stem):
        return output
    source_artist = clean_artist(parts[0])
    source_title = clean_title(parts[1], source_artist or "")
    if not source_artist or not source_title:
        return output
    if (
        clean_artist(output.get("artist")) == source_artist
        and clean_title(output.get("title"), source_artist) == source_title
    ):
        return output
    repaired = dict(output)
    repaired["artist"] = source_artist
    repaired["title"] = source_title
    repaired["needsReview"] = True
    repaired["reviewReason"] = (
        "Filename identifies a karaoke or cover performer that differs from the "
        "model attribution; verify the exact recording identity."
    )
    return repaired


def default_output_path(path: Path) -> Path:
    return path.with_name(f"{path.stem}-clean{path.suffix}")


def load_manifest(path: Path) -> list[dict[str, object]]:
    with path.open("r", encoding="utf-8-sig") as file:
        value = json.load(file)
    if not isinstance(value, list):
        raise ValueError("Manifest root must be a JSON array.")
    return value


def postprocess_manifest(
    manifest: list[dict[str, object]],
) -> tuple[list[dict[str, object]], Counter[str], list[tuple[str, list[str]]]]:
    cleaned_manifest = copy.deepcopy(manifest)
    field_changes: Counter[str] = Counter()
    samples: list[tuple[str, list[str]]] = []

    for index, item in enumerate(cleaned_manifest, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"Manifest item {index} is not an object.")
        output = item.get("outputMetadata")
        if not isinstance(output, dict):
            continue
        cleaned = preserve_supported_title_context(
            repair_misplaced_artist_title(
                clean_output_metadata(output),
                item,
            ),
            item,
        )
        cleaned = preserve_cover_performer_context(cleaned, item)
        if not cleaned.get("title") and cleaned.get("artist"):
            source_stem = Path(str(item.get("source", ""))).stem
            recovered_title = clean_title(source_stem, str(cleaned["artist"]))
            if recovered_title and not PLACEHOLDER_TITLE_PATTERN.fullmatch(
                recovered_title
            ):
                cleaned["title"] = recovered_title
                cleaned["needsReview"] = False
                cleaned["reviewReason"] = None
        changed_fields = [
            field for field, value in cleaned.items() if output.get(field) != value
        ]
        if not changed_fields:
            continue
        item["outputMetadata"] = cleaned
        field_changes.update(changed_fields)
        if len(samples) < 20:
            samples.append((str(item.get("source", "")), changed_fields))

    return cleaned_manifest, field_changes, samples


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply conservative post-LLM cleanup to a music manifest."
    )
    parser.add_argument("manifest", type=Path, help="LLM-processed manifest JSON.")
    parser.add_argument("--out", type=Path, help="Clean JSON output path.")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow replacing an existing output file.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report changes without writing JSON.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_path = args.manifest.expanduser().resolve()
    output_path = (
        args.out.expanduser().resolve()
        if args.out
        else default_output_path(input_path)
    )

    if not input_path.is_file():
        print(f"Manifest does not exist: {input_path}", file=sys.stderr)
        return 1
    if output_path == input_path and not args.overwrite:
        print("Use --overwrite to replace the input manifest.", file=sys.stderr)
        return 1
    if output_path.exists() and not args.overwrite and not args.dry_run:
        print(f"Output already exists: {output_path}", file=sys.stderr)
        return 1

    try:
        manifest = load_manifest(input_path)
        cleaned, field_changes, samples = postprocess_manifest(manifest)
        if not args.dry_run:
            write_json_atomic(output_path, cleaned)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Cannot postprocess manifest: {exc}", file=sys.stderr)
        return 1

    changed_tracks = sum(
        1
        for before, after in zip(manifest, cleaned)
        if before.get("outputMetadata") != after.get("outputMetadata")
    )
    print(f"Tracks:          {len(manifest):,}")
    print(f"Changed tracks: {changed_tracks:,}")
    for field, count in sorted(field_changes.items()):
        print(f"  {field}: {count:,}")
    if samples:
        print("Samples:")
        for source, fields in samples:
            print(f"  {Path(source).name}: {', '.join(fields)}")
    print("Dry run only; no file written." if args.dry_run else f"Clean JSON:      {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
