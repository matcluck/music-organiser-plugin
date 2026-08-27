import tempfile
import unittest
from pathlib import Path

from resolve_import_plan_bundle import Plan, resolve_bundle


FIELDS = ["sourcePath", "newPath", "artist", "title"]


class ResolveImportPlanBundleTests(unittest.TestCase):
    def test_skips_lower_quality_cross_plan_identity_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            mp3 = root / "one.mp3"
            flac = root / "one.flac"
            mp3.write_bytes(b"larger mp3")
            flac.write_bytes(b"flac")
            plans = [
                Plan(root / "a.csv", FIELDS, [{
                    "sourcePath": str(mp3), "newPath": str(root / "a.mp3"),
                    "artist": "Artist", "title": "Track",
                }]),
                Plan(root / "b.csv", FIELDS, [{
                    "sourcePath": str(flac), "newPath": str(root / "b.flac"),
                    "artist": "Artist", "title": "Track",
                }]),
            ]
            skipped = resolve_bundle(plans)
            self.assertEqual(plans[0].rows[0]["newPath"], "")
            self.assertTrue(plans[1].rows[0]["newPath"])
            self.assertEqual(skipped[0]["reason"], "same_identity")

    def test_same_destination_unites_different_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "one.mp3"
            second = root / "two.mp3"
            first.write_bytes(b"one")
            second.write_bytes(b"two-two")
            destination = root / "same.mp3"
            plans = [
                Plan(root / "a.csv", FIELDS, [{
                    "sourcePath": str(first), "newPath": str(destination),
                    "artist": "A", "title": "One",
                }]),
                Plan(root / "b.csv", FIELDS, [{
                    "sourcePath": str(second), "newPath": str(destination),
                    "artist": "B", "title": "Two",
                }]),
            ]
            skipped = resolve_bundle(plans)
            self.assertEqual(sum(bool(p.rows[0]["newPath"]) for p in plans), 1)
            self.assertEqual(skipped[0]["reason"], "same_destination")


if __name__ == "__main__":
    unittest.main()
