import unittest

from postprocess_music_manifest import postprocess_manifest


def metadata(**updates):
    value = {
        "title": "Fixture Track",
        "artist": "Fixture Artist",
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
    value.update(updates)
    return value


class PostprocessManifestTests(unittest.TestCase):
    def test_preserves_named_cover_performer_from_filename(self):
        manifest = [{
            "source": (
                r"fixtures\music\Fixture Covers - Fixture Track - Instrumental "
                r"Version Originally Performed By Source Artist.ogg"
            ),
            "originalMetadata": {},
            "outputMetadata": metadata(title="Fixture Track", artist="Source Artist"),
        }]

        cleaned, changes, _samples = postprocess_manifest(manifest)
        output = cleaned[0]["outputMetadata"]
        self.assertEqual(output["artist"], "Fixture Covers")
        self.assertIn("Originally Performed By Source Artist", output["title"])
        self.assertTrue(output["needsReview"])
        self.assertIn("cover performer", output["reviewReason"])
        self.assertEqual(changes["artist"], 1)

    def test_cleans_output_without_changing_source_evidence(self):
        manifest = [{
            "source": r"fixtures\music\Fixture Track.ogg",
            "originalMetadata": {"tags": {"album": [">Album Title Goes Here<"]}},
            "outputMetadata": metadata(
                title="Fixture Track (128bpm)",
                albumArtist="Fixture Artist",
                album=">Album Title Goes Here<",
                date="2020",
                trackNumber=1,
                genre="House",
            ),
        }]

        cleaned, changes, _samples = postprocess_manifest(manifest)
        output = cleaned[0]["outputMetadata"]
        self.assertEqual(output["title"], "Fixture Track")
        self.assertIsNone(output["album"])
        self.assertIsNone(output["albumArtist"])
        self.assertIsNone(output["trackNumber"])
        self.assertEqual(changes["title"], 1)
        self.assertEqual(cleaned[0]["originalMetadata"], manifest[0]["originalMetadata"])

    def test_recovers_title_after_removing_credit_only_title(self):
        manifest = [{
            "source": r"fixtures\music\Fupi - Melodic EDM (Prod. by Fixture).ogg",
            "originalMetadata": {},
            "outputMetadata": metadata(
                title="(Prod. by Fixture)",
                artist="Fupi",
            ),
        }]

        cleaned, _changes, _samples = postprocess_manifest(manifest)
        self.assertEqual(cleaned[0]["outputMetadata"]["title"], "Melodic EDM")
        self.assertFalse(cleaned[0]["outputMetadata"]["needsReview"])


if __name__ == "__main__":
    unittest.main()
