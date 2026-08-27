import unittest

from sanitize_music_audit import conservative_actionable_review


def item(artist, title):
    return {"outputMetadata": {"artist": artist, "title": title}}


class SanitizeMusicAuditTests(unittest.TestCase):
    def test_rejects_removal_of_mashup_context(self):
        self.assertFalse(conservative_actionable_review(
            "The 'Mash-Up' part of the title should be removed.",
            item("Fixture Artist", "Fixture Track (Fixture Mash-Up)"),
        ))

    def test_rejects_artist_absorbed_into_title(self):
        self.assertFalse(conservative_actionable_review(
            "The title should be 'Fixture Artist - Fixture Track (Fixture Remix)'.",
            item("Fixture Artist", "Fixture Track (Fixture Remix)"),
        ))
        self.assertFalse(conservative_actionable_review(
            "The title should be 'Fixture Track (Original)'.",
            item("Fixture Artist", "Original"),
        ))

    def test_retains_supported_version_restoration(self):
        self.assertTrue(conservative_actionable_review(
            "The title should include the '(Remastered 2019)' text to match the originalMetadata.",
            item("Fixture Artist", "Fixture Track"),
        ))


if __name__ == "__main__":
    unittest.main()
