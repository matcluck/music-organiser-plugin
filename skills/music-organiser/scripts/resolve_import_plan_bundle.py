#!/usr/bin/env python3
"""Resolve duplicate tracks and destinations across multiple import plans."""

from __future__ import annotations

import argparse
import csv
import os
from dataclasses import dataclass
from pathlib import Path

from build_music_output_csv import duplicate_preference
from music_library_index import normalize_identity


def path_key(value: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(value))).casefold()


@dataclass
class Plan:
    path: Path
    fields: list[str]
    rows: list[dict[str, str]]


def load_plan(path: Path) -> Plan:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"Plan has no header: {path}")
        fields = list(reader.fieldnames)
        required = {"sourcePath", "newPath", "artist", "title"}
        missing = required.difference(fields)
        if missing:
            raise ValueError(f"Plan is missing fields: {', '.join(sorted(missing))}")
        return Plan(path, fields, list(reader))


def resolve_bundle(plans: list[Plan]) -> list[dict[str, str]]:
    active: list[tuple[int, int, dict[str, str]]] = []
    for plan_index, plan in enumerate(plans):
        for row_index, row in enumerate(plan.rows):
            if row.get("newPath", "").strip():
                active.append((plan_index, row_index, row))

    parent = list(range(len(active)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    by_destination: dict[str, int] = {}
    by_identity: dict[tuple[str, str], int] = {}
    for index, (_plan_index, _row_index, row) in enumerate(active):
        destination = path_key(row["newPath"])
        if destination in by_destination:
            union(index, by_destination[destination])
        else:
            by_destination[destination] = index

        identity = (
            normalize_identity(row.get("artist")),
            normalize_identity(row.get("title")),
        )
        if all(identity):
            if identity in by_identity:
                union(index, by_identity[identity])
            else:
                by_identity[identity] = index

    groups: dict[int, list[int]] = {}
    for index in range(len(active)):
        groups.setdefault(find(index), []).append(index)

    skipped: list[dict[str, str]] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        winner = min(
            members,
            key=lambda index: duplicate_preference(Path(active[index][2]["sourcePath"])),
        )
        winner_row = active[winner][2]
        for index in members:
            if index == winner:
                continue
            plan_index, _row_index, row = active[index]
            same_destination = path_key(row["newPath"]) == path_key(winner_row["newPath"])
            same_identity = (
                normalize_identity(row.get("artist")),
                normalize_identity(row.get("title")),
            ) == (
                normalize_identity(winner_row.get("artist")),
                normalize_identity(winner_row.get("title")),
            )
            original_destination = row["newPath"]
            row["newPath"] = ""
            skipped.append({
                "skippedPlan": str(plans[plan_index].path),
                "skippedSourcePath": row.get("sourcePath", ""),
                "skippedDestination": original_destination,
                "keptSourcePath": winner_row.get("sourcePath", ""),
                "keptDestination": winner_row.get("newPath", ""),
                "artist": row.get("artist", ""),
                "title": row.get("title", ""),
                "reason": "+".join(
                    part for part, matched in (
                        ("same_destination", same_destination),
                        ("same_identity", same_identity),
                    ) if matched
                ),
            })
    return skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plans", nargs="+", type=Path)
    parser.add_argument("--out-suffix", default="-bundle-resolved")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Do not write plans; fail if any active cross-plan duplicate remains.",
    )
    args = parser.parse_args()

    plans = [load_plan(path) for path in args.plans]
    skipped = resolve_bundle(plans)
    if not args.verify_only:
        for plan in plans:
            output = plan.path.with_name(f"{plan.path.stem}{args.out_suffix}{plan.path.suffix}")
            with output.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=plan.fields)
                writer.writeheader()
                writer.writerows(plan.rows)
            active = sum(bool(row.get("newPath", "").strip()) for row in plan.rows)
            print(f"{output}: {active:,} importable")

    report_fields = [
        "skippedPlan", "skippedSourcePath", "skippedDestination",
        "keptSourcePath", "keptDestination", "artist", "title", "reason",
    ]
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with args.report.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=report_fields)
        writer.writeheader()
        writer.writerows(skipped)
    print(f"Cross-plan duplicate skips: {len(skipped):,}")
    print(f"Evidence report:            {args.report}")
    if args.verify_only and skipped:
        print("Cross-plan verification failed: active duplicates remain.")
        return 1
    if args.verify_only:
        print("Cross-plan verification passed: zero active duplicates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
