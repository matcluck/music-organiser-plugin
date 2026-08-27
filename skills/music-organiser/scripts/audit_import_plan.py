#!/usr/bin/env python3
"""Find destination and metadata-identity collisions for a music import plan."""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

from music_library_index import (
    load_library_identities,
    normalize_identity,
    read_audio_identity,
    refresh_library_index,
)


FEATURE_CREDIT = re.compile(r"\s+(?:feat(?:uring)?\.?|ft\.?)\s+", re.IGNORECASE)
TITLE_FEATURE = re.compile(
    r"\s*[\[(](?:feat(?:uring)?\.?|ft\.?)\s+[^\])]+[\])]", re.IGNORECASE
)


def canonical_release_identity(artist: str | None, title: str | None) -> tuple[str, str]:
    """Conservatively align credits placed in artist vs title tag fields."""
    primary_artist = FEATURE_CREDIT.split(artist or "", maxsplit=1)[0]
    base_title = TITLE_FEATURE.sub("", title or "")
    return normalize_identity(primary_artist), normalize_identity(base_title)


def destination_is_same_release(
    destination: Path,
    artist: str | None,
    title: str | None,
) -> bool:
    existing_artist, existing_title, error = read_audio_identity(destination)
    if error:
        return False
    planned = canonical_release_identity(artist, title)
    existing = canonical_release_identity(existing_artist, existing_title)
    return all(planned) and planned == existing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("library", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument(
        "--index",
        type=Path,
        help="Persistent SQLite library index. Avoids rereading unchanged audio tags.",
    )
    parser.add_argument(
        "--refresh-index",
        action="store_true",
        help="Incrementally refresh --index before auditing the plan.",
    )
    parser.add_argument(
        "--verify-index-after-days",
        type=float,
        default=7.0,
        help="Full tag-verification age used with --refresh-index. Default: 7.",
    )
    parser.add_argument(
        "--force-index-verify",
        action="store_true",
        help="Force a complete tag reread while refreshing the index.",
    )
    args = parser.parse_args()

    if args.refresh_index and args.index is None:
        parser.error("--refresh-index requires --index")
    if args.index is not None:
        if args.refresh_index:
            stats = refresh_library_index(
                args.index,
                args.library,
                verify_after_days=args.verify_index_after_days,
                force_verify=args.force_index_verify,
            )
            print(
                f"Index refresh: {stats.reused:,} unchanged; "
                f"{stats.reindexed:,} metadata reread; {stats.removed:,} removed; "
                f"{stats.drifted:,} identity drift"
            )
            print(
                "Index verification: "
                + (stats.verification_trigger or "not due; stat reconciliation only")
            )
        identities, scanned, unreadable = load_library_identities(
            args.index, args.library
        )
    else:
        # Preserve the old one-shot behavior for callers that do not opt into a cache.
        import tempfile

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_index = Path(temporary_directory) / "library.sqlite"
            refresh_library_index(temporary_index, args.library)
            identities, scanned, unreadable = load_library_identities(
                temporary_index, args.library
            )

    with args.plan.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    findings: list[dict[str, str]] = []
    for row in rows:
        destination = row.get("newPath", "")
        if not destination:
            continue
        identity = (
            normalize_identity(row.get("artist")),
            normalize_identity(row.get("title")),
        )
        matches = identities.get(identity, []) if all(identity) else []
        destination_exists = Path(destination).is_file()
        if (
            destination_exists
            and not matches
            and destination_is_same_release(
                Path(destination), row.get("artist"), row.get("title")
            )
        ):
            # A common metadata drift is moving the featured performer between
            # artist and title. A path collision is safe to skip only when the
            # destination's own tags match this conservative release identity.
            matches = [destination]
        if destination_exists or matches:
            findings.append({
                "sourcePath": row.get("sourcePath", ""),
                "newPath": destination,
                "artist": row.get("artist", ""),
                "title": row.get("title", ""),
                "destinationExists": "yes" if destination_exists else "no",
                "existingIdentityPaths": " | ".join(matches),
                "recommendation": "skip_existing" if matches else "resolve_collision",
            })

    args.out.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "sourcePath", "newPath", "artist", "title", "destinationExists",
        "existingIdentityPaths", "recommendation",
    ]
    with args.out.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(findings)

    print(f"Library files scanned: {scanned:,}")
    print(f"Unreadable files:      {unreadable:,}")
    print(f"Plan collisions:       {len(findings):,}")
    print(f"Report:                {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
