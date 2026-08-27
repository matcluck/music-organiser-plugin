import csv
import tempfile
import unittest
from pathlib import Path

from prepare_music_import import csv_has_rows, is_within


class PrepareMusicImportTests(unittest.TestCase):
    def test_prepared_output_must_remain_in_work_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            work = root / "work"
            work.mkdir()
            self.assertTrue(is_within(work / "audio", work))
            self.assertFalse(is_within(root / "library", work))

    def test_csv_row_gate_distinguishes_header_only_report(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            report = Path(temporary_directory) / "report.csv"
            with report.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["finding"])
                writer.writeheader()
            self.assertFalse(csv_has_rows(report))
            with report.open("a", encoding="utf-8", newline="") as handle:
                csv.DictWriter(handle, fieldnames=["finding"]).writerow({"finding": "x"})
            self.assertTrue(csv_has_rows(report))


if __name__ == "__main__":
    unittest.main()
