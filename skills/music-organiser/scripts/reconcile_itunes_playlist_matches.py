#!/usr/bin/env python3
"""Rank unresolved legacy-iTunes playlist items against an existing djay library.

This is deliberately read-only.  It consumes one or more recovery plans made by
build_itunes_playlist_recovery_plan.py and produces an evidence file: no audio,
djay database, or playlist is modified.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
import unicodedata
from collections import defaultdict
from difflib import SequenceMatcher
from pathlib import Path


NOISE_WORDS = {"feat", "featuring", "ft", "the", "and", "with", "vs", "vs."}
VERSION_WORDS = {
    "remix", "mix", "edit", "extended", "radio", "club", "original", "bootleg",
    "vip", "mashup", "acapella", "instrumental", "version", "rework", "dub",
}


def folded(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()


def words(value: object) -> set[str]:
    return {word for word in folded(value).split() if word and word not in NOISE_WORDS}


def core_title(value: object) -> str:
    return " ".join(word for word in folded(value).split() if word not in VERSION_WORDS)


def ratio(left: str, right: str) -> float:
    return SequenceMatcher(None, left, right).ratio() if left and right else 0.0


def jaccard(left: set[str], right: set[str]) -> float:
    return len(left & right) / len(left | right) if left and right else 0.0


def version_words(value: object) -> set[str]:
    return words(value) & VERSION_WORDS


def score(source: dict, candidate: dict) -> dict:
    source_title, candidate_title = folded(source["title"]), folded(candidate["title"])
    source_artist, candidate_artist = folded(source["artist"]), folded(candidate["artist"])
    source_album, candidate_album = folded(source.get("album", "")), folded(candidate["album"])
    title_ratio = ratio(source_title, candidate_title)
    artist_ratio = max(ratio(source_artist, candidate_artist), jaccard(words(source_artist), words(candidate_artist)))
    album_ratio = ratio(source_album, candidate_album)
    source_core, candidate_core = core_title(source_title), core_title(candidate_title)
    core_equal = bool(source_core and source_core == candidate_core)
    source_versions, candidate_versions = version_words(source_title), version_words(candidate_title)
    version_overlap = bool(source_versions & candidate_versions)
    version_conflict = bool(source_versions and candidate_versions and not version_overlap)
    total = 65 * title_ratio + 25 * artist_ratio + 10 * album_ratio
    if core_equal:
        total += 15
    if version_overlap:
        total += 5
    if version_conflict:
        total -= 18
    total = round(max(0, min(100, total)), 1)
    return {
        "score": total,
        "title_ratio": round(title_ratio, 3),
        "artist_similarity": round(artist_ratio, 3),
        "album_similarity": round(album_ratio, 3),
        "core_title_equal": core_equal,
        "version_conflict": version_conflict,
    }


def confidence(item: dict, candidates: list[dict]) -> str:
    if not candidates:
        return "unmatched"
    first = candidates[0]
    gap = first["score"] - (candidates[1]["score"] if len(candidates) > 1 else 0)
    # Duplicate imports are common in the live library.  An exact identity remains
    # strong evidence even when two equivalent copies tie for first place.
    if (first["title_ratio"] >= 0.96 and first["artist_similarity"] >= 0.70
            and not first["version_conflict"]):
        return "high"
    if (first["core_title_equal"] and first["artist_similarity"] >= 0.60
            and first["score"] >= 80 and not first["version_conflict"]):
        return "high"
    if (first["score"] >= 68 and first["title_ratio"] >= 0.60
            and not first["version_conflict"] and (gap >= 5 or first["artist_similarity"] >= 0.60)):
        return "possible"
    return "review"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path, nargs="+", help="Recovery-plan JSON files")
    parser.add_argument("--djay-db", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument("--top", type=int, default=5)
    args = parser.parse_args()

    unresolved: list[dict] = []
    for plan_path in args.plan:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        for playlist in plan["playlists"]:
            for membership in playlist["memberships"]:
                if not membership.get("djay_media_key"):
                    unresolved.append({
                        "plan": str(plan_path), "playlist": playlist["destination_name"], **membership,
                    })

    uri = f"file:{args.djay_db.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        rows = connection.execute(
            "SELECT d.key, f.c0title, f.c1artist, f.c3album "
            "FROM database2 AS d JOIN fts_searchIndex_content AS f ON f.docid=d.rowid "
            "WHERE d.collection='mediaItems'"
        ).fetchall()
    finally:
        connection.close()
    library = [
        {"djay_media_key": str(key), "title": title or "", "artist": artist or "", "album": album or ""}
        for key, title, artist, album in rows if title
    ]

    title_index: dict[str, list[dict]] = defaultdict(list)
    exact_title_index: dict[str, list[dict]] = defaultdict(list)
    for candidate in library:
        exact_title_index[folded(candidate["title"])].append(candidate)
        for word in words(candidate["title"]):
            if len(word) >= 3:
                title_index[word].append(candidate)

    results: list[dict] = []
    for item in unresolved:
        source_words = {word for word in words(item["title"]) if len(word) >= 3}
        pool_by_key: dict[str, dict] = {}
        for candidate in exact_title_index.get(folded(item["title"]), []):
            pool_by_key[candidate["djay_media_key"]] = candidate
        for word in source_words:
            for candidate in title_index.get(word, []):
                pool_by_key[candidate["djay_media_key"]] = candidate
        ranked = []
        for candidate in pool_by_key.values():
            evidence = score(item, candidate)
            # Keep candidates with a meaningful approximate title relationship.
            if evidence["title_ratio"] >= 0.36 or evidence["core_title_equal"]:
                ranked.append({**candidate, **evidence})
        ranked.sort(key=lambda candidate: (-candidate["score"], candidate["title"].casefold(), candidate["djay_media_key"]))
        ranked = ranked[:args.top]
        results.append({**item, "confidence": confidence(item, ranked), "candidates": ranked})

    args.out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "djay_database": str(args.djay_db), "unresolved_count": len(results),
        "high_confidence_count": sum(row["confidence"] == "high" for row in results),
        "possible_count": sum(row["confidence"] == "possible" for row in results),
        "review_count": sum(row["confidence"] == "review" for row in results),
        "unmatched_count": sum(row["confidence"] == "unmatched" for row in results),
        "results": results,
    }
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with args.csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "playlist", "track_no", "artist", "title", "album", "confidence", "candidate_rank",
            "candidate_title", "candidate_artist", "candidate_album", "djay_media_key", "score",
            "title_ratio", "artist_similarity", "album_similarity", "core_title_equal", "version_conflict",
        ])
        writer.writeheader()
        for item in results:
            for rank, candidate in enumerate(item["candidates"], 1):
                writer.writerow({
                    "playlist": item["playlist"], "track_no": item["track_no"], "artist": item["artist"],
                    "title": item["title"], "album": item.get("album", ""), "confidence": item["confidence"],
                    "candidate_rank": rank, "candidate_title": candidate["title"],
                    "candidate_artist": candidate["artist"], "candidate_album": candidate["album"],
                    **{key: candidate[key] for key in ("djay_media_key", "score", "title_ratio", "artist_similarity", "album_similarity", "core_title_equal", "version_conflict")},
                })
    print(f"Unresolved memberships: {len(results)}")
    print(f"High confidence: {payload['high_confidence_count']}; possible: {payload['possible_count']}; review: {payload['review_count']}; unmatched: {payload['unmatched_count']}")
    print(f"Evidence: {args.out}")
    print(f"Candidates CSV: {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
