#!/usr/bin/env python3
"""Create a derived conservative audit without altering the source audit evidence."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from audit_music_manifest_parallel import actionable_review
from populate_music_manifest import write_json_atomic


CONTEXT_WORDS = ("remix", "bootleg", "vip", "edit", "mash-up", "mashup")


def normalized(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def proposed_title(feedback: str) -> str | None:
    match = re.search(r'(?:title should be|should be)\s+"([^"]+)"', feedback, re.I)
    if match:
        return match.group(1).strip()
    match = re.search(r"(?:title should be|should be)\s+'([^']+)'", feedback, re.I)
    return match.group(1).strip() if match else None


def conservative_actionable_review(
    feedback: str, item: dict[str, object]
) -> bool:
    if not actionable_review(feedback, item):
        return False
    output = item.get("outputMetadata")
    if not isinstance(output, dict):
        return False
    current_title = str(output.get("title") or "")
    current_artist = str(output.get("artist") or "")
    lower_feedback = feedback.casefold()

    # Version context is identity, not filename noise. Never let a verifier
    # erase it when the current title already preserves it.
    if any(word in lower_feedback for word in ("remove", "drop", "strip")):
        for word in CONTEXT_WORDS:
            if word in current_title.casefold() and word in lower_feedback:
                return False

    # The title field must not absorb an already-separated artist credit.
    proposed = proposed_title(feedback)
    artist_key = normalized(current_artist)
    if proposed and artist_key:
        proposed_key = normalized(proposed)
        current_key = normalized(current_title)
        if proposed_key.startswith(f"{artist_key} ") and not current_key.startswith(
            f"{artist_key} "
        ):
            return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("audit", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    manifest_path = args.manifest.expanduser().resolve()
    audit_path = args.audit.expanduser().resolve()
    output_path = args.out.expanduser().resolve()
    if output_path.exists() and not args.overwrite:
        parser.error(f"output already exists: {output_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, list) or not isinstance(audit, dict):
        parser.error("manifest must be an array and audit must be an object")
    reviews = audit.get("reviews")
    if not isinstance(reviews, list):
        parser.error("audit does not contain a reviews array")

    cleaned: list[dict[str, object]] = []
    rejected: list[dict[str, object]] = []
    for entry in reviews:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), int):
            parser.error("audit contains an invalid review entry")
        item_id = entry["id"]
        derived = dict(entry)
        if entry.get("needsRevision") is True:
            feedback = entry.get("feedback")
            keep = (
                0 <= item_id < len(manifest)
                and isinstance(feedback, str)
                and conservative_actionable_review(feedback, manifest[item_id])
            )
            if not keep:
                rejected.append(dict(entry))
                derived["needsRevision"] = False
                derived["feedback"] = None
        cleaned.append(derived)

    payload = dict(audit)
    payload["sourceAudit"] = str(audit_path)
    payload["reviews"] = cleaned
    payload["sanitization"] = {
        "policy": "conservative-supported-review-v1",
        "inputFlagged": sum(1 for row in reviews if row.get("needsRevision") is True),
        "retainedFlagged": sum(1 for row in cleaned if row.get("needsRevision") is True),
        "rejectedCount": len(rejected),
        "rejectedReviews": rejected,
    }
    write_json_atomic(output_path, payload)
    print(f"Input flagged:    {payload['sanitization']['inputFlagged']}")
    print(f"Retained flagged: {payload['sanitization']['retainedFlagged']}")
    print(f"Rejected noise:   {len(rejected)}")
    print(f"Derived audit:    {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
