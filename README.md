# Music Organiser plugin

This repository contains the dual Claude Code and Codex `music-organiser` plugin. It inventories and organises local DJ libraries, offers current-provider or local metadata inference, produces destination-neutral cue proposals, and routes approved publication to independent platform plugins.

Rekordbox does not require djay. Music Organiser owns cue analysis, Rekordbox owns Rekordbox publication, and djay is loaded only when the user explicitly selects a djay workflow or imports existing djay cues.

## Install from the DJ Tools marketplace

### Claude Code

```text
/plugin marketplace add matcluck/dj-tools-marketplace
/plugin install music-organiser@dj-tools
```

Invoke the installed plugin workflow with:

```text
/music-organiser:music-organiser
```

### Codex

```text
codex plugin marketplace add matcluck/dj-tools-marketplace
codex plugin add music-organiser@dj-tools
```

Invoke the installed plugin workflow with:

```text
$music-organiser:music-organiser
```

The catalogue is maintained at [`matcluck/dj-tools-marketplace`](https://github.com/matcluck/dj-tools-marketplace). Both runtimes install this repository as the plugin source.

## Local plugin validation

From the repository root:

```powershell
claude plugin validate .
$env:PYTHONPATH = '.\skills\music-organiser\scripts'
python -m unittest discover -s '.\skills\music-organiser\tests' -p 'test_*.py'
```

Repository maintainers should also run the bundled Codex plugin and workflow-metadata validators available in their development environment; the README does not assume a particular Codex installation directory.

The plugin's callable workflow lives under [`skills/music-organiser`](skills/music-organiser). Its scripts, references, self-contained tests, and reviewed CC0 audio fixtures travel with the plugin in either runtime.

## Companion destinations

The public Music Organiser marketplace does not bundle platform implementations:

- Install `rekordbox-skill@dj-tools` when Rekordbox publication is required.
- The djay plugin is intentionally private and is not listed or fetched by the public marketplace.

Music Organiser can always finish after organisation or neutral cue proposals. It must report a missing destination plugin instead of silently routing through another platform.

## Runtime choices

- Metadata inference can use the active Claude/Codex provider or an approved local llama.cpp setup.
- Cue analysis is a separate local-only flow configured through `MUSIC_CUE_ENGINE_ROOT` or the plugin's ignored `.runtime/cue-engine` directory.
- Cue proposals are hash-bound and destination-neutral. Publishing them requires the appropriate destination plugin.
- Machine-specific libraries, models, databases, caches, and run evidence remain outside Git.

## Upstream work and acknowledgements

- [`payne0420/djay-pro-autohotcue`](https://github.com/payne0420/djay-pro-autohotcue) (MIT) is the principal prior implementation behind local hot-cue analysis. Historical integration work used commit `686ff9fe7f8c7391874e3784d214fad62d9ccaa9`.
- [`mcroydon/djcues`](https://github.com/mcroydon/djcues) (BSD-3-Clause) is relevant prior art for semantic A-H cue placement.
- [`CPJKU/beat_this`](https://github.com/CPJKU/beat_this) (MIT) supplies the learned beat/downbeat tracker used by the cue engine.
- [`dylanljones/pyrekordbox`](https://github.com/dylanljones/pyrekordbox) (MIT) underpins the standalone Rekordbox publication route maintained in the separate Rekordbox plugin.

See each upstream project for its complete license and notices. The marketplace is independent community tooling and is not affiliated with Algoriddim, AlphaTheta, Pioneer DJ, or Rekordbox.
