#!/usr/bin/env python3
"""Delete only verified lower-priority duplicate sources from a music plan."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from apply_music_plan import (
    PlanError,
    artwork_hashes,
    file_sha256,
    is_within,
    load_csv_rows,
    make_writable,
    metadata_values,
    normalized_path,
    validate_contract,
    verify_rewritten_file,
)
from build_music_output_csv import load_manifest, planned_path


@dataclass(frozen=True)
class DuplicateSkip:
    source: Path
    destination: Path
    destination_metadata: dict[str, object]


def destination_identity(path: Path) -> str:
    return normalized_path(path.parent / path.stem)


def find_duplicate_skips(
    manifest: list[dict[str, object]],
    csv_rows: list[dict[str, str]],
    source_root: Path,
    destination_root: Path,
) -> list[DuplicateSkip]:
    validate_contract(
        manifest,
        csv_rows,
        source_root,
        destination_root,
        # Winner source files were already removed after the locked copy plan
        # completed, so source-size quality rankings can no longer be recomputed.
        strict_duplicate_check=False,
    )

    winners: dict[str, tuple[Path, dict[str, object]]] = {}
    prepared: list[tuple[Path, Path | None, str]] = []
    for item, row in zip(manifest, csv_rows, strict=True):
        source = Path(row["sourcePath"])
        output = item.get("outputMetadata")
        if not isinstance(output, dict):
            output = {}
        candidate = planned_path(source, destination_root, output)
        new_path = row["newPath"].strip()
        prepared.append((source, candidate, new_path))
        if new_path:
            destination = Path(new_path)
            winners[destination_identity(destination)] = (
                destination,
                metadata_values(output),
            )

    duplicates: list[DuplicateSkip] = []
    seen_sources: set[str] = set()
    for source, candidate, new_path in prepared:
        if new_path or candidate is None:
            continue
        source_key = normalized_path(source)
        if source_key in seen_sources:
            raise PlanError(f"Duplicate cleanup source appears more than once: {source}")
        winner = winners.get(destination_identity(candidate))
        if winner is None:
            raise PlanError(f"Duplicate source has no selected winner: {source}")
        destination, destination_metadata = winner
        if not is_within(source, source_root):
            raise PlanError(f"Duplicate source escapes the source root: {source}")
        if not is_within(destination, destination_root):
            raise PlanError(f"Duplicate winner escapes the destination root: {destination}")
        seen_sources.add(source_key)
        duplicates.append(DuplicateSkip(source, destination, destination_metadata))
    return duplicates


def journal_header(
    manifest_path: Path,
    plan_path: Path,
    source_root: Path,
    destination_root: Path,
    count: int,
) -> dict[str, object]:
    return {
        "type": "header",
        "version": 1,
        "operation": "remove-duplicate-skips",
        "manifest": str(manifest_path),
        "manifestSha256": file_sha256(manifest_path),
        "plan": str(plan_path),
        "planSha256": file_sha256(plan_path),
        "sourceRoot": str(source_root),
        "destinationRoot": str(destination_root),
        "duplicateCount": count,
    }


def load_deleted_sources(
    path: Path,
    expected_header: dict[str, object],
    resume: bool,
) -> set[str]:
    if not resume:
        if path.exists():
            raise PlanError(f"Cleanup journal already exists: {path}")
        return set()
    if not path.is_file():
        raise PlanError(f"Cleanup journal does not exist: {path}")

    deleted: set[str] = set()
    with path.open("r", encoding="utf-8") as file:
        first = file.readline()
        try:
            existing_header = json.loads(first)
        except json.JSONDecodeError as exc:
            raise PlanError(f"Cleanup journal has an invalid header: {path}") from exc
        if existing_header != expected_header:
            raise PlanError("Cleanup journal does not match the current plan and roots.")
        for line_number, line in enumerate(file, start=2):
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PlanError(
                    f"Cleanup journal has invalid JSON on line {line_number}."
                ) from exc
            if record.get("type") == "source" and record.get("status") == "deleted":
                deleted.add(normalized_path(Path(str(record.get("source", "")))))
    return deleted


def verify_cleanup(
    duplicates: list[DuplicateSkip],
    previously_deleted: set[str],
    progress_every: int,
) -> None:
    errors: list[str] = []
    verified_destinations: set[str] = set()
    for index, duplicate in enumerate(duplicates, start=1):
        try:
            source_key = normalized_path(duplicate.source)
            if not duplicate.source.is_file() and source_key not in previously_deleted:
                raise PlanError(f"Duplicate source is missing: {duplicate.source}")

            destination_key = normalized_path(duplicate.destination)
            if destination_key not in verified_destinations:
                if not duplicate.destination.is_file():
                    raise PlanError(
                        f"Selected duplicate winner is missing: {duplicate.destination}"
                    )
                artwork_count = len(artwork_hashes(duplicate.destination))
                verify_rewritten_file(
                    duplicate.destination,
                    duplicate.destination_metadata,
                    artwork_count,
                )
                verified_destinations.add(destination_key)
        except (OSError, PlanError) as exc:
            if len(errors) < 50:
                errors.append(str(exc))
        if progress_every and (
            index % progress_every == 0 or index == len(duplicates)
        ):
            print(f"Preflight: {index:,}/{len(duplicates):,}")
    if errors:
        raise PlanError(
            "Duplicate cleanup preflight failed (showing up to 50):\n  "
            + "\n  ".join(errors)
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Delete verified lower-priority duplicate source tracks."
    )
    parser.add_argument("manifest", type=Path)
    parser.add_argument("plan", type=Path)
    parser.add_argument("source_root", type=Path)
    parser.add_argument("destination_root", type=Path)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--progress-every", type=int, default=100)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest_path = args.manifest.expanduser().resolve()
    plan_path = args.plan.expanduser().resolve()
    source_root = args.source_root.expanduser().resolve()
    destination_root = args.destination_root.expanduser().resolve()
    journal_path = args.journal.expanduser().resolve()

    try:
        manifest = load_manifest(manifest_path)
        csv_rows = load_csv_rows(plan_path)
        duplicates = find_duplicate_skips(
            manifest, csv_rows, source_root, destination_root
        )
        header = journal_header(
            manifest_path,
            plan_path,
            source_root,
            destination_root,
            len(duplicates),
        )
        deleted = load_deleted_sources(journal_path, header, args.resume)
        verify_cleanup(duplicates, deleted, args.progress_every)
    except (OSError, PlanError, ValueError, json.JSONDecodeError) as exc:
        print(f"Cannot remove duplicate skips: {exc}", file=sys.stderr)
        return 1

    remaining = sum(
        1 for item in duplicates if normalized_path(item.source) not in deleted
    )
    print(f"Duplicate skips:      {len(duplicates):,}")
    print(f"Previously deleted:   {len(deleted):,}")
    print(f"Remaining this run:   {remaining:,}")
    print("Winner destinations: verified")
    if not args.apply:
        print("Dry run complete; no sources were deleted.")
        return 0

    mode = "a" if args.resume else "x"
    completed = 0
    try:
        with journal_path.open(mode, encoding="utf-8", newline="\n") as journal:
            if not args.resume:
                journal.write(json.dumps(header, ensure_ascii=False) + "\n")
                journal.flush()
            for index, duplicate in enumerate(duplicates, start=1):
                source_key = normalized_path(duplicate.source)
                if source_key not in deleted:
                    make_writable(duplicate.source)
                    duplicate.source.unlink()
                    journal.write(
                        json.dumps(
                            {
                                "type": "source",
                                "status": "deleted",
                                "source": str(duplicate.source),
                                "winner": str(duplicate.destination),
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    journal.flush()
                    completed += 1
                if args.progress_every and (
                    index % args.progress_every == 0 or index == len(duplicates)
                ):
                    print(f"Applied: {index:,}/{len(duplicates):,}")
    except OSError as exc:
        print(f"Stopped after deleting {completed:,} source(s): {exc}", file=sys.stderr)
        print(f"Resume with --resume. Journal: {journal_path}")
        return 1

    print(f"Deleted this run:     {completed:,}")
    print(f"Journal:              {journal_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
