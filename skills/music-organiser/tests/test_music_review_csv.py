import unittest
from pathlib import Path

from build_music_review_csv import audit_findings, build_review_rows


def item(source: str, **metadata):
    output = {
        "title": "Track Name",
        "artist": "Artist Name",
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
    return {"source": source, "originalMetadata": {}, "outputMetadata": output}


class MusicReviewCsvTests(unittest.TestCase):
    def test_user_excluded_track_is_not_a_review_item(self):
        track = item(
            r"fixtures\music\Fixture MC - Test Rap.wav",
            excluded=True,
            exclusionReason="Intentionally excluded by user.",
        )

        rows, priorities, excluded_duplicates = build_review_rows(
            [track], Path(r"fixtures\organized")
        )

        self.assertEqual(rows, [])
        self.assertEqual(dict(priorities), {})
        self.assertEqual(excluded_duplicates, 0)

    def test_prioritizes_review_and_contamination_findings(self):
        reviewed = item(
            r"fixtures\music\unknown.mp3",
            title=None,
            artist=None,
            needsReview=True,
            reviewReason="Identity is unknown.",
        )
        findings = audit_findings(reviewed)
        self.assertIn("needs review", [finding.reason for finding in findings])
        self.assertIn("missing title or artist", [finding.reason for finding in findings])
        self.assertTrue(all(finding.priority == "High" for finding in findings))

        dirty = item(
            r"fixtures\music\track.mp3",
            title="unfinished_master",
            artist="fixture artist",
        )
        findings = audit_findings(dirty)
        reasons = {finding.reason: finding.priority for finding in findings}
        self.assertEqual(reasons["title contains underscore"], "Medium")
        self.assertEqual(reasons["title contains workflow or promo text"], "Medium")
        self.assertEqual(reasons["artist casing"], "Medium")

    def test_excludes_duplicate_losers_and_keeps_original_evidence(self):
        numbered = item(
            r"fixtures\music\Track Name-1.mp3",
            artist="fixture artist",
        )
        winner = item(
            r"fixtures\music\Track Name.mp3",
            artist="fixture artist",
        )
        winner["originalMetadata"] = {
            "tags": {
                "TIT2": {"text": ["Track Name"]},
                "TPE1": {"text": ["fixture artist"]},
            }
        }

        rows, priorities, excluded = build_review_rows(
            [numbered, winner], Path(r"fixtures\organized")
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sourcePath"], winner["source"])
        self.assertEqual(rows[0]["originalTitle"], "Track Name")
        self.assertEqual(rows[0]["originalArtist"], "fixture artist")
        self.assertEqual(priorities["Medium"], 1)
        self.assertEqual(excluded, 1)


if __name__ == "__main__":
    unittest.main()
