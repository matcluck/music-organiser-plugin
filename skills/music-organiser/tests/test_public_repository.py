import hashlib
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mutagen import File as MutagenFile


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = ROOT.parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "audio"


def publishable_files():
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return [REPOSITORY_ROOT / line for line in result.stdout.splitlines() if line and (REPOSITORY_ROOT / line).is_file()]


class PublicRepositoryTests(unittest.TestCase):
    def test_publishable_text_has_no_private_machine_data(self):
        forbidden = {
            "macOS user path": "/Users" + "/",
            "Linux user path": "/home" + "/",
        }
        drive_path = re.compile(r"\b[A-Za-z]:\\")
        private_ip = re.compile(r"\b(?:10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)\d{1,3}(?:\.\d{1,3}){2}\b")
        email = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
        secret = re.compile(r"\b(?:api[_-]?key|password|secret|access[_-]?token)\s*[:=]\s*['\"][^<'\"]+", re.I)
        failures = []
        for path in publishable_files():
            if path.suffix.lower() in {".ogg", ".png", ".jpg", ".jpeg", ".db"}:
                continue
            text = path.read_text(encoding="utf-8", errors="strict")
            for label, marker in forbidden.items():
                if marker.casefold() in text.casefold():
                    failures.append(f"{path.relative_to(REPOSITORY_ROOT)}: {label}")
            if drive_path.search(text):
                failures.append(f"{path.relative_to(REPOSITORY_ROOT)}: absolute drive path")
            if private_ip.search(text):
                failures.append(f"{path.relative_to(REPOSITORY_ROOT)}: private IP address")
            if email.search(text):
                failures.append(f"{path.relative_to(REPOSITORY_ROOT)}: email address")
            if secret.search(text):
                failures.append(f"{path.relative_to(REPOSITORY_ROOT)}: possible secret assignment")
        self.assertEqual(failures, [])

    def test_cc0_fixture_ledger_is_complete_and_verified(self):
        ledger = json.loads((FIXTURES / "provenance.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(ledger), 10)
        self.assertLessEqual(len(ledger), 20)
        self.assertEqual({entry["license"] for entry in ledger}, {"CC0-1.0"})
        self.assertEqual(
            {path.name for path in FIXTURES.glob("*.ogg")},
            {entry["file"] for entry in ledger},
        )
        for entry in ledger:
            path = FIXTURES / entry["file"]
            self.assertEqual(path.stat().st_size, entry["bytes"])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest().upper(), entry["sha256"])
            audio = MutagenFile(path)
            self.assertIsNotNone(audio)
            self.assertGreater(audio.info.length, 0)

    def test_cc0_fixture_manifest_builds_end_to_end(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "manifest.json"
            completed = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "build_music_manifest.py"), str(FIXTURES), "--out", str(output), "--overwrite"],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            manifest = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(len(manifest), 13)
            self.assertTrue(all(item["source"].lower().endswith(".ogg") for item in manifest))

    def test_cue_proposal_generator_has_a_standalone_cli(self):
        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "generate_cue_proposals.py"), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("destination-neutral", completed.stdout)
        self.assertIn("--cue-engine-root", completed.stdout)
        self.assertNotIn("--cue-workspace", completed.stdout)


if __name__ == "__main__":
    unittest.main()
