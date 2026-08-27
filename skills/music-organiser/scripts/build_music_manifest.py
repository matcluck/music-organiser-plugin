#!/usr/bin/env python3
"""Scan a music folder into a loss-aware JSON metadata manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import time
from collections.abc import Mapping, Sequence
from enum import Enum
from pathlib import Path
from typing import Any

try:
    from mutagen import File as MutagenFile
except ModuleNotFoundError:  # pragma: no cover - user setup path
    print(
        "Missing dependency: mutagen. Install it with:\n"
        "  python -m pip install -r requirements.txt",
        file=sys.stderr,
    )
    raise SystemExit(2)


AUDIO_EXTENSIONS = {
    ".aac",
    ".aif",
    ".aiff",
    ".alac",
    ".ape",
    ".flac",
    ".m4a",
    ".m4b",
    ".mp3",
    ".mp4",
    ".mpc",
    ".ogg",
    ".opus",
    ".wav",
    ".wma",
    ".wv",
}

OUTPUT_METADATA_TEMPLATE = {
    "title": None,
    "artist": None,
    "albumArtist": None,
    "album": None,
    "date": None,
    "trackNumber": None,
    "discNumber": None,
    "genre": None,
    "compilation": False,
    "needsReview": False,
    "reviewReason": None,
}

IMAGE_KEY_MARKERS = (
    "apic",
    "attached picture",
    "cover art",
    "coverart",
    "covr",
    "metadata block picture",
    "metadata_block_picture",
    "picture",
    "wm/picture",
)

IMAGE_CLASS_NAMES = {"apic", "flacpicture", "mp4cover", "picture"}

MAX_ORIGINAL_TEXT_CHARS = 500
MAX_ORIGINAL_LIST_ITEMS = 50
MAX_TAG_KEY_LENGTH = 200


def normalized_key(value: object) -> str:
    return " ".join(str(value).lower().replace("_", " ").split())


def image_mime_from_bytes(value: bytes) -> str | None:
    if value.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if value.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if value.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if value.startswith(b"BM"):
        return "image/bmp"
    if value.startswith(b"RIFF") and value[8:12] == b"WEBP":
        return "image/webp"
    return None


def is_image_value(value: object, key_hint: str = "") -> bool:
    key = normalized_key(key_hint)
    if any(marker in key for marker in IMAGE_KEY_MARKERS):
        return True
    if type(value).__name__.lower() in IMAGE_CLASS_NAMES:
        return True
    if isinstance(value, (bytes, bytearray, memoryview)):
        return image_mime_from_bytes(bytes(value)) is not None
    return False


def image_value_count(value: object) -> int:
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray, memoryview)
    ):
        return max(1, len(value))
    return 1


def truncate_text(value: object, max_chars: int = MAX_ORIGINAL_TEXT_CHARS) -> str:
    text = str(value)
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars]}...[truncated {len(text) - max_chars} chars]"


def json_safe(value: object, key_hint: str = "", depth: int = 0) -> Any:
    """Convert useful Mutagen values into a bounded JSON representation."""
    if depth > 12:
        return {"type": type(value).__name__, "value": truncate_text(value)}
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, str):
        return truncate_text(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Enum):
        return value.value
    if is_image_value(value, key_hint):
        return {"exists": True, "count": image_value_count(value)}
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return {
            "exists": True,
            "byteLength": len(raw),
        }
    if isinstance(value, Mapping):
        return {
            str(key): json_safe(item, str(key), depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, str):
        result = [
            json_safe(item, key_hint, depth + 1)
            for item in value[:MAX_ORIGINAL_LIST_ITEMS]
        ]
        if len(value) > MAX_ORIGINAL_LIST_ITEMS:
            result.append(
                f"[{len(value) - MAX_ORIGINAL_LIST_ITEMS} additional values omitted]"
            )
        return result
    if isinstance(value, (set, frozenset)):
        return [json_safe(item, key_hint, depth + 1) for item in sorted(value, key=str)]

    attributes = getattr(value, "__dict__", None)
    if isinstance(attributes, dict) and attributes:
        result: dict[str, object] = {"type": type(value).__name__}
        for name, item in attributes.items():
            if not str(name).startswith("_"):
                result[str(name)] = json_safe(item, str(name), depth + 1)
        if len(result) > 1:
            return result

    inner_value = getattr(value, "value", None)
    if inner_value is not None and inner_value is not value:
        return json_safe(inner_value, key_hint, depth + 1)
    return truncate_text(value)


def iter_tag_items(tags: object) -> list[tuple[str, object]]:
    if tags is None or not hasattr(tags, "items"):
        return []
    try:
        prepared: list[tuple[str, object]] = []
        counts: dict[str, int] = {}
        for raw_key, value in tags.items():
            key = safe_tag_key(raw_key, value)
            counts[key] = counts.get(key, 0) + 1
            if counts[key] > 1:
                key = f"{key}#{counts[key]}"
            prepared.append((key, value))
        return sorted(prepared, key=lambda item: item[0].casefold())
    except Exception:
        return []


def safe_tag_key(
    raw_key: object,
    value: object,
    max_length: int = MAX_TAG_KEY_LENGTH,
) -> str:
    """Avoid ID3 HashKeys that embed complete PRIV/GEOB binary payloads."""
    class_name = type(value).__name__
    frame_id = getattr(value, "FrameID", None)
    if not frame_id and re.fullmatch(r"[A-Z][A-Z0-9]{2,4}", class_name):
        frame_id = class_name

    parts: list[str] = []
    if frame_id:
        parts.append(str(frame_id))
        for attribute in ("owner", "desc", "lang", "email"):
            item = getattr(value, attribute, None)
            if item:
                cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", str(item))
                cleaned = " ".join(cleaned.split())[:80]
                if cleaned and cleaned not in parts:
                    parts.append(cleaned)
        key = ":".join(parts)
    else:
        key = str(raw_key)

    key = re.sub(r"[\x00-\x1f\x7f]+", " ", key).strip() or class_name
    if len(key) <= max_length:
        return key
    digest = hashlib.sha256(key.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"{key[: max_length - 15]}...#{digest}"


def embedded_artwork_count(audio: object, tag_items: list[tuple[str, object]]) -> int:
    count = 0
    pictures = getattr(audio, "pictures", None)
    if pictures:
        try:
            count += len(pictures)
        except TypeError:
            count += 1

    for key, value in tag_items:
        if is_image_value(value, key):
            count += image_value_count(value)
    return count


def original_metadata(path: Path) -> tuple[dict[str, object], str | None]:
    try:
        audio = MutagenFile(path, easy=False)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        return {
            "tagFormat": None,
            "tags": {},
            "coverArt": {"exists": False, "count": 0},
            "readError": error,
        }, error

    if audio is None:
        error = "Mutagen could not identify this audio format"
        return {
            "tagFormat": None,
            "tags": {},
            "coverArt": {"exists": False, "count": 0},
            "readError": error,
        }, error

    tags = getattr(audio, "tags", None)
    tag_items = iter_tag_items(tags)
    artwork_count = embedded_artwork_count(audio, tag_items)
    serialized_tags = {
        key: json_safe(value, key)
        for key, value in tag_items
    }
    metadata: dict[str, object] = {
        "tagFormat": type(tags).__name__ if tags is not None else None,
        "tags": serialized_tags,
        "coverArt": {"exists": artwork_count > 0, "count": artwork_count},
    }
    return metadata, None


def iter_audio_files(source: Path):
    paths = (
        path
        for path in source.rglob("*")
        if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS
    )
    yield from sorted(paths, key=lambda path: str(path).casefold())


def write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(value, file, ensure_ascii=False, indent=2)
        file.write("\n")
    # Readers such as the local live monitor can briefly hold the destination
    # open on Windows.  Preserve atomic replacement, but tolerate a transient
    # sharing violation instead of terminating a long resumable model run.
    for attempt in range(10):
        try:
            temporary.replace(path)
            break
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(min(0.05 * (2**attempt), 0.5))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a source/originalMetadata/outputMetadata JSON music manifest."
    )
    parser.add_argument("source", type=Path, help="Folder containing the source music.")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("music-manifest.json"),
        help="Manifest path. Default: music-manifest.json",
    )
    parser.add_argument(
        "--limit", type=int, help="Scan only the first N tracks for a test run."
    )
    parser.add_argument(
        "--overwrite", action="store_true", help="Replace an existing manifest."
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = args.source.expanduser().resolve()
    output = args.out.expanduser().resolve()

    if not source.is_dir():
        print(f"Music folder does not exist: {source}", file=sys.stderr)
        return 1
    if output.exists() and not args.overwrite:
        print(f"Manifest already exists: {output}", file=sys.stderr)
        print("Use --overwrite to replace it.", file=sys.stderr)
        return 1
    if args.limit is not None and args.limit < 1:
        print("--limit must be at least 1", file=sys.stderr)
        return 1

    manifest: list[dict[str, object]] = []
    errors: list[tuple[Path, str]] = []
    for index, path in enumerate(iter_audio_files(source), start=1):
        if args.limit is not None and index > args.limit:
            break
        metadata, error = original_metadata(path)
        manifest.append(
            {
                "source": str(path),
                "originalMetadata": metadata,
                "outputMetadata": dict(OUTPUT_METADATA_TEMPLATE),
            }
        )
        if error:
            errors.append((path, error))
        if index == 1 or index % 100 == 0:
            print(f"Scanned {index} track(s)...")

    write_json_atomic(output, manifest)
    print(f"Tracks scanned: {len(manifest)}")
    print(f"Read errors:    {len(errors)}")
    print(f"Manifest:       {output}")
    for path, error in errors[:10]:
        print(f"  {path}: {error}", file=sys.stderr)
    if len(errors) > 10:
        print(f"  ...and {len(errors) - 10} more read errors", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
