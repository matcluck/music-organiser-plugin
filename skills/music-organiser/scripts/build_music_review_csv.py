#!/usr/bin/env python3
"""Build a focused review CSV from a cleaned music manifest."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from build_music_output_csv import build_rows, clean_text, is_ready, load_manifest
from populate_music_manifest import (
    DJ_KEY_VALUE_PATTERN,
    casing_output_reasons,
    original_tag_text,
)


REVIEW_FIELDS = [
    "priority",
    "reasons",
    "reviewDecision",
    "reviewNotes",
    "sourcePath",
    "plannedPath",
    "title",
    "artist",
    "albumArtist",
    "album",
    "date",
    "trackNumber",
    "discNumber",
    "genre",
    "compilation",
    "needsReview",
    "reviewReason",
    "originalTitle",
    "originalArtist",
    "originalAlbumArtist",
    "originalAlbum",
    "originalKey",
]

OUTPUT_TEXT_FIELDS = ("title", "artist", "albumArtist", "album", "genre")
PRIORITY_RANK = {"Low": 1, "Medium": 2, "High": 3}

DOMAIN_PATTERN = re.compile(
    r"(?:https?://|www\.)|"
    r"\b[\w.-]+\.(?:com|net|org|co|io|ru|cc|tv|me|fm|biz|info|xyz|uk|au)"
    r"(?:\b|/)",
    flags=re.I,
)
PROMOTIONAL_PATTERN = re.compile(
    r"\b(?:free\s*(?:d/?l|download)|promo(?:tional)?|official\s+(?:audio|video)|"
    r"\d{2,3}(?:\.\d+)?\s*bpm)\b",
    flags=re.I,
)
PRODUCTION_SUFFIX_PATTERN = re.compile(
    r"(?:^|[\s_([])(?:premaster|master(?:ed)?|final(?:\s+master)?)"
    r"(?:\s+[A-Z]{2,10}[-_]?\d{2,8})?[)\]]?\s*$",
    flags=re.I,
)
MOJIBAKE_PATTERN = re.compile(r"\ufffd|\u00c3.|\u00c2.|\u00e2[\u0080-\u00bf]")
BRACKET_PAIRS = (("(", ")"), ("[", "]"), ("{", "}"))


@dataclass(frozen=True)
class Finding:
    reason: str
    priority: str


def has_unbalanced_brackets(value: str) -> bool:
    return any(value.count(opening) != value.count(closing) for opening, closing in BRACKET_PAIRS)


def audit_findings(item: dict[str, object]) -> list[Finding]:
    output = item.get("outputMetadata")
    if not isinstance(output, dict):
        return [Finding("invalid outputMetadata", "High")]

    findings: list[Finding] = []
    if output.get("needsReview") is True:
        findings.append(Finding("needs review", "High"))
    if not clean_text(output.get("title")) or not clean_text(output.get("artist")):
        findings.append(Finding("missing title or artist", "High"))

    title = clean_text(output.get("title"))
    if title and re.fullmatch(DJ_KEY_VALUE_PATTERN, title, flags=re.I):
        findings.append(Finding("title is a standalone DJ key", "High"))

    for field in OUTPUT_TEXT_FIELDS:
        value = clean_text(output.get(field))
        if not value:
            continue
        if DOMAIN_PATTERN.search(value):
            findings.append(Finding(f"{field} contains a URL or domain", "High"))
        if MOJIBAKE_PATTERN.search(value):
            findings.append(Finding(f"{field} contains corrupt encoding", "High"))
        if has_unbalanced_brackets(value):
            findings.append(Finding(f"{field} has unbalanced brackets", "High"))
        if "_" in value:
            findings.append(Finding(f"{field} contains underscore", "Medium"))
        if PROMOTIONAL_PATTERN.search(value) or PRODUCTION_SUFFIX_PATTERN.search(value):
            findings.append(Finding(f"{field} contains workflow or promo text", "Medium"))
        if field in {"artist", "albumArtist"} and len(value) > 100:
            findings.append(Finding(f"{field} is unusually long", "Medium"))

    for reason in casing_output_reasons(output):
        priority = "Medium" if reason.startswith(("artist ", "albumArtist ")) else "Low"
        findings.append(Finding(reason, priority))

    unique: dict[str, Finding] = {}
    for finding in findings:
        existing = unique.get(finding.reason)
        if existing is None or PRIORITY_RANK[finding.priority] > PRIORITY_RANK[existing.priority]:
            unique[finding.reason] = finding
    return list(unique.values())


def original_evidence(item: dict[str, object]) -> dict[str, str]:
    return {
        "originalTitle": original_tag_text(item, "TIT2", "title", "\u00a9nam") or "",
        "originalArtist": original_tag_text(item, "TPE1", "artist", "\u00a9ART") or "",
        "originalAlbumArtist": original_tag_text(
            item,
            "TPE2",
            "albumartist",
            "album artist",
            "aART",
        )
        or "",
        "originalAlbum": original_tag_text(item, "TALB", "album", "\u00a9alb") or "",
        "originalKey": original_tag_text(item, "TKEY", "initialkey") or "",
    }


def build_review_rows(
    manifest: list[dict[str, object]],
    destination_root: Path,
) -> tuple[list[dict[str, object]], Counter[str], int]:
    plan_rows, _review_blanks, _duplicate_blanks = build_rows(manifest, destination_root)
    review_rows: list[dict[str, object]] = []
    priority_counts: Counter[str] = Counter()
    excluded_duplicates = 0

    for item, plan_row in zip(manifest, plan_rows):
        output = item.get("outputMetadata")
        if not isinstance(output, dict):
            output = {}
        if output.get("excluded") is True:
            continue
        if is_ready(output) and not plan_row["newPath"]:
            excluded_duplicates += 1
            continue

        findings = audit_findings(item)
        if not findings:
            continue
        priority = max(findings, key=lambda finding: PRIORITY_RANK[finding.priority]).priority
        priority_counts[priority] += 1
        evidence = original_evidence(item)
        review_rows.append(
            {
                "priority": priority,
                "reasons": "; ".join(finding.reason for finding in findings),
                "reviewDecision": "Pending",
                "reviewNotes": "",
                "sourcePath": str(item.get("source", "")),
                "plannedPath": plan_row["newPath"],
                "title": clean_text(output.get("title")),
                "artist": clean_text(output.get("artist")),
                "albumArtist": clean_text(output.get("albumArtist")),
                "album": clean_text(output.get("album")),
                "date": clean_text(output.get("date")),
                "trackNumber": output.get("trackNumber") or "",
                "discNumber": output.get("discNumber") or "",
                "genre": clean_text(output.get("genre")),
                "compilation": "true" if output.get("compilation") is True else "false",
                "needsReview": "true" if output.get("needsReview") is True else "false",
                "reviewReason": clean_text(output.get("reviewReason")),
                **evidence,
            }
        )

    review_rows.sort(
        key=lambda row: (
            -PRIORITY_RANK[str(row["priority"])],
            str(row["artist"]).casefold(),
            str(row["album"]).casefold(),
            str(row["title"]).casefold(),
            str(row["sourcePath"]).casefold(),
        )
    )
    return review_rows, priority_counts, excluded_duplicates


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=REVIEW_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a focused CSV of music metadata requiring review."
    )
    parser.add_argument("manifest", type=Path, help="Cleaned music manifest JSON.")
    parser.add_argument("destination", type=Path, help="Future music library root.")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("music-output-review.csv"),
        help="Review CSV output path. Default: music-output-review.csv",
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
        rows, priorities, excluded_duplicates = build_review_rows(
            manifest, destination_root
        )
        write_csv(output_path, rows)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Cannot build review CSV: {exc}", file=sys.stderr)
        return 1

    print(f"Manifest tracks:    {len(manifest):,}")
    print(f"Review rows:        {len(rows):,}")
    for priority in ("High", "Medium", "Low"):
        print(f"  {priority:<6} {priorities[priority]:>8,}")
    print(f"Duplicate excluded: {excluded_duplicates:,}")
    print(f"Review CSV:         {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
