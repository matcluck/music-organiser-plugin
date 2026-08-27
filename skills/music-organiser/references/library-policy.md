# Library Policy

## Clean Metadata Schema

Write only:

- `title`
- `artist`
- `albumArtist`
- `album`
- `date`
- `trackNumber`
- `discNumber`
- `genre`
- `compilation`
- embedded cover art

Keep `needsReview` and `reviewReason` in manifests only. Strip comments, URLs, social handles, BPM, key, ratings, play counts, sort fields, encoder/application data, DJ grids/cues, and unknown custom tags.

Use null for unknown values. Never write the literal string `"null"`.

## Path Layout

Albumless track:

```text
Artist/Artist - Title.ext
```

Album track:

```text
Album Artist/Album/01 - Title.ext
```

Omit the numeric prefix when the track number is unknown. For disc 2+, use `2-01 - Title.ext`.

Compilation track:

```text
Various Artists/Album/01 - Artist - Title.ext
```

DJ utility audio:

```text
DJ Samples/Category/Title.ext
```

Use `albumArtist = DJ Samples` and `genre = DJ Tools`. Retain the real source artist for extracted clips and acapellas when known.

## Album Decisions

- Assign an album only with credible release evidence.
- Put every assigned album under an album folder; do not leave tagged album tracks at artist root.
- Do not put unofficial remixes, bootlegs, VIPs, mashups, or SoundCloud edits into the sampled original's album without evidence.
- Treat `Single` and `EP` releases as albums for folder organization when the release is credible.
- Set `albumArtist = Various Artists` and `compilation = true` for genuine compilations.

## Text Cleanup

- Remove DJ keys such as `5A`, `10A/4A`, and suffixes like `9A-1-1`.
- Remove `[FREE DL]`, `free download`, domains, source-site labels, hashtags used as genre, and promotional boilerplate.
- Keep meaningful version labels: `Remix`, `Bootleg`, `VIP`, `Edit`, `Mix`, `Acapella`, `Instrumental`, and featured artists.
- Repair unbalanced brackets and obvious mojibake only when the intended text is clear.
- Use official or conventional casing. Do not blindly title-case stylized names such as `INNA`, `AC/DC`, or `3LAU`.
- Keep fields pure: artist fields contain artists, title contains the track/version, album contains the release, and genre contains a genre.

## Duplicate Policy

1. Check destination path collisions.
2. Check normalized artist/title against the existing library.
3. Check decoded-audio hashes when filenames/tags differ but recordings appear duplicated.
4. Prefer an unsuffixed source name over `(1)`, `(2)`, `-1-1`, and similar copies when recordings are identical.
5. Keep the existing organized-library copy by default.
6. Delete or move duplicate sources only after the retained file exists and verifies.

Different durations or explicit versions may be different edits. Do not collapse them solely because Shazam returned the same underlying song.

## Recognition Policy

- A Shazam match is strong identity evidence, not proof of album edition or version.
- A match inside an intro or mashup may identify only one sampled source.
- Preserve custom-edit context and route uncertain material to review.
- Use local LLMs for sanitation and normalization, not factual invention.

## iTunes Compatibility

- Build an M3U8 from final absolute destination paths after all moves.
- Remove old iTunes entries with **Keep Files** before changing paths, then reimport the regenerated playlist.
- WAV tagging is inconsistently visible in iTunes. Prefer lossless AIFF for final tagged PCM files.
