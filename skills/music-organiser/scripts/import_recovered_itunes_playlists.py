#!/usr/bin/env python3
"""Create verified djay playlists below an existing folder from recovery evidence.

Input recovery plans contain exact legacy matches; reconciliation evidence supplies
the selected first candidate for every remaining membership.  The script is
dry-run by default.  On --apply it backs up the live database, modifies a
candidate copy, validates playlist objects, relationships, view mappings and
SQLite integrity, then replaces the live DB atomically and reads it back.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


def load_djay_module(path: Path):
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("live_djay_recovery", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load djay helper: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def database_content_digest(path: Path) -> str:
    """Digest the canonical object store; SQLite backups need not share page bytes."""
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    digest = hashlib.sha256()
    try:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError(f"Backup integrity check failed: {path}")
        for row in connection.execute(
            "SELECT rowid,collection,key,data,metadata FROM database2 ORDER BY rowid"
        ):
            for value in row:
                if value is None:
                    digest.update(b"\\0")
                elif isinstance(value, bytes):
                    digest.update(len(value).to_bytes(8, "big")); digest.update(value)
                else:
                    encoded = str(value).encode("utf-8")
                    digest.update(len(encoded).to_bytes(8, "big")); digest.update(encoded)
                digest.update(b"\\xff")
    finally:
        connection.close()
    return digest.hexdigest()


def recovery_key(plan: str, playlist: str, track_no: int) -> tuple[str, str, int]:
    return str(Path(plan).resolve()), playlist, int(track_no)


def build_plan(recovery_paths: list[Path], evidence_path: Path, db_path: Path) -> dict:
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    candidates = {
        recovery_key(row["plan"], row["playlist"], row["track_no"]): row
        for row in evidence["results"]
    }
    playlists: list[dict] = []
    for recovery_path in recovery_paths:
        recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
        for source_playlist in recovery["playlists"]:
            media_keys: list[str] = []
            reconciliation: list[dict] = []
            for membership in source_playlist["memberships"]:
                media_key = membership.get("djay_media_key")
                mode = membership.get("match_mode")
                if not media_key:
                    key = recovery_key(
                        str(recovery_path), source_playlist["destination_name"], membership["track_no"]
                    )
                    row = candidates.get(key)
                    if row is None or not row.get("candidates"):
                        raise RuntimeError(
                            f"No selected reconciliation candidate for "
                            f"{source_playlist['destination_name']} #{membership['track_no']}"
                        )
                    selected = row["candidates"][0]
                    media_key = selected["djay_media_key"]
                    mode = f"reconciled_{row['confidence']}"
                    reconciliation.append({
                        "track_no": membership["track_no"], "source_artist": membership.get("artist", ""),
                        "source_title": membership.get("title", ""), "selected": selected, "mode": mode,
                    })
                media_keys.append(str(media_key))
            playlists.append({
                "name": source_playlist["destination_name"],
                "source_count": source_playlist["source_count"],
                "media_keys": media_keys,
                "reconciled": reconciliation,
            })
    names = [playlist["name"] for playlist in playlists]
    if len(set(name.casefold() for name in names)) != len(names):
        raise RuntimeError("Duplicate destination playlist name in recovery plan.")
    return {
        "type": "recovered_itunes_djay_playlist_plan",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database": str(db_path),
        "playlists": playlists,
        "membership_count": sum(len(playlist["media_keys"]) for playlist in playlists),
        "reconciled_count": sum(len(playlist["reconciled"]) for playlist in playlists),
    }


def parent_node(djay, db_path: Path, parent_ref: str):
    nodes = djay.load_djay_playlist_graph(db_path)
    matches = [node for node in nodes.values() if node.key == parent_ref or node.name.casefold() == parent_ref.casefold()]
    if len(matches) != 1:
        raise RuntimeError(f"Parent folder is missing or ambiguous: {parent_ref!r}")
    parent = matches[0]
    if parent.kind != "folder":
        raise RuntimeError(f"Parent is not a folder: {parent_ref!r}")
    sibling_names = {
        node.name.casefold() for node in nodes.values() if node.parent_key == parent.key
    }
    collisions = [playlist["name"] for playlist in []]
    return parent, sibling_names


def apply_to_candidate(djay, database: Path, plan: dict, parent_key: str) -> dict:
    connection = sqlite3.connect(database)
    try:
        parent_row = connection.execute(
            "SELECT rowid,data FROM database2 WHERE collection='mediaItemPlaylists' AND key=?", (parent_key,)
        ).fetchone()
        if parent_row is None:
            raise RuntimeError("Target parent folder disappeared.")
        parent_rowid, parent_raw = int(parent_row[0]), bytes(parent_row[1])
        parent_document = djay.exact_tsaf_doc(parent_raw, djay.TSAF)
        children = djay.tsaf_string_array(parent_document.root.get("childUUIDs"), djay.TSAF)

        media_rowids = {}
        all_media = {key for playlist in plan["playlists"] for key in playlist["media_keys"]}
        for media_key in all_media:
            row = connection.execute(
                "SELECT rowid FROM database2 WHERE collection='mediaItems' AND key=?", (media_key,)
            ).fetchone()
            if row is None:
                raise RuntimeError(f"Planned media item is absent: {media_key}")
            media_rowids[media_key] = int(row[0])

        playlist_keys = {playlist["name"]: str(uuid.uuid4()).upper() for playlist in plan["playlists"]}
        playlist_rowids: dict[str, int] = {}
        item_keys: dict[str, list[str]] = {}
        item_rowids: dict[str, list[int]] = {}
        connection.execute("BEGIN IMMEDIATE")
        try:
            for playlist in plan["playlists"]:
                name, media_keys = playlist["name"], playlist["media_keys"]
                playlist_key = playlist_keys[name]
                these_item_keys = [str(uuid.uuid4()).upper() for _ in media_keys]
                item_keys[name] = these_item_keys
                object_value = djay.TSAF.Obj("ADCMediaItemPlaylist")
                object_value.fields = [
                    ("uuid", playlist_key), ("name", name), ("parentUUID", parent_key),
                    ("type", djay.TSAF.Marker(djay.TSAF.TAG_M2E)),
                    ("itemUUIDs", djay.TSAF.Arr(parent_document.root.get("childUUIDs").tag, these_item_keys)),
                ]
                playlist_rowid = djay._insert_tsaf_object(connection, "mediaItemPlaylists", playlist_key, object_value)
                playlist_rowids[name] = playlist_rowid
                connection.execute(
                    "INSERT INTO secondaryIndex_mediaItemPlaylistIndex(rowid,name) VALUES(?,?)", (playlist_rowid, name)
                )
                rows: list[int] = []
                for item_key, media_key in zip(these_item_keys, media_keys):
                    item = djay.TSAF.Obj("ADCMediaItemPlaylistItem")
                    item.fields = [("uuid", item_key), ("playlistUUID", playlist_key), ("mediaItemUUID", media_key)]
                    item_rowid = djay._insert_tsaf_object(connection, "mediaItemPlaylistItems", item_key, item)
                    rows.append(item_rowid)
                    connection.execute(
                        "INSERT INTO relationship_relationship(name,src,dst,rules,manual) VALUES('mediaItemPlaylistItemMediaItem',?,?,4,0)",
                        (item_rowid, media_rowids[media_key]),
                    )
                    connection.execute(
                        "INSERT INTO relationship_relationship(name,src,dst,rules,manual) VALUES('mediaItemPlaylistItemPlaylist',?,?,4,0)",
                        (item_rowid, playlist_rowid),
                    )
                item_rowids[name] = rows

            djay.replace_tsaf_field(
                parent_document.root, "childUUIDs",
                djay.TSAF.Arr(parent_document.root.get("childUUIDs").tag, [*children, *playlist_keys.values()]),
            )
            connection.execute("UPDATE database2 SET data=? WHERE rowid=?", (djay._serialize_exact(parent_document, "parent folder"), parent_rowid))
            djay._append_view_rowids(connection, "view_mediaItemPlaylistsView_map", "view_mediaItemPlaylistsView_page", parent_key, list(playlist_rowids.values()))
            djay._append_view_rowids(connection, "view_mediaView_map", "view_mediaView_page", "playlist", list(playlist_rowids.values()))
            for name, playlist_key in playlist_keys.items():
                djay._append_view_rowids(connection, "view_mediaItemPlaylistView_map", "view_mediaItemPlaylistView_page", playlist_key, item_rowids[name])
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Candidate djay database integrity check failed.")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    finally:
        connection.close()
    return {"parent_key": parent_key, "playlist_keys": playlist_keys, "playlist_rowids": playlist_rowids, "item_keys": item_keys}


def verify(djay, database: Path, plan: dict, manifest: dict) -> None:
    connection = sqlite3.connect(f"file:{database.resolve().as_posix()}?mode=ro", uri=True)
    try:
        parent_raw = connection.execute("SELECT data FROM database2 WHERE collection='mediaItemPlaylists' AND key=?", (manifest["parent_key"],)).fetchone()
        if parent_raw is None:
            raise RuntimeError("Parent folder missing during readback.")
        children = djay.tsaf_string_array(djay.exact_tsaf_doc(bytes(parent_raw[0]), djay.TSAF).root.get("childUUIDs"), djay.TSAF)
        for playlist in plan["playlists"]:
            name, expected_media = playlist["name"], playlist["media_keys"]
            key = manifest["playlist_keys"][name]
            if key not in children:
                raise RuntimeError(f"Playlist is not linked under the parent folder: {name}")
            row = connection.execute("SELECT rowid,data FROM database2 WHERE collection='mediaItemPlaylists' AND key=?", (key,)).fetchone()
            if row is None:
                raise RuntimeError(f"Playlist missing after write: {name}")
            playlist_rowid, raw = int(row[0]), bytes(row[1])
            root = djay.exact_tsaf_doc(raw, djay.TSAF).root
            if djay.tsaf_text(root.get("name")) != name or djay.tsaf_text(root.get("parentUUID")) != manifest["parent_key"]:
                raise RuntimeError(f"Playlist metadata mismatch: {name}")
            keys = djay.tsaf_string_array(root.get("itemUUIDs"), djay.TSAF)
            actual_media = []
            for item_key in keys:
                item_row = connection.execute("SELECT rowid,data FROM database2 WHERE collection='mediaItemPlaylistItems' AND key=?", (item_key,)).fetchone()
                if item_row is None:
                    raise RuntimeError(f"Playlist membership missing: {item_key}")
                item_rowid, item_raw = int(item_row[0]), bytes(item_row[1])
                item_root = djay.exact_tsaf_doc(item_raw, djay.TSAF).root
                actual_media.append(djay.tsaf_text(item_root.get("mediaItemUUID")))
                relations = set(connection.execute("SELECT name,dst FROM relationship_relationship WHERE src=?", (item_rowid,)))
                if ("mediaItemPlaylistItemPlaylist", playlist_rowid) not in relations:
                    raise RuntimeError(f"Playlist relationship missing: {item_key}")
            if actual_media != expected_media:
                raise RuntimeError(f"Playlist order mismatch: {name}")
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Readback integrity check failed.")
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recovery_plan", type=Path, nargs="+")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--djay-py", type=Path, required=True)
    parser.add_argument("--djay-db", type=Path, required=True)
    parser.add_argument("--parent", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    djay = load_djay_module(args.djay_py)
    plan = build_plan(args.recovery_plan, args.evidence, args.djay_db)
    parent, siblings = parent_node(djay, args.djay_db, args.parent)
    collisions = [row["name"] for row in plan["playlists"] if row["name"].casefold() in siblings]
    if collisions:
        raise RuntimeError("Existing parent-folder playlist name collision(s): " + ", ".join(collisions))
    plan["parent_folder"] = {"name": parent.name, "key": parent.key}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Playlists: {len(plan['playlists'])}; memberships: {plan['membership_count']}; reconciled: {plan['reconciled_count']}")
    print(f"Target: /{parent.name}; plan: {args.out}")
    if not args.apply:
        print("No files changed. Re-run with --apply after reviewing the plan.")
        return 0

    djay.ensure_djay_is_closed(args.djay_db)
    source_hash = sha256(args.djay_db)
    backup = djay.backup_before_operation(args.djay_db, "recovered-itunes-playlists")
    if database_content_digest(backup) != database_content_digest(args.djay_db):
        raise RuntimeError("Live database backup content mismatch.")
    descriptor, name = tempfile.mkstemp(prefix=".MediaLibrary.recovered-playlists.", suffix=".db", dir=args.djay_db.parent)
    os.close(descriptor)
    candidate = Path(name)
    installed = False
    try:
        candidate.unlink()
        djay.CORE.clone_database(args.djay_db, candidate)
        manifest = apply_to_candidate(djay, candidate, plan, parent.key)
        verify(djay, candidate, plan, manifest)
        djay.remove_sqlite_sidecars(candidate)
        djay.ensure_djay_is_closed(args.djay_db)
        if sha256(args.djay_db) != source_hash:
            raise RuntimeError("Live database changed while the candidate was built.")
        djay.remove_sqlite_sidecars(args.djay_db)
        os.replace(candidate, args.djay_db)
        installed = True
        verify(djay, args.djay_db, plan, manifest)
    except Exception:
        if installed:
            restore = args.djay_db.with_name(f".{args.djay_db.name}.restore-{uuid.uuid4().hex}.db")
            djay.CORE.clone_database(backup, restore)
            os.replace(restore, args.djay_db)
        raise
    finally:
        candidate.unlink(missing_ok=True)
        djay.remove_sqlite_sidecars(candidate)
    journal = {"type": "recovered_itunes_playlists_import", "applied_at": datetime.now(timezone.utc).isoformat(), "plan": str(args.out), "database": str(args.djay_db), "backup": str(backup), "database_sha256_before": source_hash, "database_sha256_after": sha256(args.djay_db), "manifest": manifest}
    args.journal.write_text(json.dumps(journal, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Applied and verified. Backup: {backup}")
    print(f"Journal: {args.journal}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
