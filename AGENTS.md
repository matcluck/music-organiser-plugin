# Music Organiser repository

## Scope

- Treat the directory containing this file as the project root.
- Resolve input, music-library, review, artifact, model, and application paths at runtime. Never embed a contributor's drive letters, user-profile path, hostnames, or private network addresses.
- Store generated evidence under `artifacts/runs/<YYYY-MM-DD>-<short-name>` and keep it out of the audio library.
- Keep personal libraries, databases, caches, model weights, and run evidence outside Git.

## Required workflow

- Read `skills/music-organiser/SKILL.md` before music-library work.
- Use `music-organiser` for orchestration, metadata, organization, and destination-neutral cue proposals.
- Load `djay-skill` before djay access and `rekordbox-skill` before Rekordbox or Pioneer-device access.
- Default to inventory, preview, or dry-run. Do not retag, overwrite, move, delete, or publish without the operation's explicit approval.
- Preserve source evidence and validate exact absolute targets before recursive moves or removals.

## Project maintenance

- Keep maintained deterministic operations in `skills/music-organiser/scripts/`; do not add machine-specific one-off repairs to the public skill.
- Keep historical local material under the ignored root `legacy/` directory.
- Use only reviewed CC0 audio under `skills/music-organiser/tests/fixtures/audio/`; record source URLs, licenses, and SHA-256 values in its provenance file.
- Run `python -m unittest discover -s tests -p "test_*.py"` with `PYTHONPATH=scripts` from `skills/music-organiser` after changing Python workflow code.
- Keep `.claude-plugin/plugin.json` and `.codex-plugin/plugin.json` valid and free of machine-specific data.
- Run the public-safety scan and validate skill metadata with the bundled `skill-creator` validator.
- Do not commit unless the user explicitly asks.
