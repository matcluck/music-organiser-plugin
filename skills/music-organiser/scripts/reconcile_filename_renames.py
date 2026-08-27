#!/usr/bin/env python3
"""Backup-first reconciliation of approved audio filename renames in djay and Rekordbox.

The plan is JSON: [{"old": "<library>\\...", "new": "<library>\\..."}].
Run without --apply to validate; --apply stages both databases, verifies them,
then renames audio and atomically publishes the staged databases.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from urllib.parse import quote
from datetime import datetime, timezone
from pathlib import Path


def load_djay(workspace: Path):
    source = workspace / "djay.py"
    spec = importlib.util.spec_from_file_location("djay_filename_reconcile", source)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {source}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def require_apps_closed() -> None:
    result = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH"], text=True, capture_output=True, check=True
    )
    running = [name for name in ("djayApp.exe", "rekordbox.exe", "rekordboxAgent.exe")
               if name.casefold() in result.stdout.casefold()]
    if running:
        raise RuntimeError("Close these applications before applying: " + ", ".join(running))


def sqlite_backup(source: Path, destination: Path) -> None:
    with sqlite3.connect(source) as source_db, sqlite3.connect(destination) as destination_db:
        source_db.backup(destination_db)


def stage_for_publish(candidate: Path, target: Path) -> Path:
    """Copy a verified candidate beside its target so Windows replacement is same-volume."""
    staged = target.with_name(f".__codex_{target.name}.staged")
    if staged.exists():
        raise FileExistsError(f"Refusing to overwrite staged database: {staged}")
    shutil.copy2(candidate, staged)
    return staged


def path_key(value: Path | str) -> str:
    return os.path.normcase(str(Path(value).resolve()))


def load_plan(path: Path) -> list[tuple[Path, Path]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise ValueError("Plan must be a non-empty JSON array.")
    pairs = [(Path(item["old"]).resolve(), Path(item["new"]).resolve()) for item in data]
    if len({path_key(old) for old, _ in pairs}) != len(pairs):
        raise ValueError("Plan contains duplicate source paths.")
    if len({path_key(new) for _, new in pairs}) != len(pairs):
        raise ValueError("Plan contains duplicate destination paths.")
    for old, new in pairs:
        if not old.is_file():
            raise FileNotFoundError(old)
        if new.exists():
            raise FileExistsError(f"Destination already exists: {new}")
    return pairs


def validate_djay(djay, database: Path, pairs: list[tuple[Path, Path]]) -> dict[Path, tuple[str, str]]:
    records = djay.location_records_for_paths(database, [old for old, _ in pairs])
    # Old Windows djay records can use file:///C:%5C... rather than file:///C:/....
    # Match that form exactly when normal decoded-path matching is unavailable.
    unresolved = [old for old, _ in pairs if old not in records]
    if unresolved:
        expected = {
            "file:///" + quote(str(old).replace("\\", "%5C"), safe=":%") : old
            for old in unresolved
        }
        with sqlite3.connect(database) as connection:
            for key, raw in connection.execute(
                "SELECT key,data FROM database2 WHERE collection='localMediaItemLocations'"
            ):
                for uri in djay.iter_urls(djay.TSAF.parse(bytes(raw)).root, djay.TSAF):
                    old = expected.get(uri)
                    if old is not None:
                        records[old] = (str(key), uri)
    missing = [str(old) for old, _ in pairs if old not in records]
    if missing:
        raise RuntimeError("djay has no exact stored path for: " + "; ".join(missing))
    return records


def rewrite_djay(djay, database: Path, records: dict[Path, tuple[str, str]], pairs):
    with sqlite3.connect(database) as connection:
        for old, new in pairs:
            identifier, old_uri = records[old]
            rowid, raw = connection.execute(
                "SELECT rowid,data FROM database2 WHERE collection='localMediaItemLocations' AND key=?", (identifier,)
            ).fetchone()
            document = djay.TSAF.parse(bytes(raw))
            if djay.TSAF.serialize(document) != bytes(raw):
                raise RuntimeError(f"djay TSAF round-trip failed for {identifier}")
            changed = djay.replace_exact_url(document.root, old_uri, new.as_uri())
            if changed != 1:
                raise RuntimeError(f"djay expected one URL for {old}, found {changed}")
            blob = djay.TSAF.serialize(document)
            if djay.TSAF.serialize(djay.TSAF.parse(blob)) != blob:
                raise RuntimeError(f"djay TSAF verification failed for {identifier}")
            connection.execute("UPDATE database2 SET data=? WHERE rowid=?", (blob, rowid))
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"djay candidate integrity failed: {integrity}")


def rewrite_rekordbox(database: Path, pairs: list[tuple[Path, Path]], allow_missing: bool = False):
    from pyrekordbox import Rekordbox6Database
    from pyrekordbox.db6 import DjmdContent
    from sqlalchemy import text
    wanted = {path_key(old): new for old, new in pairs}
    matched: set[str] = set()
    with Rekordbox6Database(path=database, db_dir=database.parent) as db:
        for content in db.query(DjmdContent).all():
            old = path_key(str(content.FolderPath or ""))
            if old in wanted:
                content.FolderPath = str(wanted[old])
                matched.add(old)
        missing = set(wanted) - matched
        if missing and not allow_missing:
            raise RuntimeError("Rekordbox has no exact stored path for: " + "; ".join(sorted(missing)))
        db.commit()
        integrity = db.session.connection().execute(text("PRAGMA integrity_check")).scalar()
        foreign_keys = list(db.session.connection().execute(text("PRAGMA foreign_key_check")).fetchall())
        if integrity != "ok" or foreign_keys:
            raise RuntimeError(f"Rekordbox candidate validation failed: integrity={integrity}, foreign_keys={len(foreign_keys)}")
    db.engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("plan", type=Path)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--djay-db", type=Path, required=True)
    parser.add_argument("--rekordbox-db", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-missing-rekordbox", action="store_true")
    args = parser.parse_args()
    pairs = load_plan(args.plan)
    require_apps_closed()
    djay = load_djay(args.workspace.resolve())
    records = validate_djay(djay, args.djay_db.resolve(), pairs)
    report = {"createdAt": datetime.now(timezone.utc).isoformat(), "pairs": [
        {"old": str(old), "new": str(new), "djayIdentifier": records[old][0]} for old, new in pairs
    ]}
    if not args.apply:
        report["status"] = "validated-dry-run"
        print(json.dumps(report, indent=2))
        return
    work = args.work.resolve(); work.mkdir(parents=True, exist_ok=False)
    backups = work / "backups"; backups.mkdir()
    candidate = work / "candidate"; candidate.mkdir()
    djay_candidate = candidate / "MediaLibrary.db"
    rekordbox_candidate = candidate / "master.db"
    sqlite_backup(args.djay_db.resolve(), djay_candidate)
    shutil.copy2(args.rekordbox_db.resolve(), rekordbox_candidate)
    shutil.copy2(args.djay_db.resolve(), backups / "MediaLibrary.db")
    shutil.copy2(args.rekordbox_db.resolve(), backups / "master.db")
    xml = args.rekordbox_db.parent / "masterPlaylists6.xml"
    if xml.is_file(): shutil.copy2(xml, backups / xml.name)
    rewrite_djay(djay, djay_candidate, records, pairs)
    rewrite_rekordbox(rekordbox_candidate, pairs, args.allow_missing_rekordbox)
    djay_staged = stage_for_publish(djay_candidate, args.djay_db.resolve())
    rekordbox_staged = stage_for_publish(rekordbox_candidate, args.rekordbox_db.resolve())
    renamed: list[tuple[Path, Path]] = []
    try:
        for old, new in pairs:
            new.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old, new)
            if old.stat().st_size != new.stat().st_size:
                raise RuntimeError(f"Copied bytes differ: {old} -> {new}")
            old.unlink(); renamed.append((old, new))
        os.replace(djay_staged, args.djay_db.resolve())
        os.replace(rekordbox_staged, args.rekordbox_db.resolve())
    except Exception:
        for old, new in reversed(renamed):
            if new.exists() and not old.exists():
                old.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(new, old); new.unlink()
        for staged in (djay_staged, rekordbox_staged):
            if staged.exists(): staged.unlink()
        raise
    report["status"] = "applied"
    (work / "operation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
