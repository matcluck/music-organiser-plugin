#!/usr/bin/env python3
"""Run the deterministic review, indexed audit, and import-ready copy workflow."""

from __future__ import annotations

import argparse
import csv
import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def csv_has_rows(path: Path) -> bool:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return next(csv.DictReader(handle), None) is not None


def run_script(name: str, *arguments: object) -> None:
    command = [sys.executable, str(SCRIPT_DIR / name), *(str(value) for value in arguments)]
    subprocess.run(command, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("source_root", type=Path)
    parser.add_argument("library", type=Path)
    parser.add_argument("work", type=Path)
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--resolutions", type=Path)
    parser.add_argument("--prepared-root", type=Path)
    parser.add_argument("--force-index-verify", action="store_true")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    manifest = args.manifest.expanduser().resolve()
    source_root = args.source_root.expanduser().resolve()
    library = args.library.expanduser().resolve()
    work = args.work.expanduser().resolve()
    index = args.index.expanduser().resolve()
    prepared_root = (
        args.prepared_root.expanduser().resolve()
        if args.prepared_root
        else work / "import-ready-audio"
    )
    if not manifest.is_file() or not source_root.is_dir() or not library.is_dir():
        parser.error("Manifest, source root, and library must exist.")
    if work in {source_root, library} or not is_within(prepared_root, work):
        parser.error("Work must be separate and prepared-root must remain inside it.")
    work.mkdir(parents=True, exist_ok=True)

    ready_manifest = manifest
    if args.resolutions:
        ready_manifest = work / "manifest-import-ready.json"
        run_script(
            "apply_music_review_resolutions.py",
            manifest,
            args.resolutions.expanduser().resolve(),
            "--out",
            ready_manifest,
            "--overwrite",
        )

    review_csv = work / "metadata-review-import-ready.csv"
    run_script("build_music_review_csv.py", ready_manifest, library, "--out", review_csv)
    if csv_has_rows(review_csv):
        print(f"Blocked: unresolved metadata review rows remain in {review_csv}", file=sys.stderr)
        return 2

    library_plan = work / "import-plan-library.csv"
    collision_csv = work / "import-plan-library-audit.csv"
    run_script("build_music_output_csv.py", ready_manifest, library, "--out", library_plan)
    audit_arguments: list[object] = [
        library_plan,
        library,
        "--out",
        collision_csv,
        "--index",
        index,
        "--refresh-index",
    ]
    if args.force_index_verify:
        audit_arguments.append("--force-index-verify")
    run_script("audit_import_plan.py", *audit_arguments)
    if csv_has_rows(collision_csv):
        print(f"Blocked: live-library collisions require review in {collision_csv}", file=sys.stderr)
        return 3

    prepared_plan = work / "import-ready-plan.csv"
    journal = work / "import-ready-apply.jsonl"
    run_script("build_music_output_csv.py", ready_manifest, prepared_root, "--out", prepared_plan)
    apply_arguments: list[object] = [
        ready_manifest,
        prepared_plan,
        source_root,
        prepared_root,
        "--action",
        "copy",
        "--journal",
        journal,
        "--resume",
    ]
    run_script("apply_music_plan.py", *apply_arguments)
    if not args.apply:
        print("Preparation preflight passed; no audio copies were written.")
        return 0
    run_script("apply_music_plan.py", *apply_arguments, "--apply")
    playlist = work / "import-ready.m3u8"
    run_script("build_import_playlist.py", prepared_plan, "--out", playlist)
    print(f"Import-ready audio: {prepared_root}")
    print(f"Import playlist:    {playlist}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
