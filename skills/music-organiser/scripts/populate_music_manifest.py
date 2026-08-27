#!/usr/bin/env python3
"""Populate a music manifest's clean outputMetadata through llama.cpp."""

from __future__ import annotations

import argparse
import atexit
import copy
import difflib
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from mutagen.id3 import TCON

from build_music_manifest import OUTPUT_METADATA_TEMPLATE, write_json_atomic


def configure_console_output() -> None:
    """Keep a non-UTF-8 Windows console from aborting a resumable cleanup."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")


OUTPUT_FIELDS = tuple(OUTPUT_METADATA_TEMPLATE)

NULLABLE_STRING = {"type": ["string", "null"]}
NULLABLE_POSITIVE_INTEGER = {
    "type": ["integer", "null"],
    "minimum": 1,
}

LEGACY_OUTPUT_METADATA_SCHEMA = {
    "type": "object",
    "properties": {
        "title": NULLABLE_STRING,
        "artist": NULLABLE_STRING,
        "albumArtist": NULLABLE_STRING,
        "album": NULLABLE_STRING,
        "date": NULLABLE_STRING,
        "trackNumber": NULLABLE_POSITIVE_INTEGER,
        "discNumber": NULLABLE_POSITIVE_INTEGER,
        "genre": NULLABLE_STRING,
        "compilation": {"type": "boolean"},
        "needsReview": {"type": "boolean"},
        "reviewReason": NULLABLE_STRING,
    },
    "required": list(OUTPUT_FIELDS),
    "additionalProperties": False,
}
# Kept for deterministic validators and compatibility imports. The transport
# schema below is intentionally smaller, but final manifests still use all fields.
OUTPUT_METADATA_SCHEMA = LEGACY_OUTPUT_METADATA_SCHEMA

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "title": NULLABLE_STRING,
                    "artist": NULLABLE_STRING,
                    "albumArtist": {"type": "string"},
                    "album": {"type": "string"},
                    "date": {"type": "string"},
                    "trackNumber": {"type": "integer", "minimum": 1},
                    "discNumber": {"type": "integer", "minimum": 1},
                    "genre": {"type": "string"},
                    "compilation": {"type": "boolean"},
                    "reviewReason": {"type": "string"},
                },
                "required": ["id", "title", "artist"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """Clean music metadata using only the supplied source path, tags,
libraryContext, and auditFeedback. Return one JSON item per id.

Output contract:
- Always return id, title, and artist. Use JSON null when title or artist is unsupported.
- Omit every unsupported optional field. Optional fields are albumArtist, album, date,
  trackNumber, discNumber, genre, compilation, and reviewReason.
- Omit compilation unless true. Omit reviewReason for accepted tracks. Add one concise
  reviewReason only when core identity is genuinely uncertain; missing optional release
  facts never require review.

Cleaning rules:
- Do not invent facts. Separate mixed fields and discard unsupported fragments.
- title contains only track name and meaningful version text (Remix, VIP, Bootleg, Edit,
  Dub, Rework, Instrumental, Live, Demo, Radio Edit, Extended Mix, featured artists).
- artist and albumArtist contain only credited artists. album contains only release title.
- Remove extensions, leading file indexes, bitrates/codecs, URLs/domains, uploader and
  download labels, FREE DL/PROMO text, MASTER/PREMASTER/FINAL markers, catalogue suffixes,
  DJ keys, BPM/energy/cue/grid data, and application counters.
- Normalize ordinary display casing while preserving established stylization and
  abbreviations. Use natural non-English casing.
- Preserve a clear Artist - Title filename split. Tags may be misplaced; move supported
  values into the correct semantic field.
- Numeric-looking names can be real artists (for example 1234, 4Example, or 22TESTROSES).
  Never treat a value as a file index merely because it begins with digits. Preserve an
  artist when the artist tag, folder, or clear Artist - Title split supports it. Remove
  numeric text only when it is an unambiguous filename prefix before the full identity.
- When a clean artist/title tag agrees with the folder or Artist - Title filename split,
  preserve that identity; do not silently drop a credited artist or duplicate the title
  into the artist field.
- album is null/omitted when release membership is uncertain. If album is omitted, also
  omit albumArtist, trackNumber, and discNumber. Compilations use albumArtist
  "Various Artists" and compilation true.
- date is YYYY, YYYY-MM, or YYYY-MM-DD at supported precision; January 1 is normally a
  year-only placeholder. Track/disc numbers are positive integers.
- genre must come from supplied genre evidence; @handles, #labels, Other, and Unknown are
  unsupported.
- libraryContext is advisory identity evidence only. Never copy its release fields across
  versions. auditFeedback must be corrected only when source evidence supports it.
- Never emit the strings null, none, unknown, n/a, or other as metadata.
- Return JSON only and no fields outside the schema.
"""


@dataclass(frozen=True)
class ServerConfig:
    endpoint: str
    model: str


@dataclass(frozen=True)
class BatchOutcome:
    batch_number: int
    item_ids: list[int]
    server: ServerConfig
    result: dict[int, dict[str, object]] | None
    metrics: dict[str, object]
    elapsed_seconds: float
    retry_errors: list[str]
    error: str | None
    error_kind: str | None = None


class LlamaHTTPError(RuntimeError):
    def __init__(self, status: int, details: str):
        super().__init__(f"llama.cpp returned HTTP {status}: {details}")
        self.status = status
        self.details = details


class LlamaConnectionError(RuntimeError):
    pass


class EndpointLeaseError(RuntimeError):
    pass


class EndpointLease:
    """Hold one non-blocking process lock for each llama.cpp endpoint."""

    def __init__(self, endpoints: list[str], lock_dir: Path | None = None):
        project_root = Path(__file__).resolve().parents[3]
        self.lock_dir = lock_dir or project_root / "artifacts" / "locks"
        self.endpoints = sorted(set(endpoints))
        self.files: list[object] = []

    def acquire(self) -> "EndpointLease":
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        try:
            for endpoint in self.endpoints:
                digest = hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:16]
                path = self.lock_dir / f"music-llm-{digest}.lock"
                handle = path.open("a+b")
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:  # pragma: no cover - Windows is the supported runtime.
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except (OSError, BlockingIOError) as exc:
                    handle.close()
                    raise EndpointLeaseError(
                        f"Endpoint {endpoint} is already assigned to another music LLM job. "
                        "Wait for it to finish instead of sharing single-slot servers."
                    ) from exc
                self.files.append(handle)
        except Exception:
            self.release()
            raise
        return self

    def release(self) -> None:
        while self.files:
            handle = self.files.pop()
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:  # pragma: no cover - Windows is the supported runtime.
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def load_manifest(path: Path) -> list[dict[str, object]]:
    with path.open("r", encoding="utf-8-sig") as file:
        value = json.load(file)
    if not isinstance(value, list):
        raise ValueError("Manifest root must be a JSON array.")
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"Manifest item {index} is not an object.")
        for key in ("source", "originalMetadata", "outputMetadata"):
            if key not in item:
                raise ValueError(f"Manifest item {index} is missing {key}.")
        output = item.get("outputMetadata")
        if isinstance(output, dict):
            legacy_review_state = "needsReview" not in output
            for field, default in OUTPUT_METADATA_TEMPLATE.items():
                output.setdefault(field, default)
            if legacy_review_state:
                reason = clean_optional_string(output.get("reviewReason"))
                core_missing = not output.get("title") or not output.get("artist")
                output["needsReview"] = bool(reason and core_missing)
                if not output["needsReview"]:
                    output["reviewReason"] = None
    return value


def default_output_path(manifest_path: Path) -> Path:
    suffix = manifest_path.suffix or ".json"
    return manifest_path.with_name(manifest_path.stem + "-llm" + suffix)


def manifests_match(
    original: list[dict[str, object]], existing: list[dict[str, object]]
) -> bool:
    return len(original) == len(existing) and all(
        left.get("source") == right.get("source")
        for left, right in zip(original, existing)
    )


def safe_prompt_key(value: object, max_length: int = 160) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value))
    text = " ".join(text.split()) or "unnamed"
    if len(text) <= max_length:
        return text
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:10]
    return f"{text[: max_length - 13]}...#{digest}"


def compact_evidence(value: object, max_value_chars: int, depth: int = 0) -> Any:
    if depth > 12:
        return "[nested value omitted]"
    if isinstance(value, dict):
        if value.get("exists") is True and "byteLength" in value:
            return f"[binary metadata omitted: {value.get('byteLength', 'unknown')} bytes]"
        if "binaryBase64" in value:
            size = value.get("byteLength", "unknown")
            return f"[binary metadata omitted: {size} bytes]"
        result: dict[str, object] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 100:
                result["omittedFields"] = len(value) - index
                break
            result[safe_prompt_key(key)] = compact_evidence(
                item, max_value_chars, depth + 1
            )
        return result
    if isinstance(value, list):
        result = [
            compact_evidence(item, max_value_chars, depth + 1)
            for item in value[:50]
        ]
        if len(value) > 50:
            result.append(f"[{len(value) - 50} additional values omitted]")
        return result
    if isinstance(value, str) and len(value) > max_value_chars:
        return value[:max_value_chars] + f"...[truncated {len(value) - max_value_chars} chars]"
    return value


def contains_binary(value: object, depth: int = 0) -> bool:
    if depth > 12:
        return False
    if isinstance(value, dict):
        if value.get("exists") is True and "byteLength" in value:
            return True
        if "binaryBase64" in value:
            return True
        return any(contains_binary(item, depth + 1) for item in value.values())
    if isinstance(value, list):
        return any(contains_binary(item, depth + 1) for item in value[:100])
    return False


PROMPT_TAG_FIELDS = {
    "title": ("title", "tit2", "©nam"),
    "artist": ("artist", "artists", "tpe1", "©art"),
    "albumArtist": ("albumartist", "album artist", "tpe2", "aart"),
    "album": ("album", "talb", "©alb"),
    "date": (
        "date", "year", "tdrc", "tyer", "tdor", "tdrl", "©day",
        "txxx:recording_date", "txxx:release_time", "txxx:year",
    ),
    "trackNumber": ("track", "tracknumber", "track number", "trck", "trkn"),
    "discNumber": ("disc", "discnumber", "disc number", "tpos", "disk"),
    "genre": ("genre", "tcon", "©gen"),
    "compilation": ("compilation", "tcmp", "cpil"),
}


def concise_tag_value(value: object, max_value_chars: int) -> object:
    """Keep semantic values while dropping mutagen serialization scaffolding."""
    if isinstance(value, dict):
        if contains_binary(value):
            return None
        for key in ("text", "value", "values", "data"):
            if key in value:
                return concise_tag_value(value[key], max_value_chars)
        date_parts = [value.get(part) for part in ("year", "month", "day")]
        if isinstance(date_parts[0], int):
            result = f"{date_parts[0]:04d}"
            if isinstance(date_parts[1], int):
                result += f"-{date_parts[1]:02d}"
                if isinstance(date_parts[2], int):
                    result += f"-{date_parts[2]:02d}"
            return result
        return None
    if isinstance(value, list):
        values = [concise_tag_value(item, max_value_chars) for item in value[:20]]
        values = [item for item in values if item not in (None, "", [])]
        if len(values) == 1:
            return values[0]
        return values or None
    if isinstance(value, str):
        cleaned = " ".join(re.sub(r"[\x00-\x1f\x7f]+", " ", value).split())
        if not cleaned:
            return None
        return cleaned[:max_value_chars]
    if isinstance(value, (int, float, bool)):
        return value
    return None


def metadata_evidence(
    original: object,
    max_value_chars: int,
    max_evidence_chars: int,
) -> object:
    if not isinstance(original, dict):
        return compact_evidence(original, max_value_chars)

    evidence: dict[str, object] = {}
    raw_tags = original.get("tags")
    if not isinstance(raw_tags, dict):
        return evidence

    normalized_tags = {
        safe_prompt_key(key).casefold(): value for key, value in raw_tags.items()
    }
    for field, aliases in PROMPT_TAG_FIELDS.items():
        values: list[object] = []
        for alias in aliases:
            if alias not in normalized_tags:
                continue
            value = concise_tag_value(normalized_tags[alias], max_value_chars)
            if value not in (None, "", []) and value not in values:
                values.append(value)
        if values:
            evidence[field] = values[0] if len(values) == 1 else values

    # The limit is a last-resort guard. Canonical evidence normally stays far below it.
    while evidence and len(json.dumps(evidence, ensure_ascii=False)) > max_evidence_chars:
        evidence.pop(next(reversed(evidence)))
    return evidence


def concise_source(value: object) -> str:
    text = str(value or "")
    path = PureWindowsPath(text)
    parts = path.parts[-3:]
    return str(PureWindowsPath(*parts)) if parts else text


def model_item(
    item_id: int,
    item: dict[str, object],
    max_value_chars: int,
    max_evidence_chars: int,
    library_context: list[dict[str, object]] | None = None,
    audit_feedback: str | None = None,
) -> dict[str, object]:
    result = {
        "id": item_id,
        "source": concise_source(item.get("source")),
        "originalMetadata": metadata_evidence(
            item.get("originalMetadata", {}),
            max_value_chars,
            max_evidence_chars,
        ),
    }
    if library_context:
        result["libraryContext"] = library_context
    if audit_feedback:
        result["auditFeedback"] = audit_feedback
    return result


def build_user_prompt(items: list[dict[str, object]]) -> str:
    return (
        "Populate outputMetadata for these tracks. Tracks are in source-path order, "
        "so adjacent entries may belong to the same release. libraryContext entries are "
        "accepted local title matches supplied only as supporting identity clues. "
        "auditFeedback is a prior reviewer's specific concern: correct the field only "
        "when it is supported by the original evidence; otherwise retain or mark review.\n\n"
        + json.dumps({"tracks": items}, ensure_ascii=False, separators=(",", ":"))
    )


def http_json(
    url: str,
    payload: dict[str, object] | None,
    timeout: int,
) -> dict[str, object]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            decoded = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise LlamaHTTPError(exc.code, details) from exc
    except urllib.error.URLError as exc:
        raise LlamaConnectionError(
            f"Could not reach llama.cpp at {url}: {exc.reason}"
        ) from exc
    if not isinstance(decoded, dict):
        raise RuntimeError(f"Unexpected JSON response from {url}")
    return decoded


def discover_model(endpoint: str, timeout: int) -> str:
    response = http_json(endpoint.rstrip("/") + "/models", None, timeout)
    models = response.get("data", [])
    if not isinstance(models, list) or not models or not isinstance(models[0], dict):
        raise RuntimeError("llama.cpp /models did not return a loaded model.")
    model = models[0].get("id")
    if not isinstance(model, str) or not model:
        raise RuntimeError("llama.cpp returned a model without an id.")
    return model


def call_llama_cpp(
    endpoint: str,
    model: str,
    items: list[dict[str, object]],
    timeout: int,
    max_tokens: int,
    trace_path: Path | None = None,
) -> tuple[str, dict[str, object]]:
    response_schema = copy.deepcopy(RESPONSE_SCHEMA)
    items_schema = response_schema["properties"]["items"]
    items_schema["minItems"] = len(items)
    items_schema["maxItems"] = len(items)
    effective_max_tokens = min(max_tokens, 96 + (128 * len(items)))
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(items)},
        ],
        "temperature": 0,
        "max_tokens": effective_max_tokens,
        "cache_prompt": True,
        "stream": False,
        "response_format": {
            "type": "json_object",
            "schema": response_schema,
        },
    }
    response = http_json(
        endpoint.rstrip("/") + "/chat/completions", payload, timeout
    )
    if trace_path:
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(
            trace_path,
            {"endpoint": endpoint, "request": payload, "response": response},
        )
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise RuntimeError("llama.cpp response did not contain a completion choice.")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise RuntimeError("llama.cpp completion did not contain a message.")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("llama.cpp completion content was empty.")
    usage = response.get("usage")
    timings = response.get("timings")
    metrics: dict[str, object] = {}
    if isinstance(usage, dict):
        metrics.update(
            {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
                "total_tokens": usage.get("total_tokens"),
            }
        )
    if isinstance(timings, dict):
        metrics.update(
            {
                "prompt_tokens_per_second": timings.get("prompt_per_second"),
                "completion_tokens_per_second": timings.get("predicted_per_second"),
            }
        )
    return content, metrics


def extract_json_object(text: str) -> dict[str, object]:
    cleaned = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("Model response did not contain a JSON object.")
        value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("Model response root was not an object.")
    return value


NULL_TEXT_VALUES = {
    "null",
    "none",
    "unknown",
    "unknown album",
    "unknown artist",
    "unknown genre",
    "n/a",
    "na",
    "tba",
    "tbd",
}

GENERIC_GENRES = {"other", "misc", "miscellaneous", "unknown", "unknown genre", "n/a"}

GENERIC_ALBUM_VALUES = {
    "album title goes here",
    "random album title",
    "random album title promo cd",
    "unknown album",
    "untitled album",
}

TITLE_CASE_MINOR_WORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "but",
    "by",
    "da",
    "de",
    "del",
    "der",
    "di",
    "du",
    "for",
    "feat",
    "featuring",
    "from",
    "ft",
    "in",
    "la",
    "le",
    "n",
    "nor",
    "of",
    "on",
    "or",
    "the",
    "to",
    "van",
    "von",
    "vs",
    "with",
    "x",
}

CASING_WORD_PATTERN = re.compile(r"[A-Za-z]+(?:['’][A-Za-z]+)?")

DOMAIN_PATTERN = re.compile(
    r"(?:https?://|www\.)[^\s\])}]+|"
    r"\b[\w.-]+\.(?:com|net|org|co|io|ru|cc|tv|me|fm|biz|info|xyz|uk|au)"
    r"(?:/[^\s\])}]*)?",
    flags=re.I,
)

SOCIAL_HANDLE_PATTERN = re.compile(r"(?<![\w@])@[A-Za-z0-9_.-]+")

BPM_ANALYSIS_PATTERN = re.compile(
    r"\b\d{2,3}(?:\s*-\s*\d{2,3})?\s*bpm\b",
    flags=re.I,
)

TECHNICAL_SUFFIX_PATTERN = re.compile(
    r"(?:[-_ ]+(?:mp3|flac|wav|aac))(?:[-_ ]+\d{2,4})?\s*$",
    flags=re.I,
)

PRODUCER_CREDIT_PATTERN = re.compile(
    r"\s*[\[(]\s*(?:co-)?prod(?:uced)?\.?\s*(?:by\s+)?[^\])]*(?:[\])]|$)",
    flags=re.I,
)

BARE_PRODUCER_SUFFIX_PATTERN = re.compile(
    r"\s+(?:co-)?prod(?:uced)?\.?\s+by\b.*$",
    flags=re.I,
)

LEET_YEAR_PATTERN = re.compile(
    r"\s*[\[(]\s*[12](?=[0-9oO]*[oO])[0-9oO]{3}\s*[\])]\s*"
)

DRM_TOKEN_PATTERN = re.compile(r"\s+DRM\b", flags=re.I)

BRACKETED_NOISE_PATTERN = re.compile(
    r"\s*[\[(]\s*(?:"
    r"free\s*(?:d/?l|download)|download|promo(?:tional)?(?:\s+use\s+only)?|"
    r"official\s*(?:audio|video)|(?:youtube|soundcloud)(?:\s*rip)?|"
    r"exclusive|premiere|via|"
    r"(?:hq|hd|high\s*quality)|\d{2,4}\s*kbps|(?:mp3|flac|wav|aac)"
    r")\s*[\])]\s*",
    flags=re.I,
)

TRAILING_NOISE_PATTERN = re.compile(
    r"\s*(?:[-–—|]\s*|\s+)(?:"
    r"free\s*(?:d/?l|download)|download|promo(?:tional)?|"
    r"official\s*(?:audio|video)|(?:youtube|soundcloud)(?:\s*rip)?|"
    r"\d{2,4}\s*kbps|(?:mp3|flac|wav|aac)"
    r")\s*$",
    flags=re.I,
)

DJ_KEY_VALUE_PATTERN = (
    r"(?:[1-9]|1[0-2])(?:[AB]|[mMdD])"
    r"(?:\s*[/_]\s*(?:[1-9]|1[0-2])(?:[AB]|[mMdD]))?"
)

DJ_SUFFIX_PATTERN = re.compile(
    rf"(?:\s*[-–—|]\s*(?:{DJ_KEY_VALUE_PATTERN}(?:\s*-\s*\d+){{0,3}}|"
    rf"\d{{2,3}}(?:\.\d+)?\s*bpm|energy\s*\d+)|"
    rf"\s*[\[(]\s*{DJ_KEY_VALUE_PATTERN}(?:\s*-\s*\d+){{0,3}}\s*[\])])\s*$",
    flags=re.I,
)

PRODUCTION_SUFFIX_PATTERN = re.compile(
    r"\s+(?:MASTER(?:ED)?|PREMASTER|FINAL(?:\s+MASTER)?|DEMO\s+MASTER)"
    r"(?:\s+[A-Z]{2,10}[-_]?\d{2,8})?\s*$"
)

COMPILATION_ALBUM_PATTERN = re.compile(
    r"\b(?:various\s+artists|compilation|anthology|volume|vol\.?\s*0*\d+)\b",
    flags=re.I,
)

FEATURE_CREDIT_PATTERN = re.compile(
    r"\s+\b(?:feat(?:uring)?|ft)\.?\s*",
    flags=re.I,
)

CONTEXT_VERSION_PATTERN = re.compile(
    r"\s*[\[(][^\])]*(?:remix|mix|edit|version|vip|bootleg|rework|dub|radio|"
    r"extended|vocal|feat(?:uring)?|ft\.?)[^\])]*[\])]",
    flags=re.I,
)

PLACEHOLDER_TITLE_PATTERN = re.compile(
    r"^(?:audio|song|track|unknown|untitled)(?:\s*\d+)?$",
    flags=re.I,
)

def repair_mojibake(value: str) -> str:
    """Repair text when CP1251 Cyrillic bytes were decoded as Latin-1."""
    latin1_letters = sum(
        character.isalpha() and "\u00c0" <= character <= "\u00ff"
        for character in value
    )
    ascii_letters = sum(character.isascii() and character.isalpha() for character in value)
    letter_count = latin1_letters + ascii_letters
    if latin1_letters < 5 or not letter_count or latin1_letters / letter_count < 0.5:
        return value

    try:
        candidate = value.encode("latin-1").decode("cp1251")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value

    cyrillic_letters = sum(
        character.isalpha() and "\u0400" <= character <= "\u04ff"
        for character in candidate
    )
    candidate_letters = cyrillic_letters + sum(
        character.isascii() and character.isalpha() for character in candidate
    )
    if (
        cyrillic_letters < 5
        or not candidate_letters
        or cyrillic_letters / candidate_letters < 0.5
    ):
        return value
    return candidate


def clean_optional_string(value: object) -> str | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFKC", str(value))
    text = repair_mojibake(text)
    text = "".join(character for character in text if character >= " " or character in "\t\n")
    text = " ".join(text.split()).strip()
    if not text or text.casefold() in NULL_TEXT_VALUES:
        return None
    return text


def remove_library_noise(value: str) -> str:
    text = DOMAIN_PATTERN.sub(" ", value)
    text = BPM_ANALYSIS_PATTERN.sub(" ", text)
    text = re.sub(r"\s*[\[(]\s*[|:/-]*\s*[\])]\s*", " ", text)
    previous = None
    while text != previous:
        previous = text
        text = BRACKETED_NOISE_PATTERN.sub(" ", text)
        text = TRAILING_NOISE_PATTERN.sub("", text)
        text = DJ_SUFFIX_PATTERN.sub("", text)
        text = PRODUCTION_SUFFIX_PATTERN.sub("", text)
        text = TECHNICAL_SUFFIX_PATTERN.sub("", text)
        text = re.sub(r"\s*[\[(]\s*[|:/-]*\s*[\])]\s*", " ", text)
        text = re.sub(r"([\[(])\s+", r"\1", text)
        text = re.sub(r"\s+([\])])", r"\1", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" -–—|_.,>")


def clean_artist(value: object) -> str | None:
    artist = clean_optional_string(value)
    if artist is None:
        return None
    artist = re.sub(r"^\$\d+\s+", "", artist)
    artist = re.sub(r"^\[\s*[A-Za-z_.-]*\d+[A-Za-z0-9_.-]*\s*\]\s*", "", artist)
    artist = re.sub(r"\.(?:mp3|flac|wav|m4a|aac|ogg|opus|wma)$", "", artist, flags=re.I)
    artist = remove_library_noise(artist)
    artist = PRODUCER_CREDIT_PATTERN.sub(" ", artist)
    artist = FEATURE_CREDIT_PATTERN.sub(" feat. ", artist)
    artist = re.sub(
        r"\s*-\s*\(\s*feat\.\s*(.*?)\s*\)\s*$",
        r" feat. \1",
        artist,
        flags=re.I,
    )
    artist = " ".join(artist.split())
    return artist or None


def clean_album(value: object) -> str | None:
    album = clean_optional_string(value)
    if album is None:
        return None
    had_via_marker = bool(re.search(r"[\[(]\s*via\b", album, flags=re.I))
    album = re.sub(r"\.(?:mp3|flac|wav|m4a|aac|ogg|opus|wma)$", "", album, flags=re.I)
    album = SOCIAL_HANDLE_PATTERN.sub(" ", album)
    album = remove_library_noise(album)
    album_key = album.strip("<>[]{}() ").casefold()
    if (
        album_key in GENERIC_ALBUM_VALUES
        or album_key.startswith("atualizando -")
        or had_via_marker
    ):
        return None
    return album or None


def clean_title(value: object, artist: str | None) -> str | None:
    title = clean_optional_string(value)
    if title is None:
        return None

    title = re.sub(r"\.(?:mp3|flac|wav|m4a|aac|ogg|opus|wma)$", "", title, flags=re.I)
    title = remove_library_noise(title)
    title = PRODUCER_CREDIT_PATTERN.sub(" ", title)
    title = BARE_PRODUCER_SUFFIX_PATTERN.sub("", title)
    title = LEET_YEAR_PATTERN.sub(" ", title)
    title = DRM_TOKEN_PATTERN.sub("", title)
    if title.count("_") >= 2:
        title = title.replace("_", " ")
    else:
        title = re.sub(r"_+(?=[\s(\[\-–—|])", " ", title)
        title = re.sub(r"(?<=[\s)\]\-–—|])_+", " ", title)
    title = remove_library_noise(title)
    title = re.sub(
        r"^(?:\(?0\d{1,2}\)?(?:\s*[-._]\s*|\s+)|"
        r"\(?\d{2,3}\)?\s*[-._]\s+)",
        "",
        title,
    )
    title = re.sub(
        r"\s*[-–—]\s*(\([^)]*(?:remix|mix|edit|vip|bootleg|rework|dub)[^)]*\))\s*$",
        r" \1",
        title,
        flags=re.I,
    )
    title = re.sub(r"^['\"](.+?)['\"](?=\s*(?:\(|$))", r"\1", title)

    if artist:
        title = re.sub(
            rf"\s*[\[(]\s*{re.escape(artist)}\s*[-–—|]\s*"
            rf"{DJ_KEY_VALUE_PATTERN}(?:\s*-\s*\d+){{0,3}}\s*[\])]\s*$",
            "",
            title,
            flags=re.I,
        )
        title = re.sub(
            rf"^{re.escape(artist)}\s*[-–—]\s*",
            "",
            title,
            flags=re.I,
        )
        combined = re.fullmatch(r"(.+?)\s*[-–—]\s*(.+)", title)
        if combined:
            combined_artist = clean_artist(combined.group(1))
            if combined_artist and combined_artist.casefold() == artist.casefold():
                title = combined.group(2)
    title = " ".join(title.split()).strip(" -–—|_")
    return title or None


def clean_genre(value: object) -> str | None:
    genre = clean_optional_string(value)
    if genre is None:
        return None
    numeric_code = re.fullmatch(r"\(?([0-9]{1,3})\)?", genre)
    if numeric_code:
        code = int(numeric_code.group(1))
        genre = TCON.GENRES[code] if code < len(TCON.GENRES) else "Unknown"
    if genre.casefold() in GENERIC_GENRES:
        return None
    if genre.startswith(("@", "#")) or DOMAIN_PATTERN.search(genre):
        return None
    if re.fullmatch(r"[A-Za-z0-9_.-]+official", genre, flags=re.I):
        return None
    words = genre.split()
    if len(words) == 2 and words[0].casefold() == words[1].casefold():
        genre = words[0]
    return genre


def clean_positive_integer(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def clean_date(value: object) -> str | None:
    text = clean_optional_string(value)
    if text is None:
        return None
    return text if re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", text) else None


def clean_review_reason(value: object) -> str | None:
    reason = clean_optional_string(value)
    if reason is None:
        return None
    return reason[:300].rstrip()


def clean_output_metadata(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("outputMetadata was not an object.")
    artist = clean_artist(value.get("artist"))
    album = clean_album(value.get("album"))
    needs_review = value.get("needsReview") is True
    review_reason = clean_review_reason(value.get("reviewReason"))
    cleaned = {
        "title": clean_title(value.get("title"), artist),
        "artist": artist,
        "albumArtist": clean_artist(value.get("albumArtist")) if album else None,
        "album": album,
        "date": clean_date(value.get("date")),
        "trackNumber": clean_positive_integer(value.get("trackNumber")) if album else None,
        "discNumber": clean_positive_integer(value.get("discNumber")) if album else None,
        "genre": clean_genre(value.get("genre")),
        "compilation": value.get("compilation") is True and album is not None,
        "needsReview": needs_review,
        "reviewReason": review_reason if needs_review else None,
    }
    if value.get("excluded") is True:
        cleaned["excluded"] = True
        cleaned["exclusionReason"] = (
            clean_review_reason(value.get("exclusionReason"))
            or "Intentionally excluded by user."
        )
        cleaned["needsReview"] = False
        cleaned["reviewReason"] = None
        return cleaned
    if not cleaned["title"] or not cleaned["artist"]:
        cleaned["needsReview"] = True
        if not cleaned["reviewReason"]:
            cleaned["reviewReason"] = (
                "Insufficient reliable evidence for both a clean title and artist."
            )
    elif cleaned["needsReview"] and not cleaned["reviewReason"]:
        cleaned["reviewReason"] = "Material metadata ambiguity requires human review."
    return cleaned


def first_nested_text(value: object) -> str | None:
    if isinstance(value, str):
        return clean_optional_string(value)
    if isinstance(value, list):
        for item in value:
            text = first_nested_text(item)
            if text:
                return text
    if isinstance(value, dict):
        text_value = value.get("text")
        if text_value is not None:
            return first_nested_text(text_value)
    return None


def original_tag_text(item: dict[str, object], *tag_names: str) -> str | None:
    original = item.get("originalMetadata")
    if not isinstance(original, dict):
        return None
    tags = original.get("tags")
    if not isinstance(tags, dict):
        return None
    for tag_name in tag_names:
        for key, value in tags.items():
            if str(key).casefold() == tag_name.casefold():
                text = first_nested_text(value)
                if text:
                    return text
    return None


def original_tag_value(item: dict[str, object], *tag_names: str) -> object | None:
    original = item.get("originalMetadata")
    if not isinstance(original, dict):
        return None
    tags = original.get("tags")
    if not isinstance(tags, dict):
        return None
    wanted = {tag_name.casefold() for tag_name in tag_names}
    for key, value in tags.items():
        if str(key).casefold() in wanted:
            return value
    return None


def first_nested_date(value: object) -> str | None:
    if isinstance(value, str):
        match = re.search(r"\b((?:19|20)\d{2}(?:-\d{2}(?:-\d{2})?)?)\b", value)
        if not match:
            return None
        date = clean_date(match.group(1))
        if date and date.endswith("-01-01"):
            return date[:4]
        return date
    if isinstance(value, list):
        for item in value:
            date = first_nested_date(item)
            if date:
                return date
    if isinstance(value, dict):
        year = value.get("year")
        if isinstance(year, int) and 1900 <= year <= 2099:
            month = value.get("month")
            day = value.get("day")
            if isinstance(month, int) and 1 <= month <= 12:
                if isinstance(day, int) and 1 <= day <= 31:
                    if month == 1 and day == 1:
                        return f"{year:04d}"
                    return f"{year:04d}-{month:02d}-{day:02d}"
                return f"{year:04d}-{month:02d}"
            return f"{year:04d}"
        text_value = value.get("text")
        if text_value is not None:
            return first_nested_date(text_value)
    return None


def first_nested_integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return clean_positive_integer(value)
    if isinstance(value, float) and value.is_integer():
        return clean_positive_integer(int(value))
    if isinstance(value, str):
        text = clean_optional_string(value)
        if text is None:
            return None
        match = re.search(r"\d+", text)
        if match:
            return clean_positive_integer(match.group(0))
        return None
    if isinstance(value, list):
        for item in value:
            number = first_nested_integer(item)
            if number is not None:
                return number
    if isinstance(value, dict):
        for key in ("text", "value", "number"):
            if key in value:
                number = first_nested_integer(value[key])
                if number is not None:
                    return number
    return None


def first_nested_boolean(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().casefold()
        if text in {"1", "true", "yes"}:
            return True
        if text in {"0", "false", "no"}:
            return False
    if isinstance(value, list):
        for item in value:
            result = first_nested_boolean(item)
            if result is not None:
                return result
    if isinstance(value, dict):
        for key in ("text", "value"):
            if key in value:
                result = first_nested_boolean(value[key])
                if result is not None:
                    return result
    return None


def repair_misplaced_artist_title(
    output: dict[str, object], item: dict[str, object]
) -> dict[str, object]:
    artist_tag = original_tag_text(item, "TPE1", "artist")
    title_tag = original_tag_text(item, "TIT2", "title") or Path(
        str(item.get("source", ""))
    ).stem
    if not artist_tag or not title_tag:
        return output

    output_title = clean_optional_string(output.get("title"))
    key_tag = original_tag_text(item, "TKEY", "initialkey")
    title_has_key_suffix = bool(
        output_title
        and re.search(
            rf"\s[-–—|]\s*{re.escape(output_title)}\s*$",
            title_tag,
            flags=re.I,
        )
    )
    if (
        output_title
        and re.fullmatch(DJ_KEY_VALUE_PATTERN, output_title, flags=re.I)
        and (
            title_has_key_suffix
            or (key_tag and output_title.casefold() == key_tag.casefold())
        )
    ):
        tag_artist = clean_artist(artist_tag)
        tag_title = clean_title(title_tag, tag_artist)
        if (
            tag_artist
            and tag_title
            and tag_title.casefold() != output_title.casefold()
            and not PLACEHOLDER_TITLE_PATTERN.fullmatch(tag_title)
        ):
            repaired = dict(output)
            repaired["artist"] = tag_artist
            repaired["title"] = tag_title
            repaired["needsReview"] = False
            repaired["reviewReason"] = None
            return repaired

    combined = re.fullmatch(r"(.+?)\s+[-–—]\s+(.+)", artist_tag)
    version_only = re.fullmatch(
        r"\s*[\[(].*(?:remix|mix|edit|vip|bootleg|rework|dub|style).*?[\])]\s*",
        title_tag,
        flags=re.I,
    )
    if not combined or not version_only:
        return output

    tag_artist = clean_optional_string(combined.group(1))
    tag_title = clean_optional_string(combined.group(2))
    output_artist = clean_optional_string(output.get("artist"))
    if not tag_artist or not tag_title:
        return output
    if output_artist and output_artist.casefold() != tag_artist.casefold():
        return output

    repaired = dict(output)
    repaired["artist"] = tag_artist
    repaired["title"] = clean_title(f"{tag_title} {title_tag}", tag_artist)
    return repaired


TITLE_CONTEXT_GROUP_PATTERN = re.compile(
    r"\s*[\[(][^\])]*(?:remix|mix|edit|version|vip|bootleg|rework|dub|radio|"
    r"extended|vocal|instrumental|acapella|live|demo|feat(?:uring)?|ft\.?)"
    r"[^\])]*[\])]",
    flags=re.I,
)


def _title_without_context(value: str) -> str:
    base = TITLE_CONTEXT_GROUP_PATTERN.sub(" ", value)
    base = re.sub(r"[^\w]+", " ", base, flags=re.UNICODE)
    return " ".join(base.casefold().split())


def _title_context_signature(value: str) -> tuple[str, ...]:
    return tuple(
        re.sub(r"[^\w]+", " ", match.group(0), flags=re.UNICODE).casefold().strip()
        for match in TITLE_CONTEXT_GROUP_PATTERN.finditer(value)
    )


def preserve_supported_title_context(
    output: dict[str, object], item: dict[str, object]
) -> dict[str, object]:
    """Restore explicit version/feature context dropped by the model.

    The substitution is deliberately narrow: the cleaned evidence title must have
    the same context-free identity as the model title and add a balanced group that
    explicitly names a remix, mix, edit, VIP, bootleg, version, or feature credit.
    """

    artist = clean_artist(output.get("artist"))
    current = clean_title(output.get("title"), artist)
    if not current:
        return output
    current_base = _title_without_context(current)
    current_context = set(_title_context_signature(current))
    if not current_base:
        return output

    source_path = Path(str(item.get("source", "")))
    evidence_values = (
        original_tag_text(item, "TIT2", "title"),
        original_tag_text(item, "TALB", "album"),
        source_path.parent.name,
        source_path.stem,
    )
    for evidence in evidence_values:
        candidate = clean_title(evidence, artist)
        if not candidate or _title_without_context(candidate) != current_base:
            continue
        candidate_context = set(_title_context_signature(candidate))
        if (
            not candidate_context
            or not candidate_context.issuperset(current_context)
            or candidate_context == current_context
        ):
            continue
        repaired = dict(output)
        repaired["title"] = candidate
        return repaired
    return output


def preserve_supported_numeric_artist(
    output: dict[str, object], item: dict[str, object]
) -> dict[str, object]:
    """Keep a digit-leading artist when both the tag and filename support it.

    Models can mistake legitimate names such as ``1234`` for a filename index.  This
    guard is deliberately narrow: it applies only to digit-leading artist tags that
    also form the complete artist side of an ``Artist - Title`` filename.
    """
    tag_artist = clean_artist(original_tag_text(item, "TPE1", "artist"))
    if not tag_artist or not re.match(r"^\d", tag_artist):
        return output
    source_path = Path(str(item.get("source", "")))
    source_stem = source_path.stem.strip()
    artist_folder = source_path.parent.parent.name.strip()
    if artist_folder.casefold() != tag_artist.casefold():
        return output
    if not re.match(
        rf"^{re.escape(tag_artist)}\s+[-–—]\s+\S",
        source_stem,
        flags=re.I,
    ):
        return output
    if clean_artist(output.get("artist")) == tag_artist:
        return output
    repaired = dict(output)
    repaired["artist"] = tag_artist
    return repaired


def ground_catalogue_fields(
    output: dict[str, object], item: dict[str, object]
) -> dict[str, object]:
    grounded = dict(output)
    needs_review = output.get("needsReview") is True

    source_album = original_tag_text(item, "TALB", "album", "©alb")
    source_album = clean_album(source_album)
    candidate_album = clean_album(output.get("album"))
    if needs_review and candidate_album is None:
        resolved_album = None
    elif source_album and candidate_album:
        source_key = re.sub(r"[^a-z0-9]+", " ", source_album.casefold()).strip()
        candidate_key = re.sub(r"[^a-z0-9]+", " ", candidate_album.casefold()).strip()
        similarity = difflib.SequenceMatcher(None, source_key, candidate_key).ratio()
        resolved_album = candidate_album if similarity >= 0.65 else source_album
    else:
        resolved_album = source_album
    grounded["album"] = resolved_album

    explicit_compilation = first_nested_boolean(
        original_tag_value(item, "TCMP", "compilation", "cpil")
    )
    compilation_like_album = bool(
        resolved_album and COMPILATION_ALBUM_PATTERN.search(resolved_album)
    )
    grounded["compilation"] = bool(
        resolved_album
        and (
            explicit_compilation is True
            or compilation_like_album
            or output.get("compilation") is True
        )
    )

    if resolved_album is None:
        grounded["albumArtist"] = None
        grounded["trackNumber"] = None
        grounded["discNumber"] = None
        grounded["compilation"] = False
    else:
        source_album_artist = original_tag_text(
            item, "TPE2", "albumartist", "album artist", "aART"
        )
        if grounded["compilation"]:
            grounded["albumArtist"] = "Various Artists"
        else:
            # albumArtist is a release-level fact, not a synonym for track artist.
            # Never retain a model-inferred value when the source has no album-artist
            # tag; doing so creates a large, avoidable second-pass audit queue.
            grounded["albumArtist"] = clean_artist(source_album_artist)
        grounded["trackNumber"] = (
            None
            if needs_review and output.get("trackNumber") is None
            else first_nested_integer(
                original_tag_value(item, "TRCK", "tracknumber", "track", "trkn")
            )
        )
        grounded["discNumber"] = (
            None
            if needs_review and output.get("discNumber") is None
            else first_nested_integer(
                original_tag_value(item, "TPOS", "discnumber", "disc", "disk")
            )
        )

    grounded["date"] = (
        None
        if needs_review and output.get("date") is None
        else first_nested_date(
            original_tag_value(item, "TDRC", "TYER", "date", "year", "©day")
        )
    )

    source_genre = original_tag_text(item, "TCON", "genre", "©gen")
    grounded["genre"] = (
        None
        if needs_review and output.get("genre") is None
        else clean_genre(source_genre)
    )
    return grounded


def constrain_audit_revision(
    original: object, candidate: dict[str, object], feedback: str
) -> dict[str, object]:
    """Limit an audit requeue to the fields named by its accepted feedback."""
    if not isinstance(original, dict):
        return candidate
    text = feedback.casefold()
    album_artist_only = text.startswith("remove unsupported albumartist") or text.startswith(
        "replace albumartist"
    )
    allowed: set[str] = set()
    if "albumartist" in text or "album artist" in text or "album-artist" in text:
        allowed.add("albumArtist")
        text = (
            text.replace("albumartist", "")
            .replace("album artist", "")
            .replace("album-artist", "")
        )
    field_terms = {
        "title": ("title",),
        "artist": ("artist",),
        "album": ("album",),
        "date": ("date", "year"),
        "genre": ("genre",),
        "trackNumber": ("tracknumber", "track number"),
        "discNumber": ("discnumber", "disc number"),
        "compilation": ("compilation",),
    }
    if not album_artist_only:
        for field, terms in field_terms.items():
            if any(term in text for term in terms):
                allowed.add(field)
    if not allowed:
        return dict(original)

    merged = dict(original)
    for field in allowed:
        if field in candidate:
            merged[field] = candidate[field]
    if feedback.casefold().startswith("remove unsupported albumartist"):
        merged["albumArtist"] = None
    # An optional-field correction must not alter an accepted core identity or its
    # review state. Core title/artist feedback may adopt the model's review verdict.
    if allowed & {"title", "artist"}:
        merged["needsReview"] = candidate.get("needsReview", False)
        merged["reviewReason"] = candidate.get("reviewReason")
    return merged


def apply_deterministic_audit_feedback(
    original: object, item: dict[str, object], feedback: str
) -> tuple[dict[str, object] | object, bool]:
    """Apply audit corrections that do not require another model call.

    Album-artist removal/replacement is fully determined by source tags and the
    compilation flag.  Handling it here avoids re-sending an otherwise-correct track
    through llama.cpp merely to clear one optional release field.
    """

    if not isinstance(original, dict):
        return original, False
    text = feedback.casefold()
    if text.startswith("remove unsupported albumartist"):
        corrected = dict(original)
        corrected["albumArtist"] = None
        return corrected, True
    if text.startswith("replace albumartist"):
        source_album_artist = clean_artist(
            original_tag_text(item, "TPE2", "albumartist", "album artist", "aART")
        )
        if source_album_artist is None:
            return original, False
        corrected = dict(original)
        corrected["albumArtist"] = source_album_artist
        return corrected, True
    return original, False


def parse_model_results(text: str, allowed_ids: set[int]) -> dict[int, dict[str, object]]:
    value = extract_json_object(text)
    items = value.get("items")
    if not isinstance(items, list):
        raise ValueError("Model response did not contain an items array.")
    results: dict[int, dict[str, object]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            item_id = int(item.get("id", -1))
        except (TypeError, ValueError):
            continue
        if item_id in allowed_ids:
            legacy = item.get("outputMetadata")
            if isinstance(legacy, dict):
                raw_output = legacy
            else:
                raw_output = {
                    field: item.get(field, default)
                    for field, default in OUTPUT_METADATA_TEMPLATE.items()
                }
                reason = clean_optional_string(raw_output.get("reviewReason"))
                raw_output["needsReview"] = reason is not None
                raw_output["reviewReason"] = reason
            results[item_id] = clean_output_metadata(raw_output)
    if not results:
        raise ValueError("Model response did not contain any requested track ids.")
    return results


def is_populated(item: dict[str, object]) -> bool:
    output = item.get("outputMetadata")
    return (
        isinstance(output, dict)
        and bool(output.get("title"))
        and bool(output.get("artist"))
        and output.get("needsReview") is not True
    )


def is_reviewed(item: dict[str, object]) -> bool:
    output = item.get("outputMetadata")
    return isinstance(output, dict) and output.get("needsReview") is True


def is_processed(item: dict[str, object]) -> bool:
    return is_populated(item) or is_reviewed(item)


def context_title_key(value: object) -> str | None:
    title = clean_optional_string(value)
    if title is None:
        return None
    title = remove_library_noise(title)
    title = CONTEXT_VERSION_PATTERN.sub(" ", title)
    title = re.sub(
        r"\b(?:original|radio|extended)\s+(?:mix|edit|version)\b",
        " ",
        title,
        flags=re.I,
    )
    key = re.sub(r"[^a-z0-9]+", " ", title.casefold()).strip()
    if len(key) < 5 or PLACEHOLDER_TITLE_PATTERN.fullmatch(key):
        return None
    return key


def build_library_context_index(
    manifest: list[dict[str, object]],
) -> dict[str, list[dict[str, object]]]:
    index: dict[str, list[dict[str, object]]] = {}
    for item in manifest:
        if not is_populated(item):
            continue
        output = item.get("outputMetadata")
        assert isinstance(output, dict)
        key = context_title_key(output.get("title"))
        if key is None:
            continue
        context = {
            "sourceFilename": Path(str(item.get("source", ""))).name,
            "title": output.get("title"),
            "artist": output.get("artist"),
            "album": output.get("album"),
            "date": output.get("date"),
        }
        index.setdefault(key, []).append(context)
    return index


def library_context_for_item(
    item: dict[str, object],
    context_index: dict[str, list[dict[str, object]]],
    limit: int = 5,
) -> list[dict[str, object]]:
    output = item.get("outputMetadata")
    title = output.get("title") if isinstance(output, dict) else None
    key = context_title_key(title)
    if key is None:
        return []
    source_name = Path(str(item.get("source", ""))).name.casefold()
    return [
        context
        for context in context_index.get(key, [])
        if str(context.get("sourceFilename", "")).casefold() != source_name
    ][:limit]


def is_lowercase_multiword_name(value: object) -> bool:
    text = clean_optional_string(value)
    if text is None:
        return False
    words = CASING_WORD_PATTERN.findall(text)
    return len(words) >= 2 and any(character.isalpha() for character in text) and text == text.lower()


def likely_title_case_issue(value: object) -> bool:
    text = clean_optional_string(value)
    if text is None:
        return False
    matches = list(CASING_WORD_PATTERN.finditer(text))
    if len(matches) < 2:
        return False
    for match in matches:
        word = match.group(0)
        if len(word) == 1:
            continue
        if match.start() > 0 and text[match.start() - 1] in "$@#":
            continue
        should_start_upper = word.casefold() not in TITLE_CASE_MINOR_WORDS
        if should_start_upper and word[0].islower():
            return True
    return False


def casing_output_reasons(output: dict[str, object]) -> list[str]:
    artist_fields = [
        field
        for field in ("artist", "albumArtist")
        if is_lowercase_multiword_name(output.get(field))
    ]
    release_fields = [
        field
        for field in ("title", "album")
        if likely_title_case_issue(output.get(field))
    ]

    return [f"{field} casing" for field in artist_fields + release_fields]


def suspicious_output_reasons(item: dict[str, object]) -> list[str]:
    output = item.get("outputMetadata")
    if not isinstance(output, dict):
        return ["invalid outputMetadata"]

    try:
        canonical = ground_catalogue_fields(
            preserve_supported_numeric_artist(
                preserve_supported_title_context(
                    repair_misplaced_artist_title(clean_output_metadata(output), item),
                    item,
                ),
                item,
            ),
            item,
        )
    except (TypeError, ValueError):
        return ["invalid outputMetadata"]

    reasons = [
        field
        for field in OUTPUT_METADATA_SCHEMA["properties"]
        if output.get(field) != canonical.get(field)
    ]
    reasons.extend(casing_output_reasons(output))
    return reasons


def metric_text(value: object, decimals: int = 1) -> str:
    try:
        return f"{float(value):.{decimals}f}"
    except (TypeError, ValueError):
        return "?"


def duration_text(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "calculating"
    total_seconds = int(round(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def print_progress(
    completed: int,
    run_total: int,
    library_populated: int,
    library_reviewed: int,
    library_total: int,
    run_started: float,
) -> None:
    elapsed = max(0.0, time.perf_counter() - run_started)
    percentage = (completed / run_total * 100) if run_total else 100.0
    tracks_per_minute = (completed / elapsed * 60) if elapsed > 0 else 0.0
    remaining = max(0, run_total - completed)
    eta = remaining / (completed / elapsed) if completed > 0 and elapsed > 0 else None
    print(
        f"Progress: {completed:,}/{run_total:,} ({percentage:5.1f}%) | "
        f"library {library_populated:,} accepted + {library_reviewed:,} review "
        f"/ {library_total:,} | "
        f"{tracks_per_minute:.1f} tracks/min | "
        f"elapsed {duration_text(elapsed)} | ETA {duration_text(eta)}"
    )


def print_batch_results(
    item_ids: list[int],
    manifest: list[dict[str, object]],
    results: dict[int, dict[str, object]],
    elapsed_seconds: float,
    metrics: dict[str, object],
) -> None:
    prompt_tokens = metrics.get("prompt_tokens") or "?"
    completion_tokens = metrics.get("completion_tokens") or "?"
    prompt_rate = metric_text(metrics.get("prompt_tokens_per_second"))
    completion_rate = metric_text(metrics.get("completion_tokens_per_second"))
    print(
        f"  Completed in {elapsed_seconds:.1f}s | "
        f"prompt {prompt_tokens} tok @ {prompt_rate} tok/s | "
        f"output {completion_tokens} tok @ {completion_rate} tok/s"
    )
    for item_id in item_ids:
        result = results.get(item_id)
        if result is None:
            continue
        source = Path(str(manifest[item_id].get("source", ""))).name
        artist = result.get("artist") or "Unknown Artist"
        title = result.get("title") or "Unknown Title"
        details: list[str] = []
        if result.get("album"):
            details.append(f"album={result['album']}")
        if result.get("date"):
            details.append(f"date={result['date']}")
        if result.get("genre"):
            details.append(f"genre={result['genre']}")
        if result.get("reviewReason"):
            details.append(f"REVIEW={result['reviewReason']}")
        suffix = f" | {', '.join(details)}" if details else ""
        print(f"  [{item_id + 1}] {source} -> {artist} - {title}{suffix}")


def process_batch(
    batch_number: int,
    item_ids: list[int],
    server: ServerConfig,
    manifest: list[dict[str, object]],
    library_context_index: dict[str, list[dict[str, object]]],
    max_value_chars: int,
    max_evidence_chars: int,
    timeout: int,
    max_tokens: int,
    retries: int,
    audit_feedback: dict[int, str] | None = None,
    trace_dir: Path | None = None,
) -> BatchOutcome:
    model_items = [
        model_item(
            item_id,
            manifest[item_id],
            max_value_chars,
            max_evidence_chars,
            library_context_for_item(
                manifest[item_id],
                library_context_index,
            ),
            (audit_feedback or {}).get(item_id),
        )
        for item_id in item_ids
    ]
    retry_errors: list[str] = []
    started = time.perf_counter()
    for attempt in range(retries + 1):
        try:
            response, metrics = call_llama_cpp(
                server.endpoint,
                server.model,
                model_items,
                timeout,
                max_tokens,
                (trace_dir / f"batch-{batch_number:04d}.json") if trace_dir else None,
            )
            result = parse_model_results(response, set(item_ids))
            result = {
                item_id: ground_catalogue_fields(
                    preserve_supported_numeric_artist(
                        preserve_supported_title_context(
                            repair_misplaced_artist_title(output, manifest[item_id]),
                            manifest[item_id],
                        ),
                        manifest[item_id],
                    ),
                    manifest[item_id],
                )
                for item_id, output in result.items()
            }
            return BatchOutcome(
                batch_number=batch_number,
                item_ids=item_ids,
                server=server,
                result=result,
                metrics=metrics,
                elapsed_seconds=time.perf_counter() - started,
                retry_errors=retry_errors,
                error=None,
            )
        except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
            error = f"{type(exc).__name__}: {exc}"
            error_kind = "other"
            if isinstance(exc, LlamaConnectionError) or (
                isinstance(exc, LlamaHTTPError)
                and (exc.status == 503 or "loading model" in exc.details.casefold())
            ):
                error_kind = "unavailable"
            elif isinstance(exc, LlamaHTTPError) and (
                exc.status == 400
                and "exceed" in exc.details.casefold()
                and "context" in exc.details.casefold()
            ):
                error_kind = "context"
            if error_kind != "other":
                return BatchOutcome(
                    batch_number=batch_number,
                    item_ids=item_ids,
                    server=server,
                    result=None,
                    metrics={},
                    elapsed_seconds=time.perf_counter() - started,
                    retry_errors=retry_errors,
                    error=error,
                    error_kind=error_kind,
                )
            if attempt < retries:
                retry_errors.append(error)
                time.sleep(min(2 ** attempt, 5))
                continue
            return BatchOutcome(
                batch_number=batch_number,
                item_ids=item_ids,
                server=server,
                result=None,
                metrics={},
                elapsed_seconds=time.perf_counter() - started,
                retry_errors=retry_errors,
                error=error,
                error_kind="other",
            )

    raise AssertionError("unreachable")


def chunks(items: list[int], size: int):
    for index in range(0, len(items), size):
        yield items[index : index + size]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Populate a music manifest through a local llama.cpp server."
    )
    parser.add_argument("manifest", type=Path, help="Manifest created by build_music_manifest.py.")
    parser.add_argument(
        "--out",
        type=Path,
        help="Output manifest. Default: <manifest-name>-llm.json",
    )
    parser.add_argument(
        "--endpoint",
        action="append",
        dest="endpoints",
        metavar="URL",
        help=(
            "llama.cpp OpenAI-compatible base URL. Repeat for multiple GPU servers. "
            "Default: http://127.0.0.1:8080/v1"
        ),
    )
    parser.add_argument(
        "--model",
        help=(
            "Model alias to use on every endpoint. By default each endpoint's "
            "model is discovered independently from /v1/models."
        ),
    )
    parser.add_argument("--batch-size", type=int, default=5, help="Tracks per request. Default: 5")
    parser.add_argument("--limit", type=int, help="Process only the first N pending tracks.")
    parser.add_argument("--timeout", type=int, default=300, help="Seconds per request. Default: 300")
    parser.add_argument("--max-tokens", type=int, default=2048, help="Maximum output tokens per request. Default: 2048")
    parser.add_argument("--max-value-chars", type=int, default=500, help="Maximum characters sent from any one text tag. Default: 500")
    parser.add_argument("--max-evidence-chars", type=int, default=2400, help="Hard character budget for one track's metadata evidence. Default: 2400")
    parser.add_argument("--retries", type=int, default=1, help="Retries after a failed request. Default: 1")
    parser.add_argument(
        "--allow-shared-endpoints",
        action="store_true",
        help="Allow another music job to share these endpoints (unsafe for single-slot servers).",
    )
    parser.add_argument(
        "--restart",
        action="store_true",
        help="Replace an existing output manifest instead of resuming it.",
    )
    parser.add_argument(
        "--reprocess",
        action="store_true",
        help="Reprocess tracks that already have title and artist output values.",
    )
    parser.add_argument(
        "--reprocess-suspicious",
        action="store_true",
        help=(
            "Audit completed metadata with the deterministic cleaners and reprocess "
            "records containing removable noise or invalid field values."
        ),
    )
    parser.add_argument(
        "--reprocess-reviewed",
        action="store_true",
        help="Reprocess tracks carrying a non-null reviewReason.",
    )
    parser.add_argument(
        "--audit-feedback",
        type=Path,
        help=(
            "JSON output from audit_music_manifest.py. Requeues only the tracks "
            "the audit flagged and supplies its feedback to the local model."
        ),
    )
    parser.add_argument(
        "--trace-dir",
        type=Path,
        help=(
            "Optional local directory for exact per-batch request/response JSON. "
            "These traces can contain original embedded tags."
        ),
    )
    return parser.parse_args()


def main() -> int:
    configure_console_output()
    args = parse_args()
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
    manifest_path = args.manifest.expanduser().resolve()
    output_path = (
        args.out.expanduser().resolve() if args.out else default_output_path(manifest_path)
    )
    trace_dir = args.trace_dir.expanduser().resolve() if args.trace_dir else None
    if not manifest_path.is_file():
        print(f"Manifest does not exist: {manifest_path}", file=sys.stderr)
        return 1
    if (
        args.batch_size < 1
        or args.timeout < 1
        or args.max_tokens < 1
        or args.max_value_chars < 1
        or args.max_evidence_chars < 1
    ):
        print("Batch size, timeouts, token limits, and evidence limits must be positive.", file=sys.stderr)
        return 1
    if args.limit is not None and args.limit < 1:
        print("--limit must be at least 1", file=sys.stderr)
        return 1

    try:
        original = load_manifest(manifest_path)
        if output_path.exists() and not args.restart:
            manifest = load_manifest(output_path)
            if not manifests_match(original, manifest):
                raise ValueError(
                    "Existing output manifest does not contain the same source tracks."
                )
            print(f"Resuming existing output: {output_path}")
        else:
            manifest = original
            write_json_atomic(output_path, manifest)

        servers = [
            ServerConfig(
                endpoint=endpoint,
                model=args.model or discover_model(endpoint, min(args.timeout, 30)),
            )
            for endpoint in endpoints
        ]
    except (OSError, json.JSONDecodeError, ValueError, RuntimeError) as exc:
        print(f"Cannot start: {exc}", file=sys.stderr)
        return 1

    suspicious: dict[int, list[str]] = {}
    if args.reprocess_suspicious:
        for index, item in enumerate(manifest):
            if not is_populated(item):
                continue
            reasons = suspicious_output_reasons(item)
            if reasons:
                suspicious[index] = reasons

    audit_feedback: dict[int, str] = {}
    if args.audit_feedback:
        try:
            payload = json.loads(args.audit_feedback.expanduser().read_text(encoding="utf-8"))
            reviews = payload.get("reviews") if isinstance(payload, dict) else None
            if not isinstance(reviews, list):
                raise ValueError("audit feedback must contain a reviews array")
            for review in reviews:
                if not isinstance(review, dict) or not review.get("needsRevision"):
                    continue
                item_id = review.get("id")
                feedback = review.get("feedback")
                if not isinstance(item_id, int) or not 0 <= item_id < len(manifest):
                    raise ValueError(f"audit feedback id is out of range: {item_id!r}")
                if not isinstance(feedback, str) or not feedback.strip():
                    raise ValueError(f"audit feedback for id {item_id} is empty")
                audit_feedback[item_id] = feedback.strip()
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            print(f"Cannot read audit feedback: {exc}", file=sys.stderr)
            return 1

    deterministic_audit_applied = 0
    for item_id, feedback in list(audit_feedback.items()):
        corrected, applied = apply_deterministic_audit_feedback(
            manifest[item_id].get("outputMetadata"),
            manifest[item_id],
            feedback,
        )
        if not applied:
            continue
        manifest[item_id]["outputMetadata"] = corrected
        del audit_feedback[item_id]
        deterministic_audit_applied += 1
    if deterministic_audit_applied:
        write_json_atomic(output_path, manifest)

    library_context_index = build_library_context_index(manifest)

    pending = [
        index
        for index, item in enumerate(manifest)
        if args.reprocess
        or not is_processed(item)
        or (args.reprocess_reviewed and is_reviewed(item))
        or index in suspicious
        or index in audit_feedback
    ]
    if args.limit is not None:
        pending = pending[: args.limit]
    batches = list(chunks(pending, args.batch_size))
    print(f"GPU servers:    {len(servers)}")
    for index, server in enumerate(servers, start=1):
        print(f"  [{index}] {server.endpoint} -> {server.model}")
    print(f"Pending tracks: {len(pending)}")
    print(f"Context titles: {len(library_context_index):,} accepted title key(s)")
    if args.reprocess_suspicious:
        print(f"Audit requeued:  {len(suspicious)} populated track(s)")
    if audit_feedback:
        print(f"Feedback requeued: {len(audit_feedback)} audited track(s)")
    if deterministic_audit_applied:
        print(
            "Feedback applied locally: "
            f"{deterministic_audit_applied} deterministic track(s)"
        )

    updated = 0
    failed_batches = 0
    run_started = time.perf_counter()
    server_stats: dict[str, dict[str, float]] = {
        server.endpoint: {
            "batches": 0,
            "tracks": 0,
            "seconds": 0,
            "failed_batches": 0,
        }
        for server in servers
    }
    initially_populated = sum(1 for item in manifest if is_populated(item))
    initially_reviewed = sum(1 for item in manifest if is_reviewed(item))
    print_progress(
        completed=0,
        run_total=len(pending),
        library_populated=initially_populated,
        library_reviewed=initially_reviewed,
        library_total=len(manifest),
        run_started=run_started,
    )
    print()
    work_queue = deque(
        (batch_number, item_ids, 0)
        for batch_number, item_ids in enumerate(batches, start=1)
    )
    next_batch_number = len(batches) + 1
    active: dict[Future[BatchOutcome], tuple[ServerConfig, tuple[int, list[int], int]]] = {}
    server_available_at = {server.endpoint: 0.0 for server in servers}
    server_unavailable = {server.endpoint: 0 for server in servers}
    disabled_servers: set[str] = set()

    def submit_task(
        executor: ThreadPoolExecutor,
        server: ServerConfig,
        task: tuple[int, list[int], int],
    ) -> None:
        batch_number, item_ids, _attempts = task
        print(
            f"Batch {batch_number} started on {server.endpoint}: "
            f"{len(item_ids)} track(s) "
            f"(library items {item_ids[0] + 1}-{item_ids[-1] + 1})",
            flush=True,
        )
        future = executor.submit(
            process_batch,
            batch_number,
            item_ids,
            server,
            manifest,
            library_context_index,
            args.max_value_chars,
            args.max_evidence_chars,
            args.timeout,
            args.max_tokens,
            args.retries,
            audit_feedback,
            trace_dir,
        )
        active[future] = (server, task)

    with ThreadPoolExecutor(max_workers=len(servers)) as executor:
        while work_queue or active:
            active_endpoints = {server.endpoint for server, _task in active.values()}
            now = time.monotonic()
            for server in servers:
                if not work_queue:
                    break
                if (
                    server.endpoint in active_endpoints
                    or server.endpoint in disabled_servers
                    or server_available_at[server.endpoint] > now
                ):
                    continue
                submit_task(executor, server, work_queue.popleft())

            if not active:
                usable = [
                    server for server in servers if server.endpoint not in disabled_servers
                ]
                if not usable:
                    failed_batches += len(work_queue)
                    print(
                        f"All llama.cpp endpoints are unavailable; leaving "
                        f"{sum(len(task[1]) for task in work_queue)} track(s) pending.",
                        file=sys.stderr,
                    )
                    work_queue.clear()
                    break
                delay = min(
                    max(0.05, server_available_at[server.endpoint] - time.monotonic())
                    for server in usable
                )
                time.sleep(min(delay, 1.0))
                continue

            completed, _pending_futures = wait(
                active, timeout=1.0, return_when=FIRST_COMPLETED
            )
            for future in completed:
                server, task = active.pop(future)
                batch_number, item_ids, attempts = task
                try:
                    outcome = future.result()
                except Exception as exc:  # pragma: no cover - defensive worker boundary
                    server_stats[server.endpoint]["failed_batches"] += 1
                    print(
                        f"Batch worker on {server.endpoint} crashed: "
                        f"{type(exc).__name__}: {exc}",
                        file=sys.stderr,
                    )
                    if attempts < 2:
                        work_queue.append((batch_number, item_ids, attempts + 1))
                    else:
                        failed_batches += 1
                    continue

                for retry_error in outcome.retry_errors:
                    print(
                        f"  Batch {outcome.batch_number} on {server.endpoint} "
                        f"failed once and retried: {retry_error}",
                        file=sys.stderr,
                    )

                if outcome.result is None:
                    server_stats[server.endpoint]["failed_batches"] += 1
                    if outcome.error_kind == "context" and len(item_ids) > 1:
                        midpoint = len(item_ids) // 2
                        left = (next_batch_number, item_ids[:midpoint], 0)
                        next_batch_number += 1
                        right = (next_batch_number, item_ids[midpoint:], 0)
                        next_batch_number += 1
                        work_queue.appendleft(right)
                        work_queue.appendleft(left)
                        print(
                            f"Batch {batch_number} exceeded context; split into "
                            f"{len(left[1])} and {len(right[1])} track work units.",
                            file=sys.stderr,
                        )
                    elif outcome.error_kind == "context":
                        failed_batches += 1
                        print(
                            f"Track {item_ids[0] + 1} exceeds context even alone; "
                            "it remains pending for evidence-budget review.",
                            file=sys.stderr,
                        )
                    elif outcome.error_kind == "unavailable":
                        server_unavailable[server.endpoint] += 1
                        failures = server_unavailable[server.endpoint]
                        work_queue.appendleft((batch_number, item_ids, attempts))
                        if failures >= 5:
                            disabled_servers.add(server.endpoint)
                            print(
                                f"Endpoint {server.endpoint} disabled after {failures} "
                                "consecutive availability failures; work was requeued.",
                                file=sys.stderr,
                            )
                        else:
                            delay = min(2 ** failures, 30)
                            server_available_at[server.endpoint] = time.monotonic() + delay
                            print(
                                f"Endpoint {server.endpoint} unavailable; batch "
                                f"{batch_number} requeued and endpoint paused {delay}s.",
                                file=sys.stderr,
                            )
                    elif attempts < 2:
                        work_queue.append((batch_number, item_ids, attempts + 1))
                        print(
                            f"Batch {batch_number} failed on {server.endpoint} and was "
                            f"requeued ({attempts + 1}/2): {outcome.error}",
                            file=sys.stderr,
                        )
                    else:
                        failed_batches += 1
                        print(
                            f"Batch {batch_number} failed permanently on "
                            f"{server.endpoint}: {outcome.error}",
                            file=sys.stderr,
                        )
                    continue

                server_unavailable[server.endpoint] = 0
                server_available_at[server.endpoint] = 0.0
                for item_id, output_metadata in outcome.result.items():
                    if audit_feedback and item_id in audit_feedback:
                        output_metadata = constrain_audit_revision(
                            manifest[item_id].get("outputMetadata"),
                            output_metadata,
                            audit_feedback[item_id],
                        )
                    manifest[item_id]["outputMetadata"] = output_metadata
                    updated += 1
                stats = server_stats[server.endpoint]
                stats["batches"] += 1
                stats["tracks"] += len(outcome.result)
                stats["seconds"] += outcome.elapsed_seconds
                write_json_atomic(output_path, manifest)
                print(
                    f"Batch {outcome.batch_number} completed on "
                    f"{server.endpoint}"
                )
                print_batch_results(
                    outcome.item_ids,
                    manifest,
                    outcome.result,
                    outcome.elapsed_seconds,
                    outcome.metrics,
                )
                populated_so_far = sum(1 for item in manifest if is_populated(item))
                reviewed_so_far = sum(1 for item in manifest if is_reviewed(item))
                print(
                    f"  Saved {len(outcome.result)} result(s) | "
                    f"accepted {populated_so_far}, review {reviewed_so_far} "
                    f"/ {len(manifest)}"
                )
                print_progress(
                    completed=updated,
                    run_total=len(pending),
                    library_populated=populated_so_far,
                    library_reviewed=reviewed_so_far,
                    library_total=len(manifest),
                    run_started=run_started,
                )
                print()

                missing = set(outcome.item_ids) - set(outcome.result)
                if missing:
                    if attempts < 2:
                        for item_id in sorted(missing, reverse=True):
                            work_queue.appendleft(
                                (next_batch_number, [item_id], attempts + 1)
                            )
                            next_batch_number += 1
                    else:
                        failed_batches += len(missing)
                    print(
                        f"  Model omitted {len(missing)} track(s); "
                        + (
                            "requeued individually."
                            if attempts < 2
                            else "retry limit reached; they remain pending."
                        ),
                        file=sys.stderr,
                    )

    populated = sum(1 for item in manifest if is_populated(item))
    reviewed = sum(1 for item in manifest if is_reviewed(item))
    print(f"Tracks updated:   {updated}")
    print(f"Accepted:         {populated}/{len(manifest)}")
    print(f"Needs review:     {reviewed}/{len(manifest)}")
    print(f"Failed batches:   {failed_batches}")
    print(f"Elapsed:          {duration_text(time.perf_counter() - run_started)}")
    print("Endpoint totals:")
    for server in servers:
        stats = server_stats[server.endpoint]
        tracks = int(stats["tracks"])
        seconds = stats["seconds"]
        average = seconds / stats["batches"] if stats["batches"] else 0
        print(
            f"  {server.endpoint}: {tracks:,} track(s), "
            f"{int(stats['batches'])} batch(es), "
            f"avg {average:.1f}s/batch, "
            f"{int(stats['failed_batches'])} failed"
        )
    print(f"Output manifest:  {output_path}")
    return 0 if failed_batches == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
