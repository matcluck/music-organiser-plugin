import tempfile
import unittest
from pathlib import Path

from music_library_index import (
    load_library_identities,
    refresh_library_index,
)


class MusicLibraryIndexTests(unittest.TestCase):
    def test_incremental_refresh_and_forced_drift_detection(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            library = root / "library"
            library.mkdir()
            first = library / "first.mp3"
            second = library / "second.mp3"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            database = root / "index.sqlite"
            identities = {
                first.name: ("Artist One", "Title One", None),
                second.name: ("Artist Two", "Title Two", None),
            }
            reads = []

            def reader(path):
                reads.append(path.name)
                return identities[path.name]

            start = 1_000_000_000_000_000_000
            initial = refresh_library_index(
                database, library, identity_reader=reader, now_ns=start
            )
            self.assertEqual(initial.reindexed, 2)
            self.assertEqual(initial.verification_trigger, "new index")
            self.assertEqual(initial.drifted, 0)

            warm = refresh_library_index(
                database,
                library,
                identity_reader=reader,
                now_ns=start + 86_400 * 1_000_000_000,
            )
            self.assertEqual(warm.reused, 2)
            self.assertEqual(warm.reindexed, 0)
            self.assertIsNone(warm.verification_trigger)
            self.assertEqual(len(reads), 2)

            scheduled = refresh_library_index(
                database,
                library,
                identity_reader=reader,
                now_ns=start + 8 * 86_400 * 1_000_000_000,
            )
            self.assertEqual(scheduled.reindexed, 2)
            self.assertEqual(scheduled.drifted, 0)
            self.assertEqual(scheduled.verification_trigger, "age >= 7 days")

            identities[second.name] = ("Artist Two", "Changed Title", None)
            verified = refresh_library_index(
                database,
                library,
                identity_reader=reader,
                force_verify=True,
                now_ns=start + 9 * 86_400 * 1_000_000_000,
            )
            self.assertEqual(verified.reindexed, 2)
            self.assertEqual(verified.drifted, 1)
            self.assertEqual(verified.verification_trigger, "forced")
            indexed, scanned, unreadable = load_library_identities(database, library)
            self.assertEqual(scanned, 2)
            self.assertEqual(unreadable, 0)
            self.assertIn(("artist two", "changed title"), indexed)

    def test_index_fails_closed_for_a_different_library_root(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first_library = root / "first"
            second_library = root / "second"
            first_library.mkdir()
            second_library.mkdir()
            database = root / "index.sqlite"
            refresh_library_index(database, first_library)
            with self.assertRaisesRegex(ValueError, "different library"):
                load_library_identities(database, second_library)


if __name__ == "__main__":
    unittest.main()
