# Destination routing

Record one destination choice in the run plan:

| Choice | Required skill | Result |
| --- | --- | --- |
| `none` | none | Organized audio and evidence only |
| `djay` | `djay-skill` | Register/analyze/publish in djay only |
| `rekordbox` | `rekordbox-skill` | Import/analyze/publish in Rekordbox only |
| `both` | both skills | Independent verified publication from one source plan |

The organiser owns the canonical audio plan, metadata decisions, and cue proposal. Each destination skill owns its application lifecycle, backups, conflict policy, database writes, and read-back verification.

The public marketplace bundles neither platform skill. `rekordbox-skill` is a separate public-ready repository; `djay-skill` remains private. If the requested destination skill is unavailable, stop after the neutral artifact and explain what companion is missing. Never fetch, emulate, or substitute the other platform implicitly.

For `both`, do not implement the second target as a transfer side effect of the first. Publish from the same approved source artifact, keep separate journals, and allow one target to remain pending without rolling back a verified result in the other.
