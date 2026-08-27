import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from build_music_manifest import write_json_atomic


class MusicManifestAtomicWriteTests(unittest.TestCase):
    def test_retries_transient_permission_error(self):
        original_replace = Path.replace
        calls = 0

        def transient_replace(path, target):
            nonlocal calls
            calls += 1
            if calls < 3:
                raise PermissionError("transient sharing violation")
            return original_replace(path, target)

        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "manifest.json"
            with patch.object(Path, "replace", transient_replace), patch(
                "build_music_manifest.time.sleep"
            ):
                write_json_atomic(output, [{"status": "saved"}])

            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), [{"status": "saved"}])
            self.assertEqual(calls, 3)
            self.assertFalse(output.with_name("manifest.json.tmp").exists())


if __name__ == "__main__":
    unittest.main()
