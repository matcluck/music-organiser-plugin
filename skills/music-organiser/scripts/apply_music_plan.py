#!/usr/bin/env python3
"""Apply a verified music manifest/CSV plan without overwriting files."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

try:
    from mutagen import File as MutagenFile
    from mutagen.apev2 import APENoHeaderError, APEv2
    from mutagen.id3 import (
        APIC,
        TALB,
        TCMP,
        TCON,
        TDRC,
        TIT2,
        TPE1,
        TPE2,
        TPOS,
        TRCK,
        Encoding,
        ID3,
    )
    from mutagen.mp4 import MP4, MP4Cover
except ModuleNotFoundError:  # pragma: no cover - user setup path
    print(
        "Missing dependency: mutagen. Install it with:\n"
        "  python -m pip install -r requirements.txt",
        file=sys.stderr,
    )
    raise SystemExit(2)

from build_music_output_csv import (
    CSV_FIELDS,
    build_rows,
    clean_text,
    is_ready,
    load_manifest,
    output_extension,
    planned_path,
    positive_integer,
)


ID3_ALLOWED_FRAMES = {
    "APIC",
    "TALB",
    "TCMP",
    "TCON",
    "TDRC",
    "TIT2",
    "TPE1",
    "TPE2",
    "TPOS",
    "TRCK",
}

MP4_ALLOWED_KEYS = {
    "\xa9nam",
    "\xa9ART",
    "aART",
    "\xa9alb",
    "\xa9day",
    "trkn",
    "disk",
    "\xa9gen",
    "cpil",
    "covr",
}


class PlanError(ValueError):
    """Raised when a plan is unsafe or no longer matches its source data."""


@dataclass(frozen=True)
class PlanEntry:
    source: Path
    destination: Path
    metadata: dict[str, object]
    expected_artwork: int


def normalized_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath([normalized_path(path), normalized_path(root)]) == normalized_path(root)
    except ValueError:
        return False


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def metadata_values(output: dict[str, object]) -> dict[str, object]:
    return {
        "title": clean_text(output.get("title")),
        "artist": clean_text(output.get("artist")),
        "albumArtist": clean_text(output.get("albumArtist")),
        "album": clean_text(output.get("album")),
        "date": clean_text(output.get("date")),
        "trackNumber": positive_integer(output.get("trackNumber")),
        "discNumber": positive_integer(output.get("discNumber")),
        "genre": clean_text(output.get("genre")),
        "compilation": output.get("compilation") is True,
    }


def expected_artwork_count(item: dict[str, object]) -> int:
    original = item.get("originalMetadata")
    if not isinstance(original, dict):
        return 0
    artwork = original.get("coverArt")
    if not isinstance(artwork, dict):
        return 0
    count = artwork.get("count")
    return count if isinstance(count, int) and count > 0 else 0


def load_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        if reader.fieldnames != CSV_FIELDS:
            raise PlanError(
                f"Plan columns do not match the expected schema: {reader.fieldnames}"
            )
        return list(reader)


def load_skip_evidence(paths: list[Path]) -> dict[str, str]:
    """Load fail-closed skip evidence emitted by the collision resolvers."""
    evidence: dict[str, str] = {}
    for path in paths:
        with path.open("r", encoding="utf-8-sig", newline="") as file:
            reader = csv.DictReader(file)
            fields = set(reader.fieldnames or [])
            if {"sourcePath", "newPath", "recommendation"}.issubset(fields):
                source_field, destination_field = "sourcePath", "newPath"
                for row_number, row in enumerate(reader, start=2):
                    if row.get("recommendation", "").strip() != "skip_existing":
                        raise PlanError(
                            f"Skip evidence row {row_number} has an unsafe recommendation: {path}"
                        )
                    _add_skip_evidence(evidence, row, source_field, destination_field, path)
            elif {
                "skippedSourcePath", "skippedDestination", "reason"
            }.issubset(fields):
                source_field, destination_field = "skippedSourcePath", "skippedDestination"
                allowed = {
                    "same_destination", "same_identity",
                    "same_destination+same_identity",
                }
                for row_number, row in enumerate(reader, start=2):
                    if row.get("reason", "").strip() not in allowed:
                        raise PlanError(
                            f"Skip evidence row {row_number} has an unsafe reason: {path}"
                        )
                    if not row.get("keptSourcePath", "").strip():
                        raise PlanError(
                            f"Skip evidence row {row_number} lacks a retained source: {path}"
                        )
                    _add_skip_evidence(evidence, row, source_field, destination_field, path)
            else:
                raise PlanError(f"Unrecognized skip-evidence schema: {path}")
    return evidence


def _add_skip_evidence(
    evidence: dict[str, str],
    row: dict[str, str],
    source_field: str,
    destination_field: str,
    evidence_path: Path,
) -> None:
    # Paths are opaque: collapsing whitespace can silently change a valid
    # filename (including adjacent spaces inherited from source media).
    source = str(row.get(source_field) or "").strip()
    destination = str(row.get(destination_field) or "").strip()
    if not source or not destination:
        raise PlanError(f"Skip evidence contains a blank path: {evidence_path}")
    source_key = normalized_path(Path(source))
    destination_key = normalized_path(Path(destination))
    previous = evidence.get(source_key)
    if previous is not None and previous != destination_key:
        raise PlanError(f"Conflicting skip evidence for: {source}")
    evidence[source_key] = destination_key


def comparable_row(row: dict[str, object]) -> dict[str, str]:
    return {field: "" if row.get(field) is None else str(row.get(field)) for field in CSV_FIELDS}


def validate_contract(
    manifest: list[dict[str, object]],
    csv_rows: list[dict[str, str]],
    source_root: Path,
    destination_root: Path,
    *,
    strict_duplicate_check: bool,
    skip_evidence: dict[str, str] | None = None,
) -> list[PlanEntry]:
    if len(manifest) != len(csv_rows):
        raise PlanError(
            f"Manifest/CSV row count differs: {len(manifest):,} vs {len(csv_rows):,}."
        )

    by_source: dict[str, dict[str, object]] = {}
    for index, item in enumerate(manifest, start=1):
        if not isinstance(item, dict):
            raise PlanError(f"Manifest item {index} is not an object.")
        source = Path(str(item.get("source", "")))
        key = normalized_path(source)
        if key in by_source:
            raise PlanError(f"Manifest contains a duplicate source: {source}")
        if not is_within(source, source_root):
            raise PlanError(f"Manifest source is outside the source root: {source}")
        by_source[key] = item

    skip_evidence = skip_evidence or {}
    used_skip_evidence: set[str] = set()
    if strict_duplicate_check:
        expected_rows, _, _ = build_rows(manifest, destination_root)
        for index, (expected, actual) in enumerate(
            zip(expected_rows, csv_rows, strict=True), start=2
        ):
            if comparable_row(expected) != comparable_row(actual):
                differing = [
                    field
                    for field in CSV_FIELDS
                    if comparable_row(expected)[field] != comparable_row(actual)[field]
                ]
                source_key = normalized_path(Path(actual.get("sourcePath", "")))
                expected_destination = clean_text(expected.get("newPath"))
                evidenced_skip = (
                    differing == ["newPath"]
                    and bool(expected_destination)
                    and not clean_text(actual.get("newPath"))
                    and skip_evidence.get(source_key)
                    == normalized_path(Path(expected_destination))
                )
                if not evidenced_skip:
                    raise PlanError(
                        f"CSV row {index} differs from the manifest-derived plan: "
                        f"{', '.join(differing)}. Regenerate the CSV or supply exact skip evidence."
                    )
                used_skip_evidence.add(source_key)

        relevant_evidence = set(skip_evidence).intersection(by_source)
        unused = relevant_evidence.difference(used_skip_evidence)
        if unused:
            source = by_source[next(iter(unused))].get("source", "")
            raise PlanError(f"Skip evidence is stale or unused for: {source}")

    entries: list[PlanEntry] = []
    destinations: set[str] = set()
    destination_identities: set[str] = set()
    planned_sources: set[str] = set()

    for row_number, row in enumerate(csv_rows, start=2):
        source = Path(row["sourcePath"])
        item = by_source.get(normalized_path(source))
        if item is None:
            raise PlanError(f"CSV row {row_number} is missing from the manifest: {source}")
        output = item.get("outputMetadata")
        if not isinstance(output, dict):
            output = {}

        expected_metadata = {
            "title": clean_text(output.get("title")),
            "artist": clean_text(output.get("artist")),
            "albumArtist": clean_text(output.get("albumArtist")),
            "album": clean_text(output.get("album")),
            "date": clean_text(output.get("date")),
            "trackNumber": str(positive_integer(output.get("trackNumber")) or ""),
            "discNumber": str(positive_integer(output.get("discNumber")) or ""),
            "genre": clean_text(output.get("genre")),
            "compilation": "true" if output.get("compilation") is True else "false",
        }
        for field, expected in expected_metadata.items():
            if row[field] != expected:
                raise PlanError(
                    f"CSV row {row_number} field {field} differs from the manifest."
                )

        new_path = clean_text(row.get("newPath"))
        if not new_path:
            continue
        if not is_ready(output):
            raise PlanError(f"CSV row {row_number} plans a track still marked for review.")

        destination = Path(new_path)
        extension = item.get("outputExtension")
        derived = planned_path(source, destination_root, output, extension)
        if derived is None or normalized_path(derived) != normalized_path(destination):
            raise PlanError(
                f"CSV row {row_number} destination is not derived from the manifest: "
                f"{destination}"
            )
        if not is_within(destination, destination_root):
            raise PlanError(f"Destination escapes the destination root: {destination}")
        expected_extension = output_extension(extension, source.suffix)
        if expected_extension != destination.suffix.casefold():
            raise PlanError(
                f"Destination extension differs from the manifest: {source} -> {destination}"
            )

        source_key = normalized_path(source)
        destination_key = normalized_path(destination)
        identity_key = normalized_path(destination.parent / destination.stem)
        if source_key in planned_sources:
            raise PlanError(f"Source is planned more than once: {source}")
        if destination_key in destinations:
            raise PlanError(f"Destination is planned more than once: {destination}")
        if identity_key in destination_identities:
            raise PlanError(
                f"Destination identity collides across extensions: {destination.parent / destination.stem}"
            )
        planned_sources.add(source_key)
        destinations.add(destination_key)
        destination_identities.add(identity_key)
        entries.append(
            PlanEntry(
                source=source,
                destination=destination,
                metadata=metadata_values(output),
                expected_artwork=expected_artwork_count(item),
            )
        )

    return entries


def audio_kind_and_artwork(path: Path) -> tuple[str, int]:
    try:
        audio = MutagenFile(path, easy=False)
    except Exception as exc:
        if is_repairable_aiff(path, exc):
            return "AIFF_REPAIR", 0
        raise PlanError(f"Cannot read audio file {path}: {type(exc).__name__}: {exc}") from exc
    if audio is None:
        raise PlanError(f"Mutagen cannot identify audio file: {path}")
    if isinstance(audio, MP4):
        tags = audio.tags or {}
        return "MP4", len(tags.get("covr", []))
    if isinstance(getattr(audio, "tags", None), ID3) or path.suffix.casefold() in {
        ".mp3",
        ".wav",
        ".aif",
        ".aiff",
    }:
        tags = getattr(audio, "tags", None)
        return "ID3", len(tags.getall("APIC")) if isinstance(tags, ID3) else 0
    raise PlanError(
        f"Unsupported tag format for planned track {path}: {type(audio).__name__}"
    )


def image_mime(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"BM"):
        return "image/bmp"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if data.startswith(b"\x00\x00\x01\x00"):
        return "image/x-icon"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if len(data) >= 12 and data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in {b"avif", b"avis"}:
            return "image/avif"
        if brand in {b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"}:
            return "image/heic"
    return None


def normalized_id3_picture(picture: APIC) -> tuple[bytes, str] | None:
    data = bytes(picture.data)
    if not data or not any(data):
        return None

    try:
        description_prefix = picture.desc.encode("latin-1")
    except UnicodeEncodeError:
        description_prefix = b""
    if description_prefix:
        repaired = description_prefix + b"\x00" + data
        mime = image_mime(repaired)
        if mime:
            return repaired, mime

    mime = image_mime(data)
    if mime:
        return data, mime
    if data.startswith(b"\x00"):
        repaired = data[1:]
        mime = image_mime(repaired)
        if mime:
            return repaired, mime

    if picture.desc.startswith("\x89PNG") and data.startswith(b"\x00\x00\x0dIHDR"):
        repaired = b"\x89PNG\r\n\x1a\n\x00" + data
        return repaired, "image/png"
    return None


def artwork_hashes(path: Path) -> list[str]:
    try:
        audio = MutagenFile(path, easy=False)
    except Exception as exc:
        if is_repairable_aiff(path, exc):
            return []
        raise PlanError(f"Cannot read artwork from {path}: {exc}") from exc
    if audio is None:
        raise PlanError(f"Mutagen cannot identify audio file: {path}")
    if isinstance(audio, MP4):
        values = (audio.tags or {}).get("covr", [])
        return sorted(hashlib.sha256(bytes(value)).hexdigest() for value in values)
    tags = getattr(audio, "tags", None)
    if isinstance(tags, ID3):
        hashes = []
        for picture in tags.getall("APIC"):
            normalized = normalized_id3_picture(picture)
            if normalized is not None:
                hashes.append(hashlib.sha256(normalized[0]).hexdigest())
        return sorted(hashes)
    return []


def is_repairable_aiff(path: Path, error: Exception) -> bool:
    if path.suffix.casefold() not in {".aif", ".aiff"}:
        return False
    if "ID3v2.32 not supported" not in str(error):
        return False
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return False
    probe = subprocess.run(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=format_name:stream=codec_type:stream_disposition=attached_pic",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        return False
    try:
        result = json.loads(probe.stdout)
    except json.JSONDecodeError:
        return False
    format_name = str(result.get("format", {}).get("format_name", ""))
    streams = result.get("streams", [])
    return (
        "aiff" in format_name.split(",")
        and isinstance(streams, list)
        and len(streams) == 1
        and streams[0].get("codec_type") == "audio"
        and streams[0].get("disposition", {}).get("attached_pic", 0) == 0
    )


def repair_aiff_container(path: Path, error: Exception) -> None:
    if not is_repairable_aiff(path, error):
        raise PlanError(f"Cannot repair malformed AIFF tags in {path}: {error}") from error
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise PlanError("ffmpeg is required to repair the malformed AIFF tag chunk.")
    repaired = path.with_name(f".{path.stem}.repair-{uuid.uuid4().hex}{path.suffix}")
    try:
        process = subprocess.run(
            [
                ffmpeg,
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-c:a",
                "copy",
                "-map_metadata",
                "-1",
                str(repaired),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if process.returncode != 0 or not repaired.is_file():
            detail = process.stderr.strip() or f"ffmpeg exit code {process.returncode}"
            raise PlanError(f"ffmpeg could not repair {path}: {detail}")
        os.replace(repaired, path)
    finally:
        if repaired.exists():
            repaired.unlink()


def id3_text(tags: ID3, frame_id: str) -> str:
    frames = tags.getall(frame_id)
    if not frames:
        return ""
    values = getattr(frames[0], "text", [])
    return clean_text(values[0]) if values else ""


def verify_rewritten_file(
    path: Path,
    metadata: dict[str, object],
    expected_artwork: int,
) -> None:
    audio = MutagenFile(path, easy=False)
    if audio is None:
        raise PlanError(f"Cannot verify rewritten audio file: {path}")

    if isinstance(audio, MP4):
        tags = audio.tags or {}
        unexpected = set(tags) - MP4_ALLOWED_KEYS
        if unexpected:
            raise PlanError(f"Unexpected MP4 tags remain in {path}: {sorted(unexpected)}")

        def first(key: str) -> str:
            value = tags.get(key, [])
            if not isinstance(value, list):
                return clean_text(value)
            return clean_text(value[0]) if value else ""

        observed = {
            "title": first("\xa9nam"),
            "artist": first("\xa9ART"),
            "albumArtist": first("aART"),
            "album": first("\xa9alb"),
            "date": first("\xa9day"),
            "trackNumber": (tags.get("trkn") or [(None, 0)])[0][0],
            "discNumber": (tags.get("disk") or [(None, 0)])[0][0],
            "genre": first("\xa9gen"),
            "compilation": bool(tags.get("cpil", False)),
        }
        artwork_count = len(tags.get("covr", []))
    elif isinstance(getattr(audio, "tags", None), ID3):
        tags = audio.tags
        assert isinstance(tags, ID3)
        unexpected = {frame.FrameID for frame in tags.values()} - ID3_ALLOWED_FRAMES
        if unexpected:
            raise PlanError(f"Unexpected ID3 tags remain in {path}: {sorted(unexpected)}")
        observed = {
            "title": id3_text(tags, "TIT2"),
            "artist": id3_text(tags, "TPE1"),
            "albumArtist": id3_text(tags, "TPE2"),
            "album": id3_text(tags, "TALB"),
            "date": id3_text(tags, "TDRC"),
            "trackNumber": positive_integer(id3_text(tags, "TRCK")),
            "discNumber": positive_integer(id3_text(tags, "TPOS")),
            "genre": id3_text(tags, "TCON"),
            "compilation": id3_text(tags, "TCMP") == "1",
        }
        raw_pictures = tags.getall("APIC")
        normalized_pictures = [
            normalized_id3_picture(picture) for picture in raw_pictures
        ]
        if any(
            picture is None or picture[0] != bytes(raw.data)
            for raw, picture in zip(raw_pictures, normalized_pictures, strict=True)
        ):
            raise PlanError(f"Invalid or non-canonical artwork remains in {path}.")
        artwork_count = len(normalized_pictures)
    else:
        raise PlanError(f"Unsupported rewritten tag format: {path}")

    if observed != metadata:
        differing = [key for key in metadata if observed.get(key) != metadata.get(key)]
        raise PlanError(f"Metadata verification failed for {path}: {', '.join(differing)}")
    if artwork_count != expected_artwork:
        raise PlanError(
            f"Artwork verification failed for {path}: expected {expected_artwork}, "
            f"found {artwork_count}."
        )
    if path.suffix.casefold() == ".mp3":
        try:
            APEv2(path)
        except APENoHeaderError:
            pass
        else:
            raise PlanError(f"An APEv2 tag remains in rewritten MP3: {path}")
        with path.open("rb") as file:
            if path.stat().st_size >= 128:
                file.seek(-128, os.SEEK_END)
                if file.read(3) == b"TAG":
                    raise PlanError(f"An ID3v1 tag remains in rewritten MP3: {path}")


def rewrite_tags(path: Path, metadata: dict[str, object]) -> int:
    try:
        audio = MutagenFile(path, easy=False)
    except Exception as exc:
        repair_aiff_container(path, exc)
        audio = MutagenFile(path, easy=False)
    if audio is None:
        raise PlanError(f"Mutagen cannot identify copied audio file: {path}")

    if isinstance(audio, MP4):
        if audio.tags is None:
            audio.add_tags()
        assert audio.tags is not None
        covers = [
            MP4Cover(bytes(cover), imageformat=getattr(cover, "imageformat", MP4Cover.FORMAT_JPEG))
            for cover in audio.tags.get("covr", [])
        ]
        audio.tags.clear()
        mapping = {
            "\xa9nam": metadata["title"],
            "\xa9ART": metadata["artist"],
            "aART": metadata["albumArtist"],
            "\xa9alb": metadata["album"],
            "\xa9day": metadata["date"],
            "\xa9gen": metadata["genre"],
        }
        for key, value in mapping.items():
            if value:
                audio.tags[key] = [value]
        if metadata["trackNumber"] is not None:
            audio.tags["trkn"] = [(metadata["trackNumber"], 0)]
        if metadata["discNumber"] is not None:
            audio.tags["disk"] = [(metadata["discNumber"], 0)]
        if metadata["compilation"] is True:
            audio.tags["cpil"] = True
        if covers:
            audio.tags["covr"] = covers
        audio.save()
        return len(covers)

    if getattr(audio, "tags", None) is None:
        audio.add_tags()
    tags = audio.tags
    if not isinstance(tags, ID3):
        raise PlanError(f"Unsupported writable tag format for {path}: {type(tags).__name__}")
    pictures = []
    used_descriptions: set[str] = set()
    for picture in tags.getall("APIC"):
        normalized = normalized_id3_picture(picture)
        if normalized is None:
            continue
        picture_data, picture_mime = normalized
        output_index = len(pictures) + 1
        description = picture.desc.strip()
        if (
            not description
            or picture_data != bytes(picture.data)
            or image_mime(description.encode("latin-1", errors="ignore"))
            or description.casefold() in used_descriptions
        ):
            description = "Cover" if output_index == 1 else f"Artwork {output_index}"
        while description.casefold() in used_descriptions:
            description += " Copy"
        used_descriptions.add(description.casefold())
        pictures.append(
            APIC(
                encoding=Encoding.UTF16,
                mime=picture_mime,
                type=picture.type,
                desc=description,
                data=picture_data,
            )
        )
    tags.clear()
    encoding = Encoding.UTF16
    tags.add(TIT2(encoding=encoding, text=[metadata["title"]]))
    tags.add(TPE1(encoding=encoding, text=[metadata["artist"]]))
    if metadata["albumArtist"]:
        tags.add(TPE2(encoding=encoding, text=[metadata["albumArtist"]]))
    if metadata["album"]:
        tags.add(TALB(encoding=encoding, text=[metadata["album"]]))
    if metadata["date"]:
        tags.add(TDRC(encoding=encoding, text=[metadata["date"]]))
    if metadata["trackNumber"] is not None:
        tags.add(TRCK(encoding=encoding, text=[str(metadata["trackNumber"])]))
    if metadata["discNumber"] is not None:
        tags.add(TPOS(encoding=encoding, text=[str(metadata["discNumber"])]))
    if metadata["genre"]:
        tags.add(TCON(encoding=encoding, text=[metadata["genre"]]))
    if metadata["compilation"] is True:
        tags.add(TCMP(encoding=encoding, text=["1"]))
    for picture in pictures:
        tags.add(picture)
    if path.suffix.casefold() == ".mp3":
        audio.save(v2_version=3, v1=0)
        remove_all_id3v1(path)
        remove_all_apev2(path)
        remove_all_id3v1(path)
    else:
        audio.save(v2_version=3)
    return len(pictures)


def preflight(
    entries: list[PlanEntry],
    *,
    resume: bool,
    progress_every: int,
) -> tuple[int, int]:
    source_bytes = 0
    existing_destinations = 0
    errors: list[str] = []
    for index, entry in enumerate(entries, start=1):
        try:
            source_exists = entry.source.is_file()
            destination_exists = entry.destination.is_file()
            if not destination_exists and not source_exists:
                raise PlanError(f"Source is missing: {entry.source}")

            source_artwork: list[str] | None = None
            if source_exists:
                source_bytes += entry.source.stat().st_size
                _, artwork_count = audio_kind_and_artwork(entry.source)
                if artwork_count != entry.expected_artwork:
                    raise PlanError(
                        f"Source artwork changed since the manifest was built: {entry.source} "
                        f"(manifest {entry.expected_artwork}, file {artwork_count})"
                    )
                source_artwork = artwork_hashes(entry.source)

            if destination_exists:
                existing_destinations += 1
                if not resume:
                    raise PlanError(f"Destination already exists: {entry.destination}")
                destination_artwork = artwork_hashes(entry.destination)
                effective_artwork_count = len(
                    source_artwork
                    if source_artwork is not None
                    else destination_artwork
                )
                verify_rewritten_file(
                    entry.destination, entry.metadata, effective_artwork_count
                )
                if source_artwork is not None and source_artwork != destination_artwork:
                    raise PlanError(
                        f"Destination artwork bytes differ from the source: {entry.destination}"
                    )
        except (OSError, PlanError) as exc:
            if len(errors) < 50:
                errors.append(str(exc))
        if progress_every and (index % progress_every == 0 or index == len(entries)):
            print(f"Preflight: {index:,}/{len(entries):,}")

    if errors:
        sample = "\n  ".join(errors)
        raise PlanError(f"Preflight failed with errors (showing up to 50):\n  {sample}")
    return source_bytes, existing_destinations


def nearest_existing_parent(path: Path) -> Path:
    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    return current


def validate_free_space(
    entries: list[PlanEntry],
    destination_root: Path,
    source_bytes: int,
    action: str,
) -> None:
    existing_parent = nearest_existing_parent(destination_root)
    free = shutil.disk_usage(existing_parent).free
    same_volume = bool(entries) and os.path.splitdrive(entries[0].source)[0].casefold() == os.path.splitdrive(destination_root)[0].casefold()
    largest = max((entry.source.stat().st_size for entry in entries if entry.source.exists()), default=0)
    required = largest if action == "move" and same_volume else source_bytes
    reserve = 256 * 1024 * 1024
    if free < required + reserve:
        raise PlanError(
            f"Insufficient free space on {existing_parent}: need approximately "
            f"{required + reserve:,} bytes, found {free:,}."
        )


def journal_header(
    manifest_path: Path,
    plan_path: Path,
    source_root: Path,
    destination_root: Path,
    action: str,
) -> dict[str, object]:
    return {
        "type": "header",
        "version": 1,
        "manifest": str(manifest_path),
        "manifestSha256": file_sha256(manifest_path),
        "plan": str(plan_path),
        "planSha256": file_sha256(plan_path),
        "sourceRoot": str(source_root),
        "destinationRoot": str(destination_root),
        "action": action,
    }


def open_journal(
    path: Path,
    header: dict[str, object],
    *,
    resume: bool,
):
    path.parent.mkdir(parents=True, exist_ok=True)
    if resume:
        if not path.is_file():
            raise PlanError(f"Resume journal does not exist: {path}")
        with path.open("r", encoding="utf-8") as file:
            first = file.readline()
        try:
            existing = json.loads(first)
        except json.JSONDecodeError as exc:
            raise PlanError(f"Resume journal has an invalid header: {path}") from exc
        if existing != header:
            raise PlanError("Resume journal does not match this manifest, CSV, roots, and action.")
        return path.open("a", encoding="utf-8", newline="\n")
    if path.exists():
        raise PlanError(f"Journal already exists; use --resume or choose --journal: {path}")
    file = path.open("x", encoding="utf-8", newline="\n")
    file.write(json.dumps(header, ensure_ascii=False) + "\n")
    file.flush()
    return file


def append_journal(file, value: dict[str, object]) -> None:
    file.write(json.dumps(value, ensure_ascii=False) + "\n")
    file.flush()


def publish_without_overwrite(temporary: Path, destination: Path) -> None:
    try:
        os.link(temporary, destination)
    except FileExistsError as exc:
        raise PlanError(f"Refusing to overwrite destination: {destination}") from exc
    except OSError as exc:
        raise PlanError(
            f"Could not atomically publish {destination}; the destination filesystem "
            f"must support hard links: {exc}"
        ) from exc
    temporary.unlink()


def make_writable(path: Path) -> None:
    if path.exists():
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)


def remove_all_id3v1(path: Path, max_tags: int = 32) -> int:
    removed = 0
    for _ in range(max_tags):
        before = path.stat().st_size
        ID3().delete(path, delete_v1=True, delete_v2=False)
        if path.stat().st_size == before:
            return removed
        removed += 1
    raise PlanError(f"More than {max_tags} consecutive ID3v1 tags found in {path}.")


def remove_all_apev2(path: Path, max_tags: int = 8) -> int:
    """Remove APEv2 blocks without decoding their unwanted item values."""
    removed = 0
    for _ in range(max_tags):
        before = path.stat().st_size
        APEv2().delete(path)
        after = path.stat().st_size
        if after == before:
            return removed
        if after > before:
            raise PlanError(f"APEv2 removal unexpectedly grew {path}.")
        removed += 1
    raise PlanError(f"More than {max_tags} consecutive APEv2 tags found in {path}.")


def execute_entry(entry: PlanEntry, action: str) -> str:
    if entry.destination.exists():
        destination_artwork = artwork_hashes(entry.destination)
        if entry.source.is_file():
            source_artwork = artwork_hashes(entry.source)
            if destination_artwork != source_artwork:
                raise PlanError(
                    f"Destination artwork bytes differ from the source: {entry.destination}"
                )
            expected_artwork = len(source_artwork)
        else:
            expected_artwork = len(destination_artwork)
        verify_rewritten_file(entry.destination, entry.metadata, expected_artwork)
        if action == "move" and entry.source.exists():
            make_writable(entry.source)
            entry.source.unlink()
        return "resumed"

    if not entry.source.is_file():
        raise PlanError(f"Source disappeared after preflight: {entry.source}")
    entry.destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = entry.destination.with_name(
        f".__codex_{uuid.uuid4().hex[:12]}{entry.destination.suffix}"
    )
    try:
        source_artwork = artwork_hashes(entry.source)
        expected_artwork = len(source_artwork)
        shutil.copy2(entry.source, temporary)
        make_writable(temporary)
        artwork_count = rewrite_tags(temporary, entry.metadata)
        if artwork_count != expected_artwork:
            raise PlanError(
                f"Artwork was not preserved for {entry.source}: expected "
                f"{expected_artwork}, copied {artwork_count}."
            )
        verify_rewritten_file(temporary, entry.metadata, expected_artwork)
        if artwork_hashes(temporary) != source_artwork:
            raise PlanError(f"Artwork bytes changed while rewriting {entry.source}.")
        publish_without_overwrite(temporary, entry.destination)
        if action == "move":
            make_writable(entry.source)
            entry.source.unlink()
    finally:
        if temporary.exists():
            make_writable(temporary)
            temporary.unlink()
    return "moved" if action == "move" else "copied"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Verify and apply a manifest-derived music plan. Dry-run/preflight is the default."
        )
    )
    parser.add_argument("manifest", type=Path, help="Final cleaned manifest JSON.")
    parser.add_argument("plan", type=Path, help="CSV generated from that manifest.")
    parser.add_argument("source_root", type=Path, help="Root containing source tracks.")
    parser.add_argument("destination_root", type=Path, help="Root for organized tracks.")
    parser.add_argument("--apply", action="store_true", help="Actually write and move/copy files.")
    parser.add_argument(
        "--action", choices=["move", "copy"], default="copy", help="Default: copy."
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Accept and verify existing destinations; with --apply, resume the journal.",
    )
    parser.add_argument("--journal", type=Path, help="JSONL journal path.")
    parser.add_argument(
        "--skip-evidence",
        action="append",
        type=Path,
        default=[],
        help=(
            "Collision-resolver evidence authorizing blank newPath rows. "
            "Repeat for library and cross-run skip reports."
        ),
    )
    parser.add_argument("--limit", type=int, help="Apply only the first N planned tracks.")
    parser.add_argument(
        "--progress-every", type=int, default=250, help="Progress interval. Default: 250."
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest_path = args.manifest.expanduser().resolve()
    plan_path = args.plan.expanduser().resolve()
    source_root = args.source_root.expanduser().resolve()
    destination_root = args.destination_root.expanduser().resolve()
    journal_path = (
        args.journal.expanduser().resolve()
        if args.journal
        else plan_path.with_suffix(".apply.jsonl")
    )

    if args.limit is not None and args.limit < 1:
        print("--limit must be at least 1.", file=sys.stderr)
        return 1
    for path, label in ((manifest_path, "Manifest"), (plan_path, "Plan")):
        if not path.is_file():
            print(f"{label} does not exist: {path}", file=sys.stderr)
            return 1
    skip_evidence_paths = [path.expanduser().resolve() for path in args.skip_evidence]
    for path in skip_evidence_paths:
        if not path.is_file():
            print(f"Skip evidence does not exist: {path}", file=sys.stderr)
            return 1
    if not source_root.is_dir():
        print(f"Source root does not exist: {source_root}", file=sys.stderr)
        return 1

    try:
        manifest = load_manifest(manifest_path)
        csv_rows = load_csv_rows(plan_path)
        skip_evidence = load_skip_evidence(skip_evidence_paths)
        entries = validate_contract(
            manifest,
            csv_rows,
            source_root,
            destination_root,
            strict_duplicate_check=not args.resume or args.action == "copy",
            skip_evidence=skip_evidence,
        )
        source_bytes, existing_destinations = preflight(
            entries,
            resume=args.resume or args.action == "move",
            progress_every=args.progress_every,
        )
        validate_free_space(entries, destination_root, source_bytes, args.action)
    except (OSError, PlanError, json.JSONDecodeError) as exc:
        print(f"Cannot apply plan: {exc}", file=sys.stderr)
        return 1

    selected = entries[: args.limit] if args.limit else entries
    print(f"Manifest rows:        {len(manifest):,}")
    print(f"Plan rows:            {len(csv_rows):,}")
    print(f"Tracks to organize:   {len(entries):,}")
    print(f"Selected this run:    {len(selected):,}")
    print(f"Source bytes:         {source_bytes:,}")
    print(f"Existing/resumable:   {existing_destinations:,}")
    print("Contract:             JSON and CSV agree")
    print(f"Skip evidence files:   {len(skip_evidence_paths):,}")
    print("Metadata policy:      strict whitelist; embedded artwork preserved")

    if not args.apply:
        print("Dry run complete; no files were changed.")
        return 0

    header = journal_header(
        manifest_path,
        plan_path,
        source_root,
        destination_root,
        args.action,
    )
    try:
        journal = open_journal(journal_path, header, resume=args.resume)
    except (OSError, PlanError) as exc:
        print(f"Cannot open journal: {exc}", file=sys.stderr)
        return 1

    completed = 0
    try:
        with journal:
            for index, entry in enumerate(selected, start=1):
                try:
                    status = execute_entry(entry, args.action)
                except Exception as exc:
                    append_journal(
                        journal,
                        {
                            "type": "track",
                            "status": "failed",
                            "source": str(entry.source),
                            "destination": str(entry.destination),
                            "error": f"{type(exc).__name__}: {exc}",
                        },
                    )
                    raise
                completed += 1
                append_journal(
                    journal,
                    {
                        "type": "track",
                        "status": status,
                        "source": str(entry.source),
                        "destination": str(entry.destination),
                    },
                )
                if args.progress_every and (
                    index % args.progress_every == 0 or index == len(selected)
                ):
                    print(f"Applied: {index:,}/{len(selected):,}")
    except Exception as exc:
        print(f"Stopped after {completed:,} track(s): {exc}", file=sys.stderr)
        print(f"Resume with the same command plus --resume. Journal: {journal_path}")
        return 1

    print(f"Completed: {completed:,}")
    print(f"Journal:   {journal_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
