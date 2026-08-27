#!/usr/bin/env python3
"""Second-pass local-model quality audit for cleaned music manifests."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from populate_music_manifest import (  # Reuse the verified local-LLM transport.
    discover_model,
    http_json,
    metadata_evidence,
    write_json_atomic,
)

SCHEMA = {
    "type": "object",
    "properties": {
        "reviews": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer", "minimum": 0},
                    "needsRevision": {"type": "boolean"},
                    "feedback": {"type": ["string", "null"]},
                },
                "required": ["id", "needsRevision", "feedback"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["reviews"],
    "additionalProperties": False,
}

SYSTEM = """You are a strict quality auditor for already-cleaned music metadata.
Compare outputMetadata only against supplied source filename and original tags.
Flag needsRevision true only for a concrete supported issue: missed filename/tag
noise, a misplaced field, an unsupported or contradictory value, lost meaningful
version text, or invalid casing. feedback must be a concise corrective instruction
grounded in the supplied evidence. Do not ask for optional album/date/genre facts,
do not invent facts, and do not flag a record merely because it lacks optional tags.
For sound records return needsRevision false and feedback null."""


def load_manifest(path: Path) -> list[dict[str, object]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("manifest must be a JSON array")
    return data


def request(endpoint: str, model: str, batch: list[tuple[int, dict[str, object]]], timeout: int) -> list[dict[str, object]]:
    items = []
    for item_id, item in batch:
        items.append({
            "id": item_id,
            "source": item.get("source"),
            "originalMetadata": metadata_evidence(item.get("originalMetadata", {}), 500, 3000),
            "outputMetadata": item.get("outputMetadata"),
        })
    schema = json.loads(json.dumps(SCHEMA))
    schema["properties"]["reviews"]["minItems"] = len(items)
    schema["properties"]["reviews"]["maxItems"] = len(items)
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": json.dumps({"tracks": items}, ensure_ascii=False)}],
        "temperature": 0,
        "max_tokens": min(1024, 96 + 96 * len(items)),
        "response_format": {"type": "json_object", "schema": schema},
        "stream": False,
    }
    response = http_json(endpoint.rstrip("/") + "/chat/completions", payload, timeout)
    choices = response.get("choices")
    try:
        content = choices[0]["message"]["content"]
        parsed = json.loads(content)
        reviews = parsed["reviews"]
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("model returned an invalid audit response") from exc
    ids = {item_id for item_id, _ in batch}
    if not isinstance(reviews, list) or {review.get("id") for review in reviews if isinstance(review, dict)} != ids:
        raise ValueError("model audit response did not return every requested id exactly once")
    normalized = []
    for review in reviews:
        if not isinstance(review, dict) or not isinstance(review.get("needsRevision"), bool):
            raise ValueError("model audit response has an invalid review")
        feedback = review.get("feedback")
        if review["needsRevision"] and (not isinstance(feedback, str) or not feedback.strip()):
            raise ValueError("flagged audit review lacks feedback")
        normalized.append({"id": review["id"], "needsRevision": review["needsRevision"], "feedback": feedback.strip() if isinstance(feedback, str) else None})
    return normalized


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit cleaned music metadata through the current local model.")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8080/v1")
    parser.add_argument("--model")
    parser.add_argument("--batch-size", type=int, default=5)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    if args.batch_size < 1 or args.timeout < 1:
        parser.error("batch size and timeout must be positive")
    try:
        manifest = load_manifest(args.manifest.expanduser().resolve())
        model = args.model or discover_model(args.endpoint, min(30, args.timeout))
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"Cannot start audit: {exc}", file=sys.stderr)
        return 1
    output = args.out.expanduser().resolve() if args.out else args.manifest.with_name(args.manifest.stem + "-audit.json")
    existing: dict[int, dict[str, object]] = {}
    if output.exists():
        try:
            prior = json.loads(output.read_text(encoding="utf-8"))
            existing = {entry["id"]: entry for entry in prior.get("reviews", []) if isinstance(entry, dict) and isinstance(entry.get("id"), int)}
        except (OSError, json.JSONDecodeError, AttributeError):
            print(f"Cannot resume invalid audit output: {output}", file=sys.stderr)
            return 1
    pending = [(index, item) for index, item in enumerate(manifest) if index not in existing]
    print(f"Local model: {model}; pending audit tracks: {len(pending)}")
    for offset in range(0, len(pending), args.batch_size):
        batch = pending[offset : offset + args.batch_size]
        try:
            for review in request(args.endpoint, model, batch, args.timeout):
                existing[review["id"]] = review
        except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
            print(f"Audit batch starting at {batch[0][0] + 1} failed: {exc}", file=sys.stderr)
            return 1
        write_json_atomic(output, {"manifest": str(args.manifest.resolve()), "model": model, "reviews": [existing[index] for index in sorted(existing)]})
        flagged = sum(1 for review in existing.values() if review.get("needsRevision"))
        print(f"Audited {len(existing)}/{len(manifest)}; flagged {flagged}", flush=True)
    return 0


from audit_music_manifest_parallel import main as main, request as request


if __name__ == "__main__":
    raise SystemExit(main())
