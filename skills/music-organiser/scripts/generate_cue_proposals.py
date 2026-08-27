#!/usr/bin/env python3
"""Generate destination-neutral cue proposals with the local autohotcue engine."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
from pathlib import Path


AUDIO_EXTENSIONS = {".aac", ".aif", ".aiff", ".flac", ".m4a", ".mp3", ".oga", ".ogg", ".opus", ".wav", ".wma"}


def audio_paths(value: Path) -> list[Path]:
    if value.is_file():
        return [value.resolve()]
    if not value.is_dir():
        raise FileNotFoundError(value)
    return sorted(path.resolve() for path in value.rglob("*") if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS and not any(part.startswith(".") for part in path.relative_to(value).parts))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_revision(workspace: Path) -> str | None:
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=workspace, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def source_tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def serialize_grid(grid) -> dict | None:
    if grid is None:
        return None
    return {
        "ok": bool(grid.ok),
        "bpm": float(grid.bpm),
        "render_bpm": float(grid.render_bpm),
        "anchor_seconds": float(grid.anchor_s),
        "beat_fit": float(grid.beat_fit),
        "reason": str(grid.reason or ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    default_engine_root = Path(os.environ.get("MUSIC_CUE_ENGINE_ROOT") or Path(__file__).resolve().parents[1] / ".runtime" / "cue-engine")
    parser.add_argument("--cue-engine-root", type=Path, default=default_engine_root)
    parser.add_argument("--engine", default="ml-bass", choices=("ml-bass", "ml", "ml-librosa", "ml-allin1", "ml-songformer", "legacy"))
    parser.add_argument("--device", default="auto")
    parser.add_argument("--operation-id")
    args = parser.parse_args()

    engine_root = args.cue_engine_root.resolve()
    source_root = engine_root / "src"
    if not source_root.is_dir():
        raise RuntimeError(f"autohotcue source is missing from the configured cue workspace: {source_root}")
    sys.path.insert(0, str(source_root))
    from autohotcue import analysis, backends, djaydb

    paths = audio_paths(args.path)
    if not paths:
        raise RuntimeError("No supported audio files were found.")
    backends.init_worker(1)
    tracks = []
    for path in paths:
        digest = sha256(path)
        try:
            track, proposal = analysis.analyze(str(path), engine=args.engine, device=args.device, jobs=1)
            cues = [
                {
                    "slot": ord(slot) - ord("A"),
                    "label": djaydb.CUE_LABELS[ord(slot) - ord("A")],
                    "position_ms": max(0, int(round(float(position) * 1000))),
                }
                for slot, position in sorted(proposal.positions.items())
                if position is not None
            ]
            tracks.append({
                "path": str(path), "bytes": path.stat().st_size, "sha256": digest,
                "duration_ms": int(round(float(track.duration_s) * 1000)),
                "bpm": float(track.bpm), "first_beat_ms": int(round(float(track.first_beat_s) * 1000)),
                "grid": serialize_grid(track.grid_fit), "cues": cues,
                "warnings": [str(note) for note in proposal.notes], "review_status": "pending",
            })
        except Exception as exc:
            tracks.append({"path": str(path), "bytes": path.stat().st_size, "sha256": digest, "analysis_failure": str(exc), "review_status": "failed"})

    operation_id = args.operation_id or "cue-" + hashlib.sha256("\n".join(item["sha256"] for item in tracks).encode()).hexdigest()[:16]
    payload = {
        "schema": "music-organiser.cue-proposal/v1",
        "operation_id": operation_id,
        "engine": {"name": "djay-pro-autohotcue", "analysis_engine": args.engine, "code_revision": git_revision(engine_root), "source_tree_sha256": source_tree_sha256(source_root), "device": args.device, "beat_this_version": importlib.metadata.version("beat_this")},
        "tracks": tracks,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    failures = sum("analysis_failure" in track for track in tracks)
    print(f"Cue proposals: {len(tracks) - failures} succeeded, {failures} failed -> {args.out.resolve()}")
    return 2 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
