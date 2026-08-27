#!/usr/bin/env python3
"""Build a read-only djay playlist-recovery plan from an iTunes XML export."""

from __future__ import annotations

import argparse
import csv
import json
import plistlib
import re
import sqlite3
import unicodedata
from collections import defaultdict
from pathlib import Path


def normalise(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def parse_playlist(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("Use SOURCE=DESTINATION for --playlist.")
    source, destination = (part.strip() for part in value.split("=", 1))
    if not source or not destination:
        raise argparse.ArgumentTypeError("Playlist source and destination names are required.")
    return source, destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("itunes_xml", type=Path)
    parser.add_argument("djay_db", type=Path)
    parser.add_argument("--playlist", action="append", type=parse_playlist, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--unmatched-csv", type=Path, required=True)
    args = parser.parse_args()

    with args.itunes_xml.open("rb") as handle:
        source = plistlib.load(handle)
    tracks = {str(key): value for key, value in source.get("Tracks", {}).items()}
    source_playlists = {item.get("Name"): item for item in source.get("Playlists", [])}

    uri = f"file:{args.djay_db.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        rows = connection.execute(
            "SELECT d.key, f.c0title, f.c1artist "
            "FROM database2 AS d JOIN fts_searchIndex_content AS f ON f.docid=d.rowid "
            "WHERE d.collection='mediaItems'"
        ).fetchall()
    finally:
        connection.close()

    exact: dict[tuple[str, str], list[str]] = defaultdict(list)
    title_only: dict[str, list[str]] = defaultdict(list)
    for key, title, artist in rows:
        title_key, artist_key = normalise(title), normalise(artist)
        if title_key:
            exact[(artist_key, title_key)].append(str(key))
            title_only[title_key].append(str(key))

    playlists: list[dict] = []
    unresolved: list[dict] = []
    for source_name, destination_name in args.playlist:
        playlist = source_playlists.get(source_name)
        if not isinstance(playlist, dict):
            raise RuntimeError(f"iTunes playlist not found: {source_name!r}")
        memberships: list[dict] = []
        for position, item in enumerate(playlist.get("Playlist Items", []), 1):
            track = tracks.get(str(item.get("Track ID")), {})
            artist, title = track.get("Artist", ""), track.get("Name", "")
            title_key, artist_key = normalise(title), normalise(artist)
            candidates = exact.get((artist_key, title_key), [])
            mode = "artist_title"
            if len(candidates) != 1 and len(title_only.get(title_key, [])) == 1:
                candidates = title_only[title_key]
                mode = "title_unique"
            entry = {
                "track_no": position,
                "source_track_id": str(item.get("Track ID", "")),
                "artist": artist,
                "title": title,
                "album": track.get("Album", ""),
                "djay_media_key": candidates[0] if len(candidates) == 1 else None,
                "match_mode": mode if len(candidates) == 1 else None,
                "candidate_count": len(candidates),
            }
            memberships.append(entry)
            if entry["djay_media_key"] is None:
                unresolved.append({"playlist": destination_name, **entry})
        playlists.append({
            "source_name": source_name,
            "destination_name": destination_name,
            "source_count": len(memberships),
            "memberships": memberships,
        })

    plan = {
        "source": str(args.itunes_xml),
        "djay_database": str(args.djay_db),
        "playlists": playlists,
        "unresolved_count": len(unresolved),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    with args.unmatched_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "playlist", "track_no", "artist", "title", "album",
            "candidate_count", "match_mode", "djay_media_key", "source_track_id",
        ])
        writer.writeheader()
        writer.writerows(unresolved)
    matched = sum(
        1 for playlist in playlists for item in playlist["memberships"]
        if item["djay_media_key"]
    )
    print(f"Playlists: {len(playlists)}")
    print(f"Matched memberships: {matched}")
    print(f"Unresolved memberships: {len(unresolved)}")
    print(f"Plan: {args.out}")
    print(f"Unmatched CSV: {args.unmatched_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
