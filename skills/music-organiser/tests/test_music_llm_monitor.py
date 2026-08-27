import json
import tempfile
import unittest
from pathlib import Path

from serve_music_llm_monitor import run_status


class MusicLlmMonitorTests(unittest.TestCase):
    def test_discovers_manifest_runs_without_rekordbox_name(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            hdd_run = root / "fixture-run"
            hdd_run.mkdir()
            (hdd_run / "manifest.json").write_text(
                json.dumps([{"source": "staging/track.mp3"}]), encoding="utf-8"
            )

            unrelated = root / "model-benchmark"
            unrelated.mkdir()

            runs = run_status(root)

        self.assertEqual([run["name"] for run in runs], [hdd_run.name])
        self.assertEqual(runs[0]["tracks"], 1)


if __name__ == "__main__":
    unittest.main()
