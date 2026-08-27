#!/usr/bin/env python3
"""Build and query an incremental SQLite index of a read-only music library."""

from __future__ import annotations

import argparse
import re
import sqlite3
import time
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from mutagen import File as MutagenFile

from build_music_manifest import AUDIO_EXTENSIONS


SCHEMA_VERSION = "1"


def normalize_identity(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").casefold()).strip()


def tag_value(tags: object, *keys: str) -> str:
    if not tags:
        return ""
    for key in keys:
        try:
            value = tags.get(key)
        except AttributeError:
            return ""
        if not value:
            continue
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        value = getattr(value, "text", value)
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        return str(value)
    return ""


def read_audio_identity(path: Path) -> tuple[str, str, str | None]:
    try:
        audio = MutagenFile(path)
        tags = audio.tags if audio else None
        return (
            tag_value(tags, "artist", "TPE1", "\xa9ART"),
            tag_value(tags, "title", "TIT2", "\xa9nam"),
            None,
        )
    except Exception as exc:  # pragma: no cover - codec-specific defensive boundary
        return "", "", f"{type(exc).__name__}: {exc}"


def connect_index(database: Path) -> sqlite3.Connection:
    database.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database, timeout=30)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS files (
            path TEXT PRIMARY KEY COLLATE NOCASE,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            artist TEXT NOT NULL,
            title TEXT NOT NULL,
            normalized_artist TEXT NOT NULL,
            normalized_title TEXT NOT NULL,
            error TEXT,
            indexed_at_ns INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS files_identity
            ON files(normalized_artist, normalized_title);
        """
    )
    integrity = connection.execute("PRAGMA quick_check").fetchone()
    if not integrity or integrity[0] != "ok":
        connection.close()
        raise ValueError(f"SQLite integrity check failed for library index: {database}")
    return connection


def _metadata(connection: sqlite3.Connection) -> dict[str, str]:
    return dict(connection.execute("SELECT key, value FROM metadata"))


def validate_index_root(connection: sqlite3.Connection, library: Path) -> None:
    metadata = _metadata(connection)
    schema = metadata.get("schema_version")
    stored_root = metadata.get("library_root")
    if schema is not None and schema != SCHEMA_VERSION:
        raise ValueError(
            f"Index schema {schema} is incompatible with expected {SCHEMA_VERSION}."
        )
    if stored_root is not None and stored_root.casefold() != str(library).casefold():
        raise ValueError(
            f"Index belongs to a different library: {stored_root} (requested {library})"
        )


@dataclass(frozen=True)
class RefreshStats:
    scanned: int
    reused: int
    reindexed: int
    removed: int
    unreadable: int
    drifted: int
    verification_trigger: str | None


def refresh_library_index(
    database: Path,
    library: Path,
    *,
    identity_reader: Callable[[Path], tuple[str, str, str | None]] = read_audio_identity,
    verify_after_days: float = 7.0,
    force_verify: bool = False,
    now_ns: int | None = None,
) -> RefreshStats:
    library = library.expanduser().resolve()
    if not library.is_dir():
        raise ValueError(f"Music library does not exist: {library}")
    database = database.expanduser().resolve()
    with closing(connect_index(database)) as connection:
        validate_index_root(connection, library)
        metadata = _metadata(connection)
        now_ns = time.time_ns() if now_ns is None else now_ns
        last_full_verify_ns = int(metadata.get("last_full_verify_ns", "0"))
        verify_interval_ns = max(0, int(verify_after_days * 86_400 * 1_000_000_000))
        if force_verify:
            verification_trigger = "forced"
        elif not last_full_verify_ns:
            verification_trigger = "new index"
        elif now_ns - last_full_verify_ns >= verify_interval_ns:
            verification_trigger = f"age >= {verify_after_days:g} days"
        else:
            verification_trigger = None
        existing = {
            str(path).casefold(): (
                str(path), int(size), int(mtime_ns), artist, title, error
            )
            for path, size, mtime_ns, artist, title, error in connection.execute(
                "SELECT path, size, mtime_ns, artist, title, error FROM files"
            )
        }
        seen: set[str] = set()
        scanned = reused = reindexed = unreadable = drifted = 0
        indexed_at_ns = now_ns
        connection.execute("BEGIN IMMEDIATE")
        try:
            for path in library.rglob("*"):
                if not path.is_file() or path.suffix.casefold() not in AUDIO_EXTENSIONS:
                    continue
                scanned += 1
                resolved = path.resolve()
                path_text = str(resolved)
                key = path_text.casefold()
                seen.add(key)
                stat = resolved.stat()
                cached = existing.get(key)
                stat_unchanged = bool(
                    cached and cached[1:3] == (stat.st_size, stat.st_mtime_ns)
                )
                if stat_unchanged and verification_trigger is None:
                    reused += 1
                    continue
                artist, title, error = identity_reader(resolved)
                if error:
                    unreadable += 1
                if (
                    stat_unchanged
                    and cached is not None
                    and cached[3:] != (artist, title, error)
                ):
                    drifted += 1
                connection.execute(
                    """
                    INSERT INTO files(
                        path, size, mtime_ns, artist, title,
                        normalized_artist, normalized_title, error, indexed_at_ns
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(path) DO UPDATE SET
                        size=excluded.size,
                        mtime_ns=excluded.mtime_ns,
                        artist=excluded.artist,
                        title=excluded.title,
                        normalized_artist=excluded.normalized_artist,
                        normalized_title=excluded.normalized_title,
                        error=excluded.error,
                        indexed_at_ns=excluded.indexed_at_ns
                    """,
                    (
                        path_text,
                        stat.st_size,
                        stat.st_mtime_ns,
                        artist,
                        title,
                        normalize_identity(artist),
                        normalize_identity(title),
                        error,
                        indexed_at_ns,
                    ),
                )
                reindexed += 1
            removed_paths = [stored[0] for key, stored in existing.items() if key not in seen]
            connection.executemany("DELETE FROM files WHERE path = ?", ((p,) for p in removed_paths))
            connection.executemany(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
                (
                    ("schema_version", SCHEMA_VERSION),
                    ("library_root", str(library)),
                    ("refreshed_at_ns", str(indexed_at_ns)),
                    *(
                        (("last_full_verify_ns", str(indexed_at_ns)),)
                        if verification_trigger is not None
                        else ()
                    ),
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        total_unreadable = connection.execute(
            "SELECT COUNT(*) FROM files WHERE error IS NOT NULL"
        ).fetchone()[0]
    return RefreshStats(
        scanned,
        reused,
        reindexed,
        len(removed_paths),
        total_unreadable,
        drifted,
        verification_trigger,
    )


def load_library_identities(
    database: Path, library: Path
) -> tuple[dict[tuple[str, str], list[str]], int, int]:
    library = library.expanduser().resolve()
    database = database.expanduser().resolve()
    if not database.is_file():
        raise ValueError(f"Library index does not exist: {database}")
    with closing(connect_index(database)) as connection:
        validate_index_root(connection, library)
        identities: dict[tuple[str, str], list[str]] = {}
        for artist, title, path in connection.execute(
            """
            SELECT normalized_artist, normalized_title, path
            FROM files
            WHERE normalized_artist <> '' AND normalized_title <> ''
            ORDER BY path COLLATE NOCASE
            """
        ):
            identities.setdefault((artist, title), []).append(path)
        scanned, unreadable = connection.execute(
            "SELECT COUNT(*), SUM(CASE WHEN error IS NOT NULL THEN 1 ELSE 0 END) FROM files"
        ).fetchone()
    return identities, int(scanned or 0), int(unreadable or 0)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("library", type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument(
        "--verify-after-days",
        type=float,
        default=7.0,
        help="Force a complete tag verification when the index is this old. Default: 7.",
    )
    parser.add_argument(
        "--force-verify",
        action="store_true",
        help="Reread every audio tag now, even when path metadata is unchanged.",
    )
    args = parser.parse_args()
    stats = refresh_library_index(
        args.db,
        args.library,
        verify_after_days=args.verify_after_days,
        force_verify=args.force_verify,
    )
    print(f"Library files seen: {stats.scanned:,}")
    print(f"Cached unchanged:   {stats.reused:,}")
    print(f"Metadata reread:    {stats.reindexed:,}")
    print(f"Removed from index: {stats.removed:,}")
    print(f"Unreadable indexed: {stats.unreadable:,}")
    print(f"Identity drift:     {stats.drifted:,}")
    print(
        "Full verification: "
        + (stats.verification_trigger or "not due; stat reconciliation only")
    )
    print(f"Index:              {args.db.expanduser().resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
