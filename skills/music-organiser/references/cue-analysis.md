# Cue-point analysis

Cue analysis consumes audio and emits a destination-neutral proposal. It must not write djay or Rekordbox during analysis.

## Required proposal evidence

Store one record per track with:

- schema version and operation ID;
- canonical audio path, size, duration, and SHA-256;
- engine, code revision, model revision, and inference device;
- detected BPM and grid or downbeat evidence;
- zero to eight semantic cue slots with label and millisecond position;
- warnings, analysis failure, and review status.

Keep proposals and journals under the run artifact directory. Never store them in the live audio library.

## Execution gates

1. Run the dependency and hardware preflight.
2. Check current GPU jobs and preserve them.
3. Run one proposal-only track and validate decoded duration, grid fit, cue bounds, slot uniqueness, and deterministic serialization.
4. Run the batch resumably. Proposal workers may run in parallel; platform database publication must remain a separate validated stage.
5. Review representative waveforms and every warning or failure before publication.

Generate the neutral artifact with:

```powershell
uv run python scripts/generate_cue_proposals.py <audio-or-folder> --out <proposal.json> --cue-engine-root <cue-engine-root>
```

The generator binds every record to the source size and SHA-256 and never opens a djay or Rekordbox database. The cue engine is an organiser dependency, resolved from `MUSIC_CUE_ENGINE_ROOT` or the ignored project-local `.runtime/cue-engine`; it is not resolved from a djay workspace. Review the artifact before loading a destination skill.
