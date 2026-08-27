#!/usr/bin/env python3
"""Apply explicit evidence-backed review resolutions to a manifest copy."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path

from build_music_manifest import write_json_atomic
from populate_music_manifest import clean_output_metadata


ALLOWED_FIELDS = {
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
}


def apply_resolutions(
    manifest: list[dict[str, object]],
    resolutions: list[dict[str, object]],
    *,
    audited_sources: set[str] | None = None,
) -> list[dict[str, object]]:
    result = copy.deepcopy(manifest)
    by_source: dict[str, dict[str, object]] = {}
    for item in result:
        source = item.get("source")
        if not isinstance(source, str) or source.casefold() in by_source:
            raise ValueError("Manifest sources must be unique non-empty strings.")
        by_source[source.casefold()] = item

    seen: set[str] = set()
    for resolution in resolutions:
        source = resolution.get("source")
        changes = resolution.get("outputMetadata")
        evidence = resolution.get("evidence")
        if not isinstance(source, str) or not source:
            raise ValueError("Every resolution requires an exact source path.")
        key = source.casefold()
        if key in seen:
            raise ValueError(f"Duplicate resolution source: {source}")
        seen.add(key)
        item = by_source.get(key)
        if item is None:
            raise ValueError(f"Resolution source is absent from manifest: {source}")
        if not isinstance(changes, dict) or not changes:
            raise ValueError(f"Resolution has no outputMetadata changes: {source}")
        unknown = set(changes) - ALLOWED_FIELDS
        if unknown:
            raise ValueError(f"Unsupported resolution fields for {source}: {sorted(unknown)}")
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError(f"Resolution requires an evidence note: {source}")
        current = item.get("outputMetadata")
        audit_authorized = key in (audited_sources or set())
        marking_review = changes.get("needsReview") is True
        if not isinstance(current, dict) or (
            current.get("needsReview") is not True
            and not audit_authorized
            and not marking_review
        ):
            raise ValueError(
                f"Resolution target is not currently under review or audit-flagged: {source}"
            )
        merged = dict(current)
        merged.update(changes)
        cleaned = clean_output_metadata(merged)
        if marking_review:
            if cleaned.get("needsReview") is not True or not cleaned.get("reviewReason"):
                raise ValueError(
                    f"A review hold requires needsReview true and a reason: {source}"
                )
        else:
            if cleaned.get("needsReview") is not False:
                raise ValueError(f"Resolution must explicitly clear needsReview: {source}")
            if not cleaned.get("title") or not cleaned.get("artist"):
                raise ValueError(f"Resolution must retain title and artist: {source}")
        item["outputMetadata"] = cleaned
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply explicit evidence-backed metadata review resolutions."
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("resolutions", type=Path)
    parser.add_argument(
        "--audit",
        type=Path,
        help="Retained audit feedback authorizing corrections to non-review rows.",
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if args.out.exists() and not args.overwrite:
        parser.error(f"Output exists; pass --overwrite to replace it: {args.out}")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
    resolutions = json.loads(args.resolutions.read_text(encoding="utf-8-sig"))
    if not isinstance(manifest, list) or not isinstance(resolutions, list):
        parser.error("Manifest and resolutions must both be JSON arrays.")
    audited_sources: set[str] = set()
    if args.audit is not None:
        audit = json.loads(args.audit.read_text(encoding="utf-8-sig"))
        reviews = audit.get("reviews") if isinstance(audit, dict) else None
        if not isinstance(reviews, list):
            parser.error("Audit must be an object containing a reviews array.")
        for review in reviews:
            if not isinstance(review, dict) or review.get("needsRevision") is not True:
                continue
            identifier = review.get("id")
            if not isinstance(identifier, int) or not 0 <= identifier < len(manifest):
                parser.error(f"Audit contains an invalid track id: {identifier!r}")
            source = manifest[identifier].get("source")
            if not isinstance(source, str) or not source:
                parser.error(f"Manifest track index {identifier} has no source path.")
            audited_sources.add(source.casefold())
    resolved = apply_resolutions(
        manifest, resolutions, audited_sources=audited_sources
    )
    write_json_atomic(args.out, resolved)
    print(f"Resolved tracks: {len(resolutions)}")
    print(f"Output manifest: {args.out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
