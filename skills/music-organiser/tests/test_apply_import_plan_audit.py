import unittest

from apply_import_plan_audit import resolve_plan


class ApplyImportPlanAuditTests(unittest.TestCase):
    def plan(self):
        return [{
            "sourcePath": r"fixtures\staging\one.mp3",
            "newPath": r"fixtures\music\Artist\Artist - One.mp3",
            "artist": "Artist",
            "title": "One",
        }]

    def finding(self, **overrides):
        row = {
            "sourcePath": r"fixtures\staging\one.mp3",
            "newPath": r"fixtures\music\Artist\Artist - One.mp3",
            "existingIdentityPaths": r"fixtures\music\Elsewhere\One.flac",
            "recommendation": "skip_existing",
        }
        row.update(overrides)
        return row

    def test_applies_evidenced_skip_existing(self):
        rows, skipped = resolve_plan(
            ["sourcePath", "newPath", "artist", "title"], self.plan(), [self.finding()]
        )
        self.assertEqual(rows[0]["newPath"], "")
        self.assertEqual(len(skipped), 1)

    def test_rejects_unresolved_collision(self):
        with self.assertRaisesRegex(ValueError, "Unsafe or unresolved"):
            resolve_plan(
                ["sourcePath", "newPath", "artist", "title"],
                self.plan(), [self.finding(recommendation="resolve_collision")],
            )

    def test_rejects_destination_mismatch(self):
        with self.assertRaisesRegex(ValueError, "does not match"):
            resolve_plan(
                ["sourcePath", "newPath", "artist", "title"],
                self.plan(), [self.finding(newPath=r"fixtures\music\wrong.mp3")],
            )

    def test_rejects_skip_without_identity_evidence(self):
        with self.assertRaisesRegex(ValueError, "lacks identity evidence"):
            resolve_plan(
                ["sourcePath", "newPath", "artist", "title"],
                self.plan(), [self.finding(existingIdentityPaths="")],
            )


if __name__ == "__main__":
    unittest.main()
