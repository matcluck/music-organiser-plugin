# Command Workflow

Set paths explicitly for each batch:

```powershell
$project = (Resolve-Path ".").Path
$skill = $project
$python = (Get-Command python).Source
$inbox = "<path-to-inbox>"
$library = "<path-to-library>"
$runName = "$(Get-Date -Format yyyy-MM-dd)-new-import"
$work = "$project\artifacts\runs\$runName"
New-Item -ItemType Directory -Path $work -Force | Out-Null
```

Choose a unique, descriptive `$runName` for each batch. Never set `$work` to the drive root or an audio-library directory.

Run the dependency preflight and install only the selected scope when needed:

```powershell
& "$skill\scripts\dependency_preflight.ps1" -Mode metadata-provider
# Or: metadata-local / cues-local
# Add -Install only after the user selects and approves that scope.
```

## Manifest and metadata inference

Build the manifest first, then choose one inference path.

```powershell
& $python "$skill\scripts\build_music_manifest.py" $inbox `
  --out "$work\manifest.json" --overwrite

```

### Current-provider mode

Ask the active Claude or Codex provider to propose cleaned manifest values from
the preserved evidence. Save the proposal and provider/model identity in the
run directory, then run `postprocess_music_manifest.py` on that proposal. This
mode needs no local model server.

### Local mode

```powershell
& $python "$skill\scripts\populate_music_manifest.py" "$work\manifest.json" `
  --out "$work\manifest-llm.json" `
  --endpoint http://127.0.0.1:8080/v1 `
  --endpoint http://127.0.0.1:8081/v1

& $python "$skill\scripts\postprocess_music_manifest.py" "$work\manifest-llm.json" `
  --out "$work\manifest-final.json" --overwrite
```

## Required quality audit and targeted feedback pass

After the initial cleanup is complete, audit every final metadata row with an
independent pass in the selected inference mode. The commands below are the
local-mode path. They flag only concrete, evidence-supported mistakes and do
not fill in optional missing facts.

```powershell
& $python "$skill\scripts\audit_music_manifest_parallel.py" "$work\manifest-final.json" `
  --out "$work\metadata-audit-raw.json" `
  --endpoint http://127.0.0.1:8080/v1 `
  --endpoint http://127.0.0.1:8081/v1

& $python "$skill\scripts\sanitize_music_audit.py" `
  "$work\manifest-final.json" "$work\metadata-audit-raw.json" `
  --out "$work\metadata-audit.json"

& $python "$skill\scripts\populate_music_manifest.py" "$work\manifest-final.json" `
  --out "$work\manifest-audited.json" `
  --audit-feedback "$work\metadata-audit.json" `
  --endpoint http://127.0.0.1:8080/v1 `
  --endpoint http://127.0.0.1:8081/v1

& $python "$skill\scripts\postprocess_music_manifest.py" "$work\manifest-audited.json" `
  --out "$work\manifest-ready.json" --overwrite
```

For an opt-in local trace of the exact model requests and responses, add
`--trace-dir "$work\llm-trace"` to the population command. Start the
local-only auto-refresh monitor with:

```powershell
& $python "$skill\scripts\serve_music_llm_monitor.py" `
  --runs-root "$project\artifacts\runs"
```

The monitor is only reachable from this computer at `http://127.0.0.1:8090`.

Resume the same populate command after interruption. Add `--reprocess-suspicious` to revisit completed rows containing deterministic warning signs. Use `--reprocess-reviewed` for rows with a review reason.

Population and audit commands reserve their endpoint set for the life of the
process. Do not launch a second music LLM job against the same single-slot
servers; it will fail closed instead of silently sharing them.

Start local inference with the llama.cpp installation and model paths selected
during dependency setup. The parallel second-pass audit defaults to 10 tracks
per request and automatically splits an unusually large evidence set.

## Review and Plan

For repeated batches, keep a reusable library index and use the deterministic
preparation command. Every run performs a cheap filesystem reconciliation; a
full tag verification triggers every seven days by default:

```powershell
& $python "$skill\scripts\music_library_index.py" $library `
  --db "$project\artifacts\runs\<run-id>\library-index.sqlite"

& $python "$skill\scripts\prepare_music_import.py" `
  "$work\manifest-final.json" "$work\staging" $library $work `
  --index "$project\artifacts\runs\<run-id>\library-index.sqlite"
```

The preparation command is dry-run-first. Add `--resolutions` with an explicit
evidence-backed resolution JSON when needed. Add `--apply` only to create tagged,
organized copies under the run's `import-ready-audio` folder. Use
`--force-index-verify` after an out-of-band tagger may have preserved file size
and modification time.

```powershell
& $python "$skill\scripts\build_music_review_csv.py" "$work\manifest-final.json" $library `
  --out "$work\review.csv"

& $python "$skill\scripts\build_music_output_csv.py" "$work\manifest-final.json" $library `
  --out "$work\plan.csv"

& $python "$skill\scripts\audit_import_plan.py" "$work\plan.csv" $library `
  --out "$work\collisions.csv"
```

Convert only evidence-backed existing-library matches into explicit skips, then
re-audit the resolved plan. Any unresolved path collision fails closed:

```powershell
& $python "$skill\scripts\apply_import_plan_audit.py" `
  "$work\plan.csv" "$work\collisions.csv" `
  --out "$work\plan-resolved.csv" `
  --skip-report "$work\existing-library-skips.csv"

& $python "$skill\scripts\audit_import_plan.py" `
  "$work\plan-resolved.csv" $library `
  --out "$work\plan-resolved-audit.csv"
```

Resolve every non-`skip_existing` collision before applying. If output metadata
changes, regenerate the CSV; do not hand-edit a locked plan.

For several staged collections, resolve identity and destination duplicates
across their already library-resolved plans, then prove no active cross-plan
duplicates remain:

```powershell
& $python "$skill\scripts\resolve_import_plan_bundle.py" `
  $plan1 $plan2 $plan3 `
  --out-suffix=-handoff `
  --report "$work\cross-run-skips.csv"

& $python "$skill\scripts\resolve_import_plan_bundle.py" `
  $handoffPlan1 $handoffPlan2 $handoffPlan3 `
  --verify-only --report "$work\cross-run-verification.csv"
```

When only a few manifest rows changed after a full audit, seed the final audit
from exact unchanged rows. The audit command then rechecks only changed indexes:

```powershell
& $python "$skill\scripts\seed_music_audit.py" `
  "$work\manifest-before.json" "$work\manifest-after.json" `
  "$work\audit-before.json" --out "$work\audit-final-raw.json"
```

## Apply

Dry-run first:

```powershell
& $python "$skill\scripts\apply_music_plan.py" `
  "$work\manifest-final.json" "$work\plan.csv" $inbox $library `
  --action move --journal "$work\apply.jsonl" `
  --skip-evidence "$work\existing-library-skips.csv" `
  --skip-evidence "$work\cross-run-skips.csv"
```

Apply only after the dry run and review succeed:

```powershell
& $python "$skill\scripts\apply_music_plan.py" `
  "$work\manifest-final.json" "$work\plan.csv" $inbox $library `
  --action move --journal "$work\apply.jsonl" --apply
```

After interruption, run the identical command with `--resume`. Use `--action copy` instead when source retention is required.

## Playlist and Audits

```powershell
& $python "$skill\scripts\build_import_playlist.py" "$work\plan.csv" `
  --out "$work\import.m3u8"

& $python "$skill\scripts\audit_short_audio.py" $library `
  --seconds 30 --out "$work\short-audio.csv"
```

Verify every playlist path exists before importing it into iTunes.
