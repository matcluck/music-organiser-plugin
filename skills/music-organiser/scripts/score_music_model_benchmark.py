#!/usr/bin/env python3
"""Score a candidate cleaned manifest against a reviewed reference manifest."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from populate_music_manifest import write_json_atomic


FIELD_WEIGHTS = {
    "title": 4.0,
    "artist": 4.0,
    "albumArtist": 1.0,
    "album": 1.0,
    "date": 1.0,
    "trackNumber": 0.5,
    "discNumber": 0.5,
    "genre": 1.0,
    "compilation": 0.5,
    "needsReview": 1.0,
}


def normalized(value: object) -> object:
    if not isinstance(value, str):
        return value
    return re.sub(r"\s+", " ", value).strip().casefold()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    reference = json.loads(args.reference.read_text(encoding="utf-8"))
    candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    if not isinstance(reference, list) or not isinstance(candidate, list):
        parser.error("both manifests must be JSON arrays")
    count = min(len(reference), len(candidate), args.limit or len(reference))
    if count < 1:
        parser.error("no comparable tracks")

    exact = {field: 0 for field in FIELD_WEIGHTS}
    regressions: list[dict[str, object]] = []
    weighted_earned = 0.0
    weighted_total = 0.0
    for index in range(count):
        if reference[index].get("source") != candidate[index].get("source"):
            parser.error(f"source mismatch at index {index}")
        expected = reference[index].get("outputMetadata")
        actual = candidate[index].get("outputMetadata")
        if not isinstance(expected, dict) or not isinstance(actual, dict):
            parser.error(f"invalid outputMetadata at index {index}")
        row_differences: dict[str, dict[str, object]] = {}
        for field, weight in FIELD_WEIGHTS.items():
            weighted_total += weight
            if normalized(expected.get(field)) == normalized(actual.get(field)):
                exact[field] += 1
                weighted_earned += weight
            else:
                row_differences[field] = {
                    "expected": expected.get(field),
                    "actual": actual.get(field),
                }
        if row_differences:
            regressions.append(
                {
                    "id": index,
                    "source": reference[index].get("source"),
                    "differences": row_differences,
                }
            )

    report = {
        "reference": str(args.reference.resolve()),
        "candidate": str(args.candidate.resolve()),
        "tracks": count,
        "weightedExactPercent": round(100 * weighted_earned / weighted_total, 3),
        "fieldExact": {
            field: {
                "count": exact[field],
                "percent": round(100 * exact[field] / count, 3),
            }
            for field in FIELD_WEIGHTS
        },
        "tracksWithAnyDifference": len(regressions),
        "regressions": regressions,
    }
    print(f"Tracks:             {count}")
    print(f"Weighted exact:     {report['weightedExactPercent']:.3f}%")
    print(f"Exact title:        {exact['title']}/{count}")
    print(f"Exact artist:       {exact['artist']}/{count}")
    print(f"Tracks with a diff: {len(regressions)}/{count}")
    if args.out:
        write_json_atomic(args.out, report)
        print(f"Report:             {args.out.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
