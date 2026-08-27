#!/usr/bin/env python3
"""Apply safe existing-library skips from an import-plan collision audit."""

from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path


PLAN_REQUIRED_FIELDS = {"sourcePath", "newPath"}
AUDIT_REQUIRED_FIELDS = {
    "sourcePath",
    "newPath",
    "existingIdentityPaths",
    "recommendation",
}


def path_key(value: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(value))).casefold()


def read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        return list(reader.fieldnames), list(reader)


def resolve_plan(
    plan_fields: list[str],
    plan_rows: list[dict[str, str]],
    audit_rows: list[dict[str, str]],
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    missing_plan = PLAN_REQUIRED_FIELDS.difference(plan_fields)
    if missing_plan:
        raise ValueError(f"Plan is missing fields: {', '.join(sorted(missing_plan))}")

    by_source: dict[str, dict[str, str]] = {}
    for row in plan_rows:
        source = row.get("sourcePath", "").strip()
        if not source:
            raise ValueError("Plan contains a blank sourcePath")
        key = path_key(source)
        if key in by_source:
            raise ValueError(f"Plan contains duplicate sourcePath: {source}")
        by_source[key] = row

    skipped: list[dict[str, str]] = []
    seen_audit: set[str] = set()
    for finding in audit_rows:
        source = finding.get("sourcePath", "").strip()
        key = path_key(source)
        if key in seen_audit:
            raise ValueError(f"Audit contains duplicate sourcePath: {source}")
        seen_audit.add(key)
        if key not in by_source:
            raise ValueError(f"Audit sourcePath is absent from plan: {source}")

        plan_row = by_source[key]
        audited_destination = finding.get("newPath", "").strip()
        if path_key(plan_row.get("newPath", "")) != path_key(audited_destination):
            raise ValueError(f"Audit destination does not match plan for: {source}")
        if finding.get("recommendation", "").strip() != "skip_existing":
            raise ValueError(
                f"Unsafe or unresolved audit recommendation for {source}: "
                f"{finding.get('recommendation', '')!r}"
            )
        if not finding.get("existingIdentityPaths", "").strip():
            raise ValueError(f"skip_existing lacks identity evidence for: {source}")

        plan_row["newPath"] = ""
        skipped.append(dict(finding))

    return plan_rows, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plan", type=Path)
    parser.add_argument("audit", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--skip-report", type=Path)
    args = parser.parse_args()

    plan_fields, plan_rows = read_csv(args.plan)
    audit_fields, audit_rows = read_csv(args.audit)
    missing_audit = AUDIT_REQUIRED_FIELDS.difference(audit_fields)
    if missing_audit:
        raise ValueError(f"Audit is missing fields: {', '.join(sorted(missing_audit))}")

    resolved_rows, skipped = resolve_plan(plan_fields, plan_rows, audit_rows)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=plan_fields)
        writer.writeheader()
        writer.writerows(resolved_rows)

    if args.skip_report is not None:
        args.skip_report.parent.mkdir(parents=True, exist_ok=True)
        with args.skip_report.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=audit_fields)
            writer.writeheader()
            writer.writerows(skipped)

    active = sum(bool(row.get("newPath", "").strip()) for row in resolved_rows)
    print(f"Audited existing-library skips applied: {len(skipped):,}")
    print(f"Importable plan rows remaining:         {active:,}")
    print(f"Resolved plan:                          {args.out}")
    if args.skip_report is not None:
        print(f"Skip evidence:                          {args.skip_report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
