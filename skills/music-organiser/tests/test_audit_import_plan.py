import unittest

from audit_import_plan import canonical_release_identity


class AuditImportPlanTests(unittest.TestCase):
    def test_feature_credit_can_move_from_title_to_artist(self):
        planned = canonical_release_identity("Fixture Artist", "Test Track [Ft. Guest]")
        existing = canonical_release_identity("Fixture Artist feat. Guest", "Test Track")
        self.assertEqual(planned, existing)

    def test_long_feature_credit_reduces_to_primary_artist(self):
        planned = canonical_release_identity(
            "Fixture Artist feat. Guest One, Guest Two, Guest Three", "Long Credit Test"
        )
        existing = canonical_release_identity("Fixture Artist", "Long Credit Test")
        self.assertEqual(planned, existing)

    def test_non_feature_suffix_is_not_removed(self):
        self.assertNotEqual(
            canonical_release_identity("Artist", "Song (Remix)"),
            canonical_release_identity("Artist", "Song"),
        )


if __name__ == "__main__":
    unittest.main()
