import tempfile
import unittest
import wave
import os
import stat
import struct
from pathlib import Path

from mutagen.apev2 import APEBadItemError, APEv2
from mutagen.id3 import APIC, COMM, TIT2, Encoding
from mutagen.wave import WAVE

from apply_music_plan import (
    ID3_ALLOWED_FRAMES,
    PlanEntry,
    PlanError,
    execute_entry,
    image_mime,
    metadata_values,
    normalized_id3_picture,
    normalized_path,
    remove_all_apev2,
    remove_all_id3v1,
    rewrite_tags,
    validate_contract,
    verify_rewritten_file,
)
from build_music_output_csv import CSV_FIELDS, build_rows


def manifest_item(source: Path, **metadata):
    output = {
        "title": "Track",
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
    }
    output.update(metadata)
    return {
        "source": str(source),
        "originalMetadata": {
            "tags": {},
            "coverArt": {"exists": False, "count": 0},
        },
        "outputMetadata": output,
    }


class ApplyMusicPlanTests(unittest.TestCase):
    def test_contract_accepts_only_exact_evidenced_blank_skip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "source"
            destination_root = root / "organized"
            source_root.mkdir()
            source = source_root / "song.mp3"
            source.write_bytes(b"source")
            manifest = [manifest_item(source, title="Song", artist="Artist")]
            rows, _, _ = build_rows(manifest, destination_root)
            expected_destination = str(rows[0]["newPath"])
            csv_rows = [
                {field: "" if rows[0].get(field) is None else str(rows[0].get(field)) for field in CSV_FIELDS}
            ]
            csv_rows[0]["newPath"] = ""

            entries = validate_contract(
                manifest,
                csv_rows,
                source_root,
                destination_root,
                strict_duplicate_check=True,
                skip_evidence={normalized_path(source): normalized_path(Path(expected_destination))},
            )
            self.assertEqual(entries, [])

    def test_contract_rejects_blank_skip_without_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "source"
            destination_root = root / "organized"
            source_root.mkdir()
            source = source_root / "song.mp3"
            source.write_bytes(b"source")
            manifest = [manifest_item(source)]
            rows, _, _ = build_rows(manifest, destination_root)
            csv_rows = [
                {field: "" if rows[0].get(field) is None else str(rows[0].get(field)) for field in CSV_FIELDS}
            ]
            csv_rows[0]["newPath"] = ""
            with self.assertRaisesRegex(PlanError, "supply exact skip evidence"):
                validate_contract(
                    manifest, csv_rows, source_root, destination_root,
                    strict_duplicate_check=True,
                )

    def test_contract_accepts_exact_manifest_derived_csv(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "source"
            destination_root = root / "organized"
            source_root.mkdir()
            source = source_root / "song.mp3"
            source.write_bytes(b"not opened during contract validation")
            manifest = [manifest_item(source, title="Song", artist="Artist")]
            rows, _, _ = build_rows(manifest, destination_root)
            csv_rows = [
                {field: "" if row.get(field) is None else str(row.get(field)) for field in CSV_FIELDS}
                for row in rows
            ]

            entries = validate_contract(
                manifest,
                csv_rows,
                source_root,
                destination_root,
                strict_duplicate_check=True,
            )

            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].source, source)
            self.assertEqual(
                entries[0].destination,
                destination_root / "Artist" / "Artist - Song.mp3",
            )

    def test_contract_rejects_csv_metadata_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_root = root / "source"
            destination_root = root / "organized"
            source_root.mkdir()
            source = source_root / "song.mp3"
            source.write_bytes(b"source")
            manifest = [manifest_item(source, title="Song", artist="Artist")]
            rows, _, _ = build_rows(manifest, destination_root)
            csv_rows = [
                {field: "" if row.get(field) is None else str(row.get(field)) for field in CSV_FIELDS}
                for row in rows
            ]
            csv_rows[0]["title"] = "Different Song"

            with self.assertRaises(PlanError):
                validate_contract(
                    manifest,
                    csv_rows,
                    source_root,
                    destination_root,
                    strict_duplicate_check=True,
                )

    def test_rewrites_id3_whitelist_and_preserves_artwork(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "track.wav"
            with wave.open(str(path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(8000)
                output.writeframes(b"\x00\x00" * 800)

            audio = WAVE(path)
            audio.add_tags()
            assert audio.tags is not None
            artwork = b"\xff\xd8\xff\xe0test-cover"
            audio.tags.add(TIT2(encoding=Encoding.UTF16, text=["Old Title"]))
            audio.tags.add(
                COMM(encoding=Encoding.UTF16, lang="eng", desc="", text=["remove me"])
            )
            audio.tags.add(
                APIC(
                    encoding=Encoding.UTF16,
                    mime="image/jpeg",
                    type=3,
                    desc="Cover",
                    data=artwork,
                )
            )
            audio.save(v2_version=3)

            metadata = metadata_values(
                {
                    "title": "Clean Title",
                    "artist": "Clean Artist",
                    "albumArtist": "Clean Album Artist",
                    "album": "Clean Album",
                    "date": "2024",
                    "trackNumber": 2,
                    "discNumber": 1,
                    "genre": "House",
                    "compilation": True,
                }
            )
            self.assertEqual(rewrite_tags(path, metadata), 1)
            verify_rewritten_file(path, metadata, 1)

            rewritten = WAVE(path)
            assert rewritten.tags is not None
            self.assertEqual(
                {frame.FrameID for frame in rewritten.tags.values()} - ID3_ALLOWED_FRAMES,
                set(),
            )
            self.assertEqual(rewritten.tags.getall("APIC")[0].data, artwork)

    def test_normalizes_malformed_id3_artwork(self):
        jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00payload"
        leading_null = APIC(
            encoding=Encoding.LATIN1,
            mime="image/jpeg",
            type=3,
            desc="",
            data=b"\x00" + jpeg,
        )
        prefixed = APIC(
            encoding=Encoding.LATIN1,
            mime="image/jpeg",
            type=3,
            desc="\xff\xd8\xff\xe0",
            data=b"\x10JFIF\x00payload",
        )
        placeholder = APIC(
            encoding=Encoding.LATIN1,
            mime="",
            type=0,
            desc="",
            data=b"\x00" * 124,
        )

        self.assertEqual(image_mime(jpeg), "image/jpeg")
        self.assertEqual(normalized_id3_picture(leading_null), (jpeg, "image/jpeg"))
        self.assertEqual(normalized_id3_picture(prefixed), (jpeg, "image/jpeg"))
        self.assertIsNone(normalized_id3_picture(placeholder))

    def test_copying_read_only_source_produces_writable_verified_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            destination = root / "organized" / "track.wav"
            with wave.open(str(source), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(8000)
                output.writeframes(b"\x00\x00" * 800)

            audio = WAVE(source)
            audio.add_tags()
            assert audio.tags is not None
            audio.tags.add(TIT2(encoding=Encoding.UTF16, text=["Old Title"]))
            audio.save(v2_version=3)
            metadata = metadata_values(
                {
                    "title": "Clean Title",
                    "artist": "Clean Artist",
                    "compilation": False,
                }
            )
            os.chmod(source, stat.S_IREAD)
            try:
                status = execute_entry(
                    PlanEntry(source, destination, metadata, expected_artwork=0),
                    "copy",
                )
                self.assertEqual(status, "copied")
                self.assertTrue(source.exists())
                self.assertTrue(destination.exists())
                verify_rewritten_file(destination, metadata, 0)
                self.assertTrue(destination.stat().st_mode & stat.S_IWRITE)
            finally:
                os.chmod(source, stat.S_IREAD | stat.S_IWRITE)

    def test_resume_uses_effective_artwork_count_for_placeholder_frame(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            destination = root / "organized" / "track.wav"
            with wave.open(str(source), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(8000)
                output.writeframes(b"\x00\x00" * 800)

            audio = WAVE(source)
            audio.add_tags()
            assert audio.tags is not None
            audio.tags.add(TIT2(encoding=Encoding.UTF16, text=["Old Title"]))
            audio.tags.add(
                APIC(
                    encoding=Encoding.LATIN1,
                    mime="",
                    type=0,
                    desc="",
                    data=b"\x00" * 124,
                )
            )
            audio.save(v2_version=3)
            metadata = metadata_values(
                {
                    "title": "Clean Title",
                    "artist": "Clean Artist",
                    "compilation": False,
                }
            )
            entry = PlanEntry(source, destination, metadata, expected_artwork=1)

            self.assertEqual(execute_entry(entry, "copy"), "copied")
            self.assertEqual(execute_entry(entry, "copy"), "resumed")
            verify_rewritten_file(destination, metadata, 0)

    def test_move_removes_read_only_source_after_verifying_existing_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.wav"
            destination = root / "organized" / "track.wav"
            with wave.open(str(source), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(8000)
                output.writeframes(b"\x00\x00" * 800)

            metadata = metadata_values(
                {
                    "title": "Clean Title",
                    "artist": "Clean Artist",
                    "compilation": False,
                }
            )
            entry = PlanEntry(source, destination, metadata, expected_artwork=0)
            self.assertEqual(execute_entry(entry, "copy"), "copied")
            os.chmod(source, stat.S_IREAD)

            self.assertEqual(execute_entry(entry, "move"), "resumed")
            self.assertFalse(source.exists())
            verify_rewritten_file(destination, metadata, 0)

    def test_removes_stacked_id3v1_footers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stacked.mp3"

            def field(value: str, width: int) -> bytes:
                return value.encode("latin-1").ljust(width, b"\x00")[:width]

            id3v1 = (
                b"TAG"
                + field("Title", 30)
                + field("Artist", 30)
                + field("Album", 30)
                + field("2000", 4)
                + field("Comment", 30)
                + bytes([12])
            )
            audio_bytes = b"audio payload" * 100
            path.write_bytes(audio_bytes + id3v1 * 3)

            self.assertEqual(remove_all_id3v1(path), 3)
            self.assertEqual(path.read_bytes(), audio_bytes)
            self.assertEqual(remove_all_id3v1(path), 0)

    def test_removes_malformed_apev2_without_decoding_items(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "malformed.mp3"
            audio_bytes = b"audio payload" * 100
            value = b"Windows quote: \x92"
            item = (
                struct.pack("<II", len(value), 0)
                + b"Comment\x00"
                + value
            )
            footer = (
                b"APETAGEX"
                + struct.pack("<IIII", 2000, len(item) + 32, 1, 0)
                + (b"\x00" * 8)
            )
            path.write_bytes(audio_bytes + item + footer)

            with self.assertRaises(APEBadItemError):
                APEv2(path)
            self.assertEqual(remove_all_apev2(path), 1)
            self.assertEqual(path.read_bytes(), audio_bytes)
            self.assertEqual(remove_all_apev2(path), 0)


if __name__ == "__main__":
    unittest.main()
