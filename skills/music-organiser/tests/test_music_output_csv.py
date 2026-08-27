import unittest
from pathlib import Path

from build_music_output_csv import (
    build_rows,
    copy_suffix_depth,
    planned_path,
    sanitize_component,
)


def item(source: str, **metadata):
    output = {
        "title": None,
        "artist": None,
        "albumArtist": None,
        "album": None,
        "date": None,
        "trackNumber": None,
        "discNumber": None,
        "genre": None,
        "compilation": False,
        "needsReview": False,
        "reviewReason": None,
    }
    output.update(metadata)
    return {"source": source, "outputMetadata": output}


class MusicOutputCsvTests(unittest.TestCase):
    def test_uses_readable_windows_safe_punctuation(self):
        self.assertEqual(sanitize_component("Fixture:Theory", "Artist"), "Fixture-Theory")
        self.assertEqual(
            sanitize_component("Fixture Release / Alternate", "Release"),
            "Fixture Release - Alternate",
        )
        self.assertEqual(
            sanitize_component("Fixture Collection: Volume One", "Release"),
            "Fixture Collection - Volume One",
        )
        self.assertEqual(sanitize_component("What?", "Track"), "What？")
        self.assertEqual(sanitize_component("Star*Fixture", "Artist"), "Star＊Fixture")

    def test_copy_suffix_depth(self):
        self.assertEqual(copy_suffix_depth("Fixture Track"), 0)
        self.assertEqual(copy_suffix_depth("Fixture Track (1) (2) (3)"), 3)
        self.assertEqual(copy_suffix_depth("01 Fixture Track - 11A-1-1"), 2)
        self.assertEqual(copy_suffix_depth("Fixture Song - 1"), 1)

    def test_album_and_loose_paths(self):
        root = Path(r"fixtures\organized")
        album = item(
            r"fixtures\music\song.mp3",
            title="Melodic EDM",
            artist="Fupi",
            albumArtist="Fupi",
            album="CC0 Test Fixtures",
            trackNumber=1,
        )
        loose = item(
            r"fixtures\music\vip.wav",
            title="Special VIP",
            artist="Producer",
        )
        self.assertEqual(
            planned_path(Path(album["source"]), root, album["outputMetadata"]),
            root / "Fupi" / "CC0 Test Fixtures" / "01 - Melodic EDM.mp3",
        )
        self.assertEqual(
            planned_path(Path(loose["source"]), root, loose["outputMetadata"]),
            root / "Producer" / "Producer - Special VIP.wav",
        )

    def test_compilation_review_and_collisions(self):
        root = Path(r"fixtures\organized")
        compilation = item(
            r"fixtures\music\track.flac",
            title="Track",
            artist="Guest Artist",
            albumArtist="Various Artists",
            album="Collection",
            trackNumber=2,
            compilation=True,
        )
        review = item(
            r"fixtures\music\unknown.m4a",
            title="Track 01",
            needsReview=True,
            reviewReason="Artist is unknown.",
        )
        numbered_copy = dict(compilation)
        numbered_copy["source"] = r"fixtures\music\track-1.flac"
        unnumbered = dict(compilation)
        unnumbered["source"] = r"fixtures\music\track.flac"
        rows, review_blanks, duplicate_blanks = build_rows(
            [numbered_copy, unnumbered, review], root
        )
        self.assertEqual(rows[0]["newPath"], "")
        self.assertEqual(
            rows[1]["newPath"],
            str(root / "Various Artists" / "Collection" / "02 - Guest Artist - Track.flac"),
        )
        self.assertEqual(rows[2]["newPath"], "")
        self.assertEqual(review_blanks, 1)
        self.assertEqual(duplicate_blanks, 1)

    def test_user_excluded_track_has_no_planned_path(self):
        track = item(
            r"fixtures\music\Fixture MC - Test Rap.wav",
            title="Test Rap",
            artist="Fixture MC",
            excluded=True,
            exclusionReason="Intentionally excluded by user.",
        )

        self.assertIsNone(
            planned_path(
                Path(track["source"]),
                Path(r"fixtures\organized"),
                track["outputMetadata"],
            )
        )

    def test_duplicate_detection_ignores_audio_extension(self):
        root = Path(r"fixtures\organized")
        mp3 = item(
            r"fixtures\music\02 Melodic EDM - 9A.mp3",
            title="Melodic EDM (feat. Test Synth)",
            artist="Fupi & Fixture Artist",
            albumArtist="Fupi & Fixture Artist",
            album="CC0 Test Fixtures (Deluxe Edition)",
            trackNumber=2,
        )
        m4a = item(
            r"fixtures\music\02 Melodic EDM.m4a",
            title="Melodic EDM (feat. Test Synth)",
            artist="Fupi & Fixture Artist",
            albumArtist="Fupi & Fixture Artist",
            album="CC0 Test Fixtures (Deluxe Edition)",
            trackNumber=2,
        )

        rows, review_blanks, duplicate_blanks = build_rows([mp3, m4a], root)

        self.assertEqual(rows[0]["newPath"], "")
        self.assertEqual(
            rows[1]["newPath"],
            str(
                root
                / "Fupi & Fixture Artist"
                / "CC0 Test Fixtures (Deluxe Edition)"
                / "02 - Melodic EDM (feat. Test Synth).m4a"
            ),
        )
        self.assertEqual(review_blanks, 0)
        self.assertEqual(duplicate_blanks, 1)

    def test_build_rows_honors_top_level_output_extension(self):
        root = Path(r"fixtures\organized")
        track = item(
            r"fixtures\music\Artist - Track.wav",
            title="Track",
            artist="Artist",
        )
        track["outputExtension"] = ".aiff"

        rows, review_blanks, duplicate_blanks = build_rows([track], root)

        self.assertEqual(
            rows[0]["newPath"],
            str(root / "Artist" / "Artist - Track.aiff"),
        )
        self.assertEqual(review_blanks, 0)
        self.assertEqual(duplicate_blanks, 0)


if __name__ == "__main__":
    unittest.main()
