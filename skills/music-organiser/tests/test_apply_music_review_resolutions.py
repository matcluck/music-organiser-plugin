import unittest

from apply_music_review_resolutions import apply_resolutions


class ApplyMusicReviewResolutionsTests(unittest.TestCase):
    def test_can_add_evidence_backed_review_hold(self):
        manifest = [{
            "source": "bad.mp3",
            "originalMetadata": {},
            "outputMetadata": {
                "title": "Bad", "artist": "Artist", "needsReview": False,
                "reviewReason": None,
            },
        }]
        resolved = apply_resolutions(manifest, [{
            "source": "bad.mp3",
            "outputMetadata": {
                "needsReview": True,
                "reviewReason": "Decoder could not find an MPEG frame.",
            },
            "evidence": "Strict import preflight decoder failure.",
        }])
        self.assertTrue(resolved[0]["outputMetadata"]["needsReview"])

    def test_allows_exact_audit_authorized_non_review_correction(self):
        source = r"fixtures\staging\track.mp3"
        manifest = [{
            "source": source,
            "originalMetadata": {},
            "outputMetadata": {
                "title": "Short",
                "artist": "Artist",
                "albumArtist": None,
                "album": None,
                "date": None,
                "trackNumber": None,
                "discNumber": None,
                "genre": None,
                "compilation": False,
                "needsReview": False,
                "reviewReason": None,
            },
        }]
        resolutions = [{
            "source": source,
            "outputMetadata": {
                "title": "Full Embedded Title",
                "needsReview": False,
                "reviewReason": None,
            },
            "evidence": "Exact embedded TIT2 value confirmed by retained audit feedback.",
        }]
        resolved = apply_resolutions(
            manifest, resolutions, audited_sources={source.casefold()}
        )
        self.assertEqual(
            resolved[0]["outputMetadata"]["title"], "Full Embedded Title"
        )

    def test_applies_exact_evidence_backed_review_resolution(self):
        manifest = [
            {
                "source": r"fixtures\staging\track.mp3",
                "originalMetadata": {"artist": None},
                "outputMetadata": {
                    "title": "Track",
                    "artist": "Cover Artist",
                    "albumArtist": None,
                    "album": None,
                    "date": None,
                    "trackNumber": None,
                    "discNumber": None,
                    "genre": None,
                    "compilation": False,
                    "needsReview": True,
                    "reviewReason": "Verify cover identity.",
                },
            }
        ]
        resolved = apply_resolutions(
            manifest,
            [
                {
                    "source": r"fixtures\staging\track.mp3",
                    "evidence": "Published release listing.",
                    "outputMetadata": {
                        "title": "Track (Karaoke Version)",
                        "needsReview": False,
                        "reviewReason": None,
                    },
                }
            ],
        )
        self.assertEqual(resolved[0]["outputMetadata"]["title"], "Track (Karaoke Version)")
        self.assertFalse(resolved[0]["outputMetadata"]["needsReview"])
        self.assertTrue(manifest[0]["outputMetadata"]["needsReview"])
        self.assertEqual(resolved[0]["originalMetadata"], manifest[0]["originalMetadata"])

    def test_rejects_resolution_for_non_review_track(self):
        manifest = [
            {
                "source": "track.mp3",
                "outputMetadata": {
                    "title": "Track",
                    "artist": "Artist",
                    "needsReview": False,
                },
            }
        ]
        with self.assertRaisesRegex(ValueError, "not currently under review"):
            apply_resolutions(
                manifest,
                [
                    {
                        "source": "track.mp3",
                        "evidence": "Evidence",
                        "outputMetadata": {"needsReview": False},
                    }
                ],
            )
