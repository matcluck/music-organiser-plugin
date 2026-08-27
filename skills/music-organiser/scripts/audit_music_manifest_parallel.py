#!/usr/bin/env python3
"""Parallel, compact second-pass local-model audit."""

from __future__ import annotations

import argparse
import atexit
import json
import re
import sys
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path

from populate_music_manifest import (
    EndpointLease,
    EndpointLeaseError,
    LlamaConnectionError,
    LlamaHTTPError,
    ServerConfig,
    concise_source,
    configure_console_output,
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
                    "feedback": {"type": "string"},
                },
                "required": ["id", "feedback"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["reviews"],
    "additionalProperties": False,
}

SYSTEM = """Act as a conservative second-pass verifier of cleaned music metadata.
The default verdict is sound. Return a review only when the supplied evidence proves a
specific current output value is wrong. A review must name the field, quote its current
value, give the supported replacement/removal, and name the supporting source tag or
path evidence. If any of those are unavailable, omit the review.

Never flag missing optional album, albumArtist, date, genre, trackNumber, or discNumber.
Never infer compilation, albumArtist, dates, positions, or release membership. A missing
albumArtist is normal; it is not the track artist by default. Do not request a discNumber
or full date. Do not critique or rename the source filename. Preserve meaningful Remix,
VIP, Bootleg, Edit, Radio Edit, featured-artist, and similar version text. Do not request
casing changes when the proposed spelling is identical or is not explicitly supported.

Valid review classes are only: provable remaining junk inside an output field; a value in
the wrong semantic field; output contradicting an explicit source tag/path consensus;
meaningful version text lost from that consensus; or an output fact absent from all
supplied evidence. Return reviews:[] when tracks are sound. Omit every sound track from
reviews. Return JSON only."""

OPTIONAL_FIELD_TERMS = {
    "album": ("album",),
    "albumArtist": ("albumartist", "album artist"),
    "date": ("date", "year"),
    "genre": ("genre",),
    "trackNumber": ("tracknumber", "track number"),
    "discNumber": ("discnumber", "disc number"),
    "compilation": ("compilation",),
}


def actionable_review(feedback: str, item: dict[str, object]) -> bool:
    """Reject model suggestions that violate deterministic audit policy."""
    text = " ".join(feedback.casefold().split())
    output = item.get("outputMetadata")
    if not isinstance(output, dict):
        return False
    deterministic_optional = text.startswith("remove unsupported albumartist") or text.startswith(
        "replace albumartist"
    )
    if deterministic_optional and not isinstance(output.get("albumArtist"), str):
        return False
    if text.startswith("replace albumartist"):
        replacement = re.search(
            r"source-tag value\s+['\"]([^'\"]+)['\"]",
            feedback,
            flags=re.I,
        )
        current_album_artist = output.get("albumArtist")
        if (
            replacement
            and isinstance(current_album_artist, str)
            and re.sub(r"[^\w]+", "", replacement.group(1).casefold())
            == re.sub(r"[^\w]+", "", current_album_artist.casefold())
        ):
            return False
    if output.get("needsReview") is True and not deterministic_optional:
        return False
    if "source file" in text or "filename" in text or "file name" in text:
        return False
    if "casing" in text or "capitalization" in text or "capitalisation" in text:
        return False
    if (
        "correct and should remain" in text
        or "already correct" in text
        or "is sound" in text
        or "no review needed" in text
        or "no change needed" in text
        or "no changes needed" in text
        or "output metadata is correct" in text
        or "outputmetadata is correct" in text
    ):
        return False
    if text.startswith("keep "):
        return False
    identical_change = re.search(
        r"should be\s+['\"]([^'\"]+)['\"].*?not (?:just )?['\"]([^'\"]+)['\"]",
        feedback,
        flags=re.I,
    )
    if identical_change and (
        " ".join(identical_change.group(1).casefold().split())
        == " ".join(identical_change.group(2).casefold().split())
    ):
        return False
    instead_change = re.search(
        r"should be\s+['\"]([^'\"]+)['\"]\s+instead of\s+['\"]([^'\"]+)['\"]",
        feedback,
        flags=re.I,
    )
    if instead_change and (
        re.sub(r"[^\w]+", "", instead_change.group(1).casefold())
        == re.sub(r"[^\w]+", "", instead_change.group(2).casefold())
    ):
        return False
    quoted_values = re.findall(r"['\"]([^'\"]+)['\"]", feedback)
    if len(quoted_values) >= 2 and (
        " ".join(quoted_values[-1].casefold().split())
        == " ".join(quoted_values[-2].casefold().split())
    ):
        return False
    current_values = {
        field: value
        for field in ("title", "artist")
        if isinstance((value := output.get(field)), str) and value.strip()
    }

    def semantic_key(value: str) -> str:
        return re.sub(r"[^\w]+", "", value.casefold(), flags=re.UNICODE)

    for field, current_value in current_values.items():
        proposal_patterns = (
            rf"{field}(?: field)?\s+should be(?: changed to)?\s+['\"]([^'\"]+)['\"]",
            rf"change (?:the )?{field}(?: field)?\s+to\s+['\"]([^'\"]+)['\"]",
        )
        for pattern in proposal_patterns:
            proposed = re.search(pattern, feedback, flags=re.I)
            if proposed and semantic_key(proposed.group(1)) == semantic_key(current_value):
                return False
        if any(
            semantic_key(quoted) == semantic_key(current_value)
            for quoted in quoted_values
        ) and any(
            phrase in text
            for phrase in (
                "is missing",
                "should be preserved",
                "should include",
                "corrected to include",
            )
        ):
            return False
        if any(
            len(semantic_key(quoted)) >= 4
            and semantic_key(quoted) in semantic_key(current_value)
            for quoted in quoted_values
        ) and any(phrase in text for phrase in ("include", "preserve", "missing", "lost")):
            return False

    current_title = current_values.get("title", "")
    if (
        ("remix information is lost" in text and "remix" in current_title.casefold())
        or ("bootleg information is missing" in text and "bootleg" in current_title.casefold())
    ):
        return False

    proposed_unknown_artist = re.search(
        r"(?:change|set) (?:the )?artist(?: field)? to\s+['\"]?(unknown|null|unknown artist)['\"]?",
        feedback,
        flags=re.I,
    )
    if proposed_unknown_artist and current_values.get("artist"):
        return False

    if "original metadata" in text and any(
        keyword in text for keyword in ("remix", "mix", "edit", "vip", "bootleg")
    ):
        evidence_blob = json.dumps(
            {
                "source": item.get("source"),
                "originalMetadata": item.get("originalMetadata"),
            },
            ensure_ascii=False,
        )
        claimed_context = [
            value
            for value in quoted_values
            if re.search(r"remix|mix|edit|vip|bootleg", value, flags=re.I)
        ]
        if claimed_context and not any(
            semantic_key(value) in semantic_key(evidence_blob)
            for value in claimed_context
        ):
            return False
    claimed_missing_artist = re.search(
        r"(?:include|missing) (?:the )?(?:featured )?artist\s+['\"]([^'\"]+)['\"]",
        feedback,
        flags=re.I,
    )
    current_artist = output.get("artist")
    if (
        claimed_missing_artist
        and isinstance(current_artist, str)
        and claimed_missing_artist.group(1).casefold() in current_artist.casefold()
    ):
        return False
    for terms in OPTIONAL_FIELD_TERMS.values():
        if any(term in text for term in terms):
            # Optional release-field verification is handled deterministically below.
            # The model is deliberately not allowed to infer, fill, or rewrite it.
            return deterministic_optional
    if not any(
        evidence_term in text
        for evidence_term in ("source", "original metadata", "originalmetadata", " tag", "path")
    ):
        return False
    return True


def deterministic_optional_review(
    item_id: int, item: dict[str, object]
) -> dict[str, object] | None:
    output = item.get("outputMetadata")
    if not isinstance(output, dict):
        return None
    output_album_artist = output.get("albumArtist")
    if not isinstance(output_album_artist, str) or not output_album_artist.strip():
        return None
    if output.get("compilation") is True:
        return None
    evidence = metadata_evidence(item.get("originalMetadata", {}), 300, 1600)
    source_album_artist = (
        evidence.get("albumArtist") if isinstance(evidence, dict) else None
    )
    if not isinstance(source_album_artist, str) or not source_album_artist.strip():
        return {
            "id": item_id,
            "needsRevision": True,
            "feedback": (
                f"Remove unsupported albumArtist {output_album_artist!r}; "
                "the source has no album-artist tag and is not a compilation."
            ),
        }
    if source_album_artist.casefold() != output_album_artist.strip().casefold():
        return {
            "id": item_id,
            "needsRevision": True,
            "feedback": (
                f"Replace albumArtist {output_album_artist!r} with "
                f"source-tag value {source_album_artist!r}."
            ),
        }
    return None


def load_manifest(path: Path) -> list[dict[str, object]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("manifest must be a JSON array")
    return data


def request(
    endpoint: str,
    model: str,
    batch: list[tuple[int, dict[str, object]]],
    timeout: int,
) -> list[dict[str, object]]:
    items = [
        {
            "id": item_id,
            "source": concise_source(item.get("source")),
            "originalMetadata": metadata_evidence(
                item.get("originalMetadata", {}), 300, 1600
            ),
            "outputMetadata": item.get("outputMetadata"),
        }
        for item_id, item in batch
    ]
    schema = json.loads(json.dumps(SCHEMA))
    schema["properties"]["reviews"]["maxItems"] = len(items)
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {
                "role": "user",
                "content": json.dumps(
                    {"tracks": items}, ensure_ascii=False, separators=(",", ":")
                ),
            },
        ],
        "temperature": 0,
        "max_tokens": min(768, 48 + 80 * len(items)),
        "cache_prompt": True,
        "response_format": {"type": "json_object", "schema": schema},
        "stream": False,
    }
    response = http_json(endpoint.rstrip("/") + "/chat/completions", payload, timeout)
    try:
        reviews = json.loads(response["choices"][0]["message"]["content"])["reviews"]
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("model returned an invalid audit response") from exc
    ids = {item_id for item_id, _ in batch}
    if not isinstance(reviews, list):
        raise ValueError("model audit response did not return a reviews array")
    normalized: list[dict[str, object]] = []
    seen: set[int] = set()
    items_by_id = {item_id: item for item_id, item in batch}
    for review in reviews:
        if not isinstance(review, dict):
            raise ValueError("model audit response has an invalid review")
        item_id = review.get("id")
        feedback = review.get("feedback")
        if (
            not isinstance(item_id, int)
            or item_id not in ids
            or item_id in seen
            or not isinstance(feedback, str)
            or not feedback.strip()
        ):
            raise ValueError("model audit response has an invalid or duplicate review")
        seen.add(item_id)
        clean_feedback = feedback.strip()
        if actionable_review(clean_feedback, items_by_id[item_id]):
            normalized.append(
                {"id": item_id, "needsRevision": True, "feedback": clean_feedback}
            )
    normalized_ids = {review["id"] for review in normalized}
    for item_id, item in batch:
        if item_id in normalized_ids:
            continue
        deterministic = deterministic_optional_review(item_id, item)
        if deterministic is not None:
            normalized.append(deterministic)
    return normalized


def error_kind(exc: Exception) -> str:
    if isinstance(exc, LlamaConnectionError) or (
        isinstance(exc, LlamaHTTPError)
        and (exc.status == 503 or "loading model" in exc.details.casefold())
    ):
        return "unavailable"
    if isinstance(exc, LlamaHTTPError) and (
        exc.status == 400
        and "exceed" in exc.details.casefold()
        and "context" in exc.details.casefold()
    ):
        return "context"
    return "other"


def main() -> int:
    configure_console_output()
    parser = argparse.ArgumentParser(
        description="Audit cleaned metadata through local llama.cpp servers."
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--endpoint", action="append", dest="endpoints")
    parser.add_argument("--model")
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--allow-shared-endpoints", action="store_true")
    args = parser.parse_args()
    if (
        args.batch_size < 1
        or args.timeout < 1
        or args.retries < 0
        or (args.limit is not None and args.limit < 1)
    ):
        parser.error("batch size and timeout must be positive; retries cannot be negative")

    endpoints = list(
        dict.fromkeys(
            endpoint.rstrip("/")
            for endpoint in (args.endpoints or ["http://127.0.0.1:8080/v1"])
        )
    )
    if not args.allow_shared_endpoints:
        try:
            endpoint_lease = EndpointLease(endpoints).acquire()
        except (OSError, EndpointLeaseError) as exc:
            print(f"Cannot reserve llama.cpp endpoints: {exc}", file=sys.stderr)
            return 1
        atexit.register(endpoint_lease.release)

    try:
        manifest_path = args.manifest.expanduser().resolve()
        manifest = load_manifest(manifest_path)
        servers = [
            ServerConfig(
                endpoint=endpoint,
                model=args.model or discover_model(endpoint, min(30, args.timeout)),
            )
            for endpoint in endpoints
        ]
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as exc:
        print(f"Cannot start audit: {exc}", file=sys.stderr)
        return 1

    output = (
        args.out.expanduser().resolve()
        if args.out
        else manifest_path.with_name(manifest_path.stem + "-audit.json")
    )
    existing: dict[int, dict[str, object]] = {}
    if output.exists():
        try:
            prior = json.loads(output.read_text(encoding="utf-8"))
            existing = {
                entry["id"]: entry
                for entry in prior.get("reviews", [])
                if isinstance(entry, dict) and isinstance(entry.get("id"), int)
            }
        except (OSError, json.JSONDecodeError, AttributeError):
            print(f"Cannot resume invalid audit output: {output}", file=sys.stderr)
            return 1

    pending = [(index, item) for index, item in enumerate(manifest) if index not in existing]
    if args.limit is not None:
        pending = pending[: args.limit]
    target_total = len(existing) + len(pending)
    work = deque(
        (number, pending[offset : offset + args.batch_size], 0)
        for number, offset in enumerate(
            range(0, len(pending), args.batch_size), start=1
        )
    )
    next_number = len(work) + 1
    active: dict[
        Future[list[dict[str, object]]],
        tuple[ServerConfig, tuple[int, list[tuple[int, dict[str, object]]], int]],
    ] = {}
    cooldown = {server.endpoint: 0.0 for server in servers}
    availability_failures = {server.endpoint: 0 for server in servers}
    disabled: set[str] = set()
    failed = 0
    print(f"Local servers: {len(servers)}; pending audit tracks: {len(pending)}")

    with ThreadPoolExecutor(max_workers=len(servers)) as executor:
        while work or active:
            busy = {server.endpoint for server, _task in active.values()}
            now = time.monotonic()
            for server in servers:
                if not work:
                    break
                if (
                    server.endpoint in busy
                    or server.endpoint in disabled
                    or cooldown[server.endpoint] > now
                ):
                    continue
                task = work.popleft()
                number, batch, _attempts = task
                future = executor.submit(
                    request, server.endpoint, server.model, batch, args.timeout
                )
                active[future] = (server, task)
                print(
                    f"Audit batch {number} started on {server.endpoint}: "
                    f"{len(batch)} track(s)",
                    flush=True,
                )

            if not active:
                usable = [server for server in servers if server.endpoint not in disabled]
                if not usable:
                    failed += len(work)
                    print("All audit endpoints became unavailable.", file=sys.stderr)
                    break
                delay = min(
                    max(0.05, cooldown[server.endpoint] - time.monotonic())
                    for server in usable
                )
                time.sleep(min(delay, 1.0))
                continue

            done, _ = wait(active, timeout=1.0, return_when=FIRST_COMPLETED)
            for future in done:
                server, task = active.pop(future)
                number, batch, attempts = task
                try:
                    flagged = future.result()
                except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
                    kind = error_kind(exc)
                    if kind == "context" and len(batch) > 1:
                        midpoint = len(batch) // 2
                        work.appendleft((next_number + 1, batch[midpoint:], 0))
                        work.appendleft((next_number, batch[:midpoint], 0))
                        next_number += 2
                    elif kind == "context":
                        failed += 1
                        print(
                            f"Audit track {batch[0][0] + 1} exceeds context even alone.",
                            file=sys.stderr,
                        )
                    elif kind == "unavailable":
                        availability_failures[server.endpoint] += 1
                        count = availability_failures[server.endpoint]
                        work.appendleft(task)
                        if count >= 5:
                            disabled.add(server.endpoint)
                        else:
                            cooldown[server.endpoint] = (
                                time.monotonic() + min(2 ** count, 30)
                            )
                    elif attempts < args.retries:
                        work.append((number, batch, attempts + 1))
                    elif len(batch) > 1:
                        midpoint = len(batch) // 2
                        work.appendleft((next_number + 1, batch[midpoint:], 0))
                        work.appendleft((next_number, batch[:midpoint], 0))
                        next_number += 2
                        print(
                            f"Audit batch {number} stayed invalid; split into "
                            f"{midpoint} and {len(batch) - midpoint} track work units.",
                            file=sys.stderr,
                        )
                    else:
                        failed += 1
                        print(f"Audit batch {number} failed: {exc}", file=sys.stderr)
                    continue

                availability_failures[server.endpoint] = 0
                flagged_by_id = {entry["id"]: entry for entry in flagged}
                for item_id, _item in batch:
                    existing[item_id] = flagged_by_id.get(
                        item_id,
                        {"id": item_id, "needsRevision": False, "feedback": None},
                    )
                write_json_atomic(
                    output,
                    {
                        "manifest": str(manifest_path),
                        "model": servers[0].model,
                        "models": {
                            config.endpoint: config.model for config in servers
                        },
                        "reviews": [existing[index] for index in sorted(existing)],
                    },
                )
                count_flagged = sum(
                    1 for review in existing.values() if review.get("needsRevision")
                )
                print(
                    f"Audited {len(existing)}/{target_total}; "
                    f"flagged {count_flagged}",
                    flush=True,
                )

    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
