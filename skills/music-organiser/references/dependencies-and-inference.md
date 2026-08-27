# Dependencies and inference modes

Run dependency checks before choosing an inference path. Install only the missing scope after the user selects a mode.

Use the deterministic preflight:

```powershell
.\scripts\dependency_preflight.ps1 -Mode metadata-provider
.\scripts\dependency_preflight.ps1 -Mode metadata-local
.\scripts\dependency_preflight.ps1 -Mode cues-local
```

These commands are read-only. After the user chooses and approves the scoped setup, add `-Install`. The script installs base Python or locked cue-project dependencies but deliberately refuses to download llama.cpp binaries or model weights automatically.

Portable configuration variables are `MUSIC_CUE_ENGINE_ROOT`, `MUSIC_LLAMA_ROOT`, `MUSIC_LLAMA_RUNTIME`, and `MUSIC_METADATA_MODEL`. Keep their machine-specific values outside the repository.

## Base organiser

Use a project-local Python environment and the pinned repository requirements. Do not install packages globally.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

If the environment already exists, verify it by importing the required packages and showing the maintained command help before reinstalling.

## Metadata inference choice

Offer these modes explicitly:

### Current provider

Use the active Claude or Codex model to analyze manifest evidence. This is the simplest option and needs no local model server. It is best for modest batches, interactive review, or when local hardware is unavailable. Record the provider/model and keep its proposal separate from deterministic postprocessing.

Do not send audio itself. Show the user what manifest evidence will be submitted when it may contain private filenames or embedded tags.

### Local llama.cpp

Use the maintained local endpoint workflow for private, resumable, or large-batch metadata cleanup. Before offering installation or startup, check:

- `nvidia-smi` and current compute processes;
- the configured llama.cpp launcher;
- the configured llama.cpp server executable;
- the configured GGUF model path;
- ports 8080 and 8081;
- existing model-server health.

If hardware or assets are missing, report the exact gap and offer either the current-provider mode or a scoped local setup. Do not download a model, GPU runtime, or llama.cpp build without approval. Do not change driver power or clock settings.

## Cue analysis

Cue analysis is a separate local-only flow. Check the configured local cue engine, its locked environment, model weights, audio decoders, RAM/VRAM, and active GPU jobs. The engine belongs to Music Organiser's runtime and must not be discovered through a djay installation or library. Its proposal output belongs to `music-organiser`, not to a platform database.

When dependencies are absent, offer a scoped install into the cue engine's locked project environment. Follow the repository supply-chain review rule before executing newly fetched third-party code or model artifacts. Verify a proposal-only run on one bounded track before a batch.
