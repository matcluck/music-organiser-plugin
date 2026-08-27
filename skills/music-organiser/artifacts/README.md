# Local run artifacts

This directory is the local destination for manifests, plans, journals, reports, database backups,
and temporary staging created by Music Organiser workflows. Its contents are intentionally ignored
by Git because they can contain absolute library paths, original metadata, databases, and audio.

Historical run artifacts are intentionally excluded from the public skill repository. Keep local run evidence under this directory and do not commit it.

Create new runs under:

```text
artifacts/runs/YYYY-MM-DD-short-name/
```
