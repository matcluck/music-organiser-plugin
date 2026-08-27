---
name: music-organiser
description: Safely inventory, identify, clean, deduplicate, retag, structure, and verify local DJ music libraries; run metadata inference; generate destination-neutral cue-point proposals; and orchestrate publication to djay, Rekordbox, or both. Use for new music inboxes, inconsistent tags, local-model or current-provider metadata analysis, cue analysis, organized-library imports, DJ samples, M3U8 playlists, and library audits. Load the dedicated platform skill before any djay or Rekordbox write.
---

# Music Organiser

Organize music through an evidence-first, reviewable pipeline. Preserve source audio and artwork until a verified plan is explicitly applied.

## Start Here

1. Read [references/library-policy.md](references/library-policy.md).
2. Read [references/dependencies-and-inference.md](references/dependencies-and-inference.md), run the dependency preflight, and let the user choose the metadata inference mode before installing optional components.
3. Read [references/commands.md](references/commands.md) before running a complete import.
4. Inspect the input and destination roots. Never assume paths from an earlier run.
5. Keep all manifests, plans, review files, and journals outside the audio roots.
6. Keep each run under `<project>\artifacts\runs\<YYYY-MM-DD>-<short-name>`; never write organiser output into the audio-library root.

## Tool Layout

- Use the maintained commands in `scripts/` for all current work.
- Use `scripts/dependency_preflight.ps1` for repeatable dependency checks and approved scoped setup.
- Historical migration and repair utilities do not ship in the public plugin. Keep any local legacy material under the repository's ignored `legacy/` directory and never run it by default.

## Import Workflow

1. **Inventory**
   - Run `scripts/build_music_manifest.py` against the inbox.
   - Preserve every original tag as evidence. Represent binary values by existence and byte length only.
   - Treat read failures and zero-byte files as review items, not music.

2. **Choose and run metadata inference**
   - Offer `current-provider` as the simple mode: use the active Claude or Codex provider to propose metadata from the manifest evidence, preserve the provider/model in the run journal, and pass the result through the same deterministic postprocessing and review gates.
   - Offer `local` for private, resumable, or large-batch work when compatible local hardware and llama.cpp assets are available.
   - In local mode, run `scripts/populate_music_manifest.py` against one or more local llama.cpp endpoints.
   - Treat the endpoint set as exclusive to one music job. The script reserves it and fails closed rather than sharing single-slot servers; do not use `--allow-shared-endpoints` in normal runs.
   - Resume by default. Use `--restart` only when intentionally replacing prior output.
   - Run `scripts/postprocess_music_manifest.py` after the LLM pass.
   - Reject output containing DJ keys, BPM suffixes, download labels, domains, handles, mojibake, field leakage, malformed brackets, or suspicious casing.

3. **Audit the completed cleanup**
   - Use an independent pass with the selected inference mode after deterministic postprocessing. Preserve both the original proposal and audit evidence.
   - In local mode, run `scripts/audit_music_manifest_parallel.py` when both endpoints are available (use `audit_music_manifest.py` for a single endpoint).
   - The parallel audit schedules batches across both endpoints and records one complete review row per track. Derive a conservative feedback file with `scripts/sanitize_music_audit.py`; never feed unsanitized verifier output directly into population.
   - Preserve both raw and conservative per-track feedback JSON in the run directory. Re-run only retained flagged tracks through `populate_music_manifest.py --audit-feedback`, then postprocess that revised output again. Deterministic optional-field corrections are applied locally without a redundant model request.
   - The audit may flag a concrete missed cleanup or field mistake, but must not invent missing release facts. Retain unresolved identity as review items.
   - Use `scripts/serve_music_llm_monitor.py` for a local-only live view of source evidence, model progress, and current results. Opt into exact request/response trace files with `populate_music_manifest.py --trace-dir`; traces can contain original embedded tags.

4. **Resolve uncertainty**
   - Run `scripts/build_music_review_csv.py` and inspect the focused review rows.
   - Use `shazam-categorize-music` when it is available and the user wants acoustic identification of missing or suspicious artist/title identity.
   - Treat acoustic recognition as evidence. Preserve explicit remix, bootleg, VIP, mashup, acapella, and edit context from credible filenames or tags.
   - Leave `needsReview = true` when evidence is insufficient. Never invent an album or artist.

5. **Build and audit the plan**
   - Run `scripts/build_music_output_csv.py`.
   - Inspect every blank path, destination collision, and duplicate skip.
   - Refresh `scripts/music_library_index.py` and run `scripts/audit_import_plan.py`
     with `--index` so unchanged library tags are not reread for every batch.
   - Keep the default seven-day full-verification trigger. Every refresh still
     reconciles paths, sizes, and modification times; use `--force-index-verify`
     when an out-of-band tagger may have preserved those filesystem attributes.
   - When the same normalized artist/title already exists, keep the existing library file unless the user explicitly asks to compare versions or quality.

6. **Apply safely**
   - Run `scripts/apply_music_plan.py` without `--apply` first.
   - Use `--action copy` when source retention is desired.
   - Use `--action move` only when the user wants a consumed inbox. The script publishes and verifies the destination before removing that source.
   - Preserve the journal and use `--resume` after interruption. Do not edit a locked manifest or plan mid-run.

7. **Verify and hand off**
   - Confirm every planned destination exists, every moved source is absent, no `.__codex_*` files remain, and metadata/artwork verification passed.
   - Remove empty directories only after resolving and validating their absolute paths inside the intended root.
   - Run `scripts/build_import_playlist.py` after a successful apply. Verify every M3U8 path before telling the user to import it.
   - Read [references/destination-routing.md](references/destination-routing.md), record `djay`, `rekordbox`, `both`, or `none`, then load the corresponding platform skill before any platform access.

## Cue-point analysis

- Keep cue analysis separate from metadata inference. It always uses the local audio/ML stack; the current text provider is not a substitute.
- Read [references/cue-analysis.md](references/cue-analysis.md) before running or changing the cue engine.
- Analyze final canonical audio paths and write proposal evidence outside the audio library. Do not write djay or Rekordbox databases during proposal generation.
- Record source path, full audio hash, size, duration, engine/model revision, BPM/grid evidence, semantic slot, label, and millisecond position so either publisher can validate the same proposal.
- Inspect active GPU work first. Never stop an existing job, change power or clock limits, or change the approved GPU mode without fresh approval.
- Generate the neutral artifact with `scripts/generate_cue_proposals.py`. After review, load `djay-skill`, `rekordbox-skill`, or both to publish the approved proposals independently.

## Destination routing

- `djay`: load `djay-skill`; stop after verified djay publication.
- `rekordbox`: load `rekordbox-skill`; do not register tracks in djay as an incidental cue-transfer step.
- `both`: generate one proposal artifact, then run and verify each publisher separately.
- `none`: finish after organization, metadata, and optional cue proposals.

Never let one platform skill mutate the other platform implicitly.

## Existing Libraries

- Use `scripts/audit_short_audio.py` to find suspiciously short audio. Do not classify every short track as a sample; preserve legitimate intros, skits, and interludes.
- Keep DJ tools under `DJ Samples/<Category>` with `albumArtist = DJ Samples` and `genre = DJ Tools`. Useful categories include `Vocal Drops`, `Effects`, `Extracted Clips`, and `Acapellas`.
- Keep album releases in album folders. Keep genuinely albumless tracks at the artist root.
- Prefer AIFF over WAV when lossless PCM metadata must remain visible in iTunes. Verify decoded audio before removing the WAV.
- Regenerate playlists after any path correction.

## Safety Rules

- Never rewrite or move audio directly from raw LLM or Shazam output.
- Never write, retag, rename, move, delete, synchronize, or otherwise alter a source USB. Stage copies and work only from those copies.
- Never delete an input merely because recognition failed.
- Never overwrite an existing destination.
- Never remove lower-quality or suffixed duplicates until a retained copy is verified.
- Never claim the library is clean from filename inspection alone; inspect metadata and duration.
- Keep source and output paths separate during copy imports.
- Use native PowerShell path operations end to end on Windows and validate recursive move/delete targets.

## Invocation

- Installed from this marketplace in Codex: `$music-organiser:music-organiser`
- Installed from this marketplace in Claude Code: `/music-organiser:music-organiser`
- Standalone skill installations may expose the shorter `$music-organiser` or `/music-organiser` form.

The same `SKILL.md` supplies both interfaces; do not maintain a duplicate command body.

## Workspace Hygiene

- Inspect the exact plan before using `-Apply`. Any legacy relocation helper must use an explicit reviewed allowlist, refuse overwrites, hash every file, and record a relocation manifest. Keep private artifact names in ignored workspace documentation.
- Preserve relocated journals and manifests as historical evidence. Do not rewrite their embedded original paths.

## Bundled Tools

- `build_music_manifest.py`: source and original-tag evidence manifest.
- `populate_music_manifest.py`: resumable multi-endpoint local-LLM cleanup.
- `postprocess_music_manifest.py`: deterministic sanitation and review flags.
- `audit_music_manifest.py`: required second-pass local-model audit with per-track revision feedback.
- `audit_music_manifest_parallel.py`: compact resumable dual-endpoint second-pass audit.
- `sanitize_music_audit.py`: conservative derived audit that rejects verifier noise and no-op feedback.
- `seed_music_audit.py`: exact manifest-diff audit checkpoint reuse so final verification rechecks only changed rows.
- `score_music_model_benchmark.py`: compare a candidate manifest with a reviewed reference.
- `serve_music_llm_monitor.py`: local-only auto-refresh monitor for model input evidence and output progress.
- `dependency_preflight.ps1`: read-only mode checks and opt-in scoped dependency setup for provider metadata, local metadata, and local cue analysis.
- `generate_cue_proposals.py`: local ML cue analysis into a destination-neutral, hash-bound JSON proposal; it never writes a DJ application database.
- `build_music_review_csv.py`: focused unresolved/suspicious review.
- `apply_music_review_resolutions.py`: strict evidence-backed resolution of reviewed manifest rows.
- `prepare_music_import.py`: one-command review gate, indexed collision audit, dry run, and optional prepared copy.
- `build_music_output_csv.py`: deterministic destination and metadata plan.
- `music_library_index.py`: incremental SQLite identity index for fast library checks.
- `audit_import_plan.py`: existing-library identity and path collisions.
- `apply_import_plan_audit.py`: fail-closed conversion of audited `skip_existing` collisions into explicit plan skips, with optional skip-evidence CSV.
- `resolve_import_plan_bundle.py`: quality-aware cross-run identity and destination deduplication with per-skip evidence.
- `apply_music_plan.py`: strict metadata/artwork verification and transactional copy/move.
- `remove_duplicate_skips.py`: remove verified skipped duplicate sources.
- `build_import_playlist.py`: verified M3U8 generation.
- `audit_short_audio.py`: read-only short-track inventory.
- `start_music_llama_servers.ps1`: this machine's dual-GPU launcher; it preserves the driver's existing power and clock settings.
- `relocate_root_artifacts.ps1`: dry-run-first, hash-verified relocation of known organiser artifacts from the drive root.
- `reconcile_filename_renames.py`: exceptional cross-target transaction for one approved audio rename across djay and Rekordbox; it is not a general platform adapter.
