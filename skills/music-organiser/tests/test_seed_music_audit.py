import unittest

from seed_music_audit import seed_reviews


class SeedMusicAuditTests(unittest.TestCase):
    def test_seeds_only_exact_unchanged_rows(self):
        old = [
            {"source": "a.mp3", "outputMetadata": {"title": "A"}},
            {"source": "b.mp3", "outputMetadata": {"title": "B"}},
        ]
        new = [
            {"source": "a.mp3", "outputMetadata": {"title": "A"}},
            {"source": "b.mp3", "outputMetadata": {"title": "B fixed"}},
        ]
        audit = {"reviews": [
            {"id": 0, "needsRevision": False, "feedback": None},
            {"id": 1, "needsRevision": True, "feedback": "fix"},
        ]}
        seeded, changed = seed_reviews(old, new, audit)
        self.assertEqual([row["id"] for row in seeded], [0])
        self.assertEqual(changed, [1])

    def test_fails_closed_when_sources_reorder(self):
        with self.assertRaisesRegex(ValueError, "source changed"):
            seed_reviews(
                [{"source": "a"}], [{"source": "b"}],
                {"reviews": [{"id": 0, "needsRevision": False}]},
            )


if __name__ == "__main__":
    unittest.main()
