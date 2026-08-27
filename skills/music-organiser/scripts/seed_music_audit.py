#!/usr/bin/env python3
"""Seed an audit checkpoint with reviews for byte-equivalent manifest rows."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from build_music_manifest import write_json_atomic


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def seed_reviews(
    prior_manifest: list[dict[str, object]],
    current_manifest: list[dict[str, object]],
    prior_audit: dict[str, object],
) -> tuple[list[dict[str, object]], list[int]]:
    if len(prior_manifest) != len(current_manifest):
        raise ValueError("Manifest row counts differ; audit checkpoint cannot be seeded.")
    prior_reviews = prior_audit.get("reviews")
    if not isinstance(prior_reviews, list):
        raise ValueError("Prior audit has no reviews array.")
    by_id = {
        row["id"]: row
        for row in prior_reviews
        if isinstance(row, dict) and isinstance(row.get("id"), int)
    }
    seeded: list[dict[str, object]] = []
    changed: list[int] = []
    for index, (old, new) in enumerate(zip(prior_manifest, current_manifest, strict=True)):
        if old.get("source") != new.get("source"):
            raise ValueError(f"Manifest source changed at index {index}.")
        if canonical(old) == canonical(new):
            review = by_id.get(index)
            if review is None:
                raise ValueError(f"Prior audit lacks unchanged manifest index {index}.")
            seeded.append(dict(review))
        else:
            changed.append(index)
    return seeded, changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("prior_manifest", type=Path)
    parser.add_argument("current_manifest", type=Path)
    parser.add_argument("prior_audit", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if args.out.exists() and not args.overwrite:
        parser.error(f"Output exists; pass --overwrite to replace it: {args.out}")

    prior_manifest = json.loads(args.prior_manifest.read_text(encoding="utf-8-sig"))
    current_manifest = json.loads(args.current_manifest.read_text(encoding="utf-8-sig"))
    prior_audit = json.loads(args.prior_audit.read_text(encoding="utf-8-sig"))
    if not isinstance(prior_manifest, list) or not isinstance(current_manifest, list):
        parser.error("Both manifests must be JSON arrays.")
    if not isinstance(prior_audit, dict):
        parser.error("Prior audit must be a JSON object.")
    reviews, changed = seed_reviews(prior_manifest, current_manifest, prior_audit)
    write_json_atomic(args.out, {
        "manifest": str(args.current_manifest.resolve()),
        "model": prior_audit.get("model"),
        "models": prior_audit.get("models", {}),
        "reviews": sorted(reviews, key=lambda row: row["id"]),
        "seededFrom": str(args.prior_audit.resolve()),
        "seededReviews": len(reviews),
        "recheckIndexes": changed,
    })
    print(f"Seeded unchanged reviews: {len(reviews):,}")
    print(f"Tracks requiring recheck: {len(changed):,}")
    print(f"Audit checkpoint:         {args.out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
