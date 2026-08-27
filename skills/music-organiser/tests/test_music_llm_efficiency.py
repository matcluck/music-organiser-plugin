import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import audit_music_manifest_parallel as audit
import populate_music_manifest as populate
from populate_music_manifest import (
    BatchOutcome,
    EndpointLease,
    EndpointLeaseError,
    LlamaHTTPError,
    ServerConfig,
    build_user_prompt,
    concise_source,
    metadata_evidence,
    parse_model_results,
    process_batch,
)


def manifest_item():
    return {
        "source": (
            r"fixtures\project\artifacts\runs\example\staging"
            r"\Artist\Album\01 - Track.mp3"
        ),
        "originalMetadata": {
            "tagFormat": "ID3",
            "coverArt": {"exists": True, "byteLength": 999999},
            "tags": {
                "TIT2": {"type": "TIT2", "encoding": 3, "text": ["Track"]},
                "TPE1": {"type": "TPE1", "encoding": 3, "text": ["Artist"]},
                "TALB": {"type": "TALB", "encoding": 3, "text": ["Album"]},
                "TDRC": {
                    "type": "TDRC",
                    "text": [{"year": 2024, "month": 5, "day": 6}],
                },
                "TRCK": {"text": ["1/10"]},
                "TKEY": {"text": ["9m"]},
                "TBPM": {"text": ["174"]},
                "WOAF": {"url": "https://example.invalid/track"},
                "TXXX:track_url": {"text": ["https://example.invalid/track"]},
                "TSSE": {"text": ["Lavf61.7.100"]},
                "PRIV:TRAKTOR4": {
                    "exists": True,
                    "byteLength": 8192,
                    "binaryBase64": "unused",
                },
            },
        },
        "outputMetadata": {
            "title": None,
            "artist": None,
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
    }


class CompactTransportTests(unittest.TestCase):
    def test_metadata_evidence_keeps_only_canonical_release_fields(self):
        evidence = metadata_evidence(manifest_item()["originalMetadata"], 500, 2400)
        self.assertEqual(
            evidence,
            {
                "title": "Track",
                "artist": "Artist",
                "album": "Album",
                "date": "2024-05-06",
                "trackNumber": "1/10",
            },
        )
        serialized = json.dumps(evidence)
        for noise in ("TKEY", "TBPM", "WOAF", "Lavf", "binaryBase64", "encoding"):
            self.assertNotIn(noise, serialized)

    def test_prompt_uses_short_source_and_compact_json(self):
        item = manifest_item()
        prompt_item = {
            "id": 0,
            "source": concise_source(item["source"]),
            "originalMetadata": metadata_evidence(item["originalMetadata"], 500, 2400),
        }
        prompt = build_user_prompt([prompt_item])
        payload = prompt[prompt.index("{") :]
        self.assertEqual(
            concise_source(item["source"]), r"Artist\Album\01 - Track.mp3"
        )
        self.assertNotIn("\n  ", payload)
        self.assertEqual(json.loads(payload)["tracks"][0]["id"], 0)

    def test_compact_and_legacy_responses_normalize_to_same_manifest_shape(self):
        compact = json.dumps(
            {
                "items": [
                    {
                        "id": 7,
                        "title": "Track",
                        "artist": "Artist",
                        "album": "Album",
                    }
                ]
            }
        )
        legacy_output = {
            "title": "Track",
            "artist": "Artist",
            "albumArtist": None,
            "album": "Album",
            "date": None,
            "trackNumber": None,
            "discNumber": None,
            "genre": None,
            "compilation": False,
            "needsReview": False,
            "reviewReason": None,
        }
        legacy = json.dumps(
            {"items": [{"id": 7, "outputMetadata": legacy_output}]}
        )
        self.assertEqual(
            parse_model_results(compact, {7}),
            parse_model_results(legacy, {7}),
        )
        self.assertEqual(
            set(parse_model_results(compact, {7})[7]),
            set(legacy_output),
        )

    def test_review_reason_infers_review_state(self):
        response = json.dumps(
            {
                "items": [
                    {
                        "id": 1,
                        "title": None,
                        "artist": None,
                        "reviewReason": "Core identity is unsupported.",
                    }
                ]
            }
        )
        result = parse_model_results(response, {1})[1]
        self.assertTrue(result["needsReview"])
        self.assertEqual(result["reviewReason"], "Core identity is unsupported.")

    def test_consensus_guard_preserves_digit_leading_artist(self):
        item = manifest_item()
        item["source"] = r"fixtures\staging\1991, Fixture Duo\Fixture Track\1991, Fixture Duo - Fixture Track.mp3"
        item["originalMetadata"]["tags"]["TPE1"]["text"] = ["1991, Fixture Duo"]
        model_output = populate.clean_output_metadata(
            {"title": "Fixture Track", "artist": "Fixture Duo"}
        )
        repaired = populate.preserve_supported_numeric_artist(model_output, item)
        self.assertEqual(repaired["artist"], "1991, Fixture Duo")

    def test_numeric_guard_does_not_restore_filename_index(self):
        item = manifest_item()
        item["source"] = r"fixtures\staging\Artist\Album\01 - Artist - Track.mp3"
        item["originalMetadata"]["tags"]["TPE1"]["text"] = ["01"]
        model_output = populate.clean_output_metadata(
            {"title": "Track", "artist": "Artist"}
        )
        repaired = populate.preserve_supported_numeric_artist(model_output, item)
        self.assertEqual(repaired["artist"], "Artist")

    def test_album_artist_audit_cannot_rewrite_accepted_identity(self):
        original = populate.clean_output_metadata(
            {
                "title": "Fixture Track (Extended Mix)",
                "artist": "Fixture Duo, Guest Singer",
                "albumArtist": "Fixture Duo, Guest Singer",
                "album": "Fixture Release (Remixes)",
            }
        )
        candidate = populate.clean_output_metadata(
            {
                "title": "Fixture Track",
                "artist": "Fixture Duo",
                "album": "Fixture Release (Remixes)",
            }
        )
        merged = populate.constrain_audit_revision(
            original,
            candidate,
            "Remove unsupported albumArtist 'Fixture Duo, Guest Singer'; "
            "the source has no album-artist tag and is not a compilation.",
        )
        self.assertEqual(merged["title"], original["title"])
        self.assertEqual(merged["artist"], original["artist"])
        self.assertEqual(merged["album"], original["album"])
        self.assertIsNone(merged["albumArtist"])

    def test_album_artist_removal_is_applied_without_model_revision(self):
        item = manifest_item()
        original = populate.clean_output_metadata(
            {
                "title": "Track",
                "artist": "Artist",
                "album": "Album",
                "albumArtist": "Artist",
            }
        )
        corrected, applied = populate.apply_deterministic_audit_feedback(
            original,
            item,
            "Remove unsupported albumArtist 'Artist'; the source has no "
            "album-artist tag and is not a compilation.",
        )
        self.assertTrue(applied)
        self.assertIsNone(corrected["albumArtist"])
        self.assertEqual(corrected["title"], "Track")
        self.assertEqual(corrected["artist"], "Artist")


class FailureClassificationTests(unittest.TestCase):
    def test_endpoint_lease_defaults_to_repository_artifacts(self):
        repository_root = Path(__file__).resolve().parents[3]
        lease = EndpointLease(["http://127.0.0.1:8080/v1"])
        self.assertEqual(lease.lock_dir, repository_root / "artifacts" / "locks")

    def test_endpoint_lease_rejects_concurrent_music_job(self):
        with tempfile.TemporaryDirectory() as directory:
            first = EndpointLease(
                ["http://127.0.0.1:8080/v1"], Path(directory)
            ).acquire()
            try:
                second = EndpointLease(
                    ["http://127.0.0.1:8080/v1"], Path(directory)
                )
                with self.assertRaises(EndpointLeaseError):
                    second.acquire()
            finally:
                first.release()

    def test_context_error_is_not_retried_unchanged(self):
        item = manifest_item()
        with patch(
            "populate_music_manifest.call_llama_cpp",
            side_effect=LlamaHTTPError(
                400, "request exceeds the available context size"
            ),
        ) as call:
            outcome = process_batch(
                1,
                [0, 1],
                ServerConfig("http://127.0.0.1:1/v1", "model"),
                [item, item],
                {},
                500,
                2400,
                1,
                512,
                3,
            )
        self.assertEqual(call.call_count, 1)
        self.assertEqual(outcome.error_kind, "context")

    def test_loading_error_is_returned_for_scheduler_backoff(self):
        item = manifest_item()
        with patch(
            "populate_music_manifest.call_llama_cpp",
            side_effect=LlamaHTTPError(503, '{"message":"Loading model"}'),
        ) as call:
            outcome = process_batch(
                1,
                [0],
                ServerConfig("http://127.0.0.1:1/v1", "model"),
                [item],
                {},
                500,
                2400,
                1,
                512,
                3,
            )
        self.assertEqual(call.call_count, 1)
        self.assertEqual(outcome.error_kind, "unavailable")


class AuditTransportTests(unittest.TestCase):
    def test_parallel_audit_cli_invokes_its_parser(self):
        completed = subprocess.run(
            [sys.executable, audit.__file__, "--help"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Audit cleaned metadata", completed.stdout)

    def test_audit_sends_compact_cached_request_and_returns_only_flags(self):
        captured = {}

        def fake_http_json(url, payload, timeout):
            captured.update({"url": url, "payload": payload, "timeout": timeout})
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "reviews": [
                                        {
                                            "id": 0,
                                            "feedback": (
                                                "Remove residual key '9A' from title; "
                                                "source title tag is 'Track'."
                                            ),
                                        }
                                    ]
                                }
                            )
                        }
                    }
                ]
            }

        item = manifest_item()
        item["outputMetadata"]["title"] = "Track - 9A"
        item["outputMetadata"]["artist"] = "Artist"
        with patch.object(audit, "http_json", side_effect=fake_http_json):
            reviews = audit.request(
                "http://127.0.0.1:8080/v1", "model", [(0, item)], 30
            )
        self.assertTrue(captured["payload"]["cache_prompt"])
        self.assertNotIn("\n  ", captured["payload"]["messages"][1]["content"])
        self.assertEqual(
            reviews,
            [
                {
                    "id": 0,
                    "needsRevision": True,
                    "feedback": (
                        "Remove residual key '9A' from title; "
                        "source title tag is 'Track'."
                    ),
                }
            ],
        )

    def test_audit_empty_reviews_means_sound_batch(self):
        with patch.object(
            audit,
            "http_json",
            return_value={
                "choices": [{"message": {"content": '{"reviews":[]}'}}]
            },
        ):
            self.assertEqual(
                audit.request(
                    "http://127.0.0.1:8080/v1",
                    "model",
                    [(0, manifest_item())],
                    30,
                ),
                [],
            )

    def test_audit_rejects_request_to_fill_missing_optional_field(self):
        item = manifest_item()
        item["outputMetadata"]["title"] = "Track"
        item["outputMetadata"]["artist"] = "Artist"
        response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "reviews": [
                                    {"id": 0, "feedback": "Missing genre. Add Pop."}
                                ]
                            }
                        )
                    }
                }
            ]
        }
        with patch.object(audit, "http_json", return_value=response):
            reviews = audit.request(
                "http://127.0.0.1:8080/v1", "model", [(0, item)], 30
            )
        self.assertEqual(reviews, [])

    def test_audit_rejects_noop_version_and_identical_artist_feedback(self):
        item = manifest_item()
        item["outputMetadata"]["title"] = "Fixture Track (Extended Mix)"
        item["outputMetadata"]["artist"] = "Fixture Artist"
        self.assertFalse(
            audit.actionable_review(
                "title should include '(Extended Mix)', matching originalMetadata title field",
                item,
            )
        )
        self.assertFalse(
            audit.actionable_review(
                "The 'artist' field should be 'Fixture Artist' instead of 'Fixture Artist'. "
                "The original metadata includes the artist prefix.",
                item,
            )
        )

    def test_audit_rejects_claimed_version_absent_from_source_evidence(self):
        item = manifest_item()
        item["outputMetadata"]["title"] = "Fixture Track"
        item["originalMetadata"]["tags"]["TIT2"]["text"] = ["Fixture Track"]
        self.assertFalse(
            audit.actionable_review(
                "Keep the 'Radio Edit' text in the title. The original metadata "
                "includes the remix type as part of the title.",
                item,
            )
        )

    def test_audit_does_not_requeue_existing_manual_review(self):
        item = manifest_item()
        item["outputMetadata"]["needsReview"] = True
        item["outputMetadata"]["reviewReason"] = "Identity is uncertain."
        self.assertFalse(
            audit.actionable_review(
                "Remove 'v3' from title; original metadata title tag is 'Track v3'.",
                item,
            )
        )

    def test_audit_does_not_repeat_applied_album_artist_removal(self):
        item = manifest_item()
        item["outputMetadata"]["albumArtist"] = None
        self.assertFalse(
            audit.actionable_review(
                "Remove unsupported albumArtist 'Artist'; the source has no "
                "album-artist tag and is not a compilation.",
                item,
            )
        )

    def test_audit_does_not_repeat_semantically_identical_album_artist_replace(self):
        item = manifest_item()
        item["outputMetadata"]["albumArtist"] = "F.X."
        self.assertFalse(
            audit.actionable_review(
                "Replace albumArtist 'F.X' with source-tag value 'F.X.'.",
                item,
            )
        )


class SchedulerTests(unittest.TestCase):
    def run_main_with_worker(self, directory, worker, endpoints):
        root = Path(directory)
        manifest_path = root / "manifest.json"
        output_path = root / "manifest-llm.json"
        items = [manifest_item(), manifest_item()]
        items[1]["source"] = items[1]["source"].replace("Track.mp3", "Track 2.mp3")
        manifest_path.write_text(json.dumps(items), encoding="utf-8")
        argv = [
            "populate_music_manifest.py",
            str(manifest_path),
            "--out",
            str(output_path),
            "--batch-size",
            "2",
            "--allow-shared-endpoints",
        ]
        for endpoint in endpoints:
            argv.extend(["--endpoint", endpoint])
        with (
            patch.object(populate, "discover_model", return_value="model"),
            patch.object(populate, "process_batch", side_effect=worker),
            patch.object(populate.sys, "argv", argv),
        ):
            code = populate.main()
        return code, json.loads(output_path.read_text(encoding="utf-8"))

    def test_context_overflow_batch_is_split_and_completed(self):
        calls = []

        def worker(batch_number, item_ids, server, *_args, **_kwargs):
            calls.append(list(item_ids))
            if len(item_ids) > 1:
                return BatchOutcome(
                    batch_number,
                    item_ids,
                    server,
                    None,
                    {},
                    0.01,
                    [],
                    "too large",
                    "context",
                )
            item_id = item_ids[0]
            return BatchOutcome(
                batch_number,
                item_ids,
                server,
                {
                    item_id: {
                        "title": f"Track {item_id + 1}",
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
                },
                {},
                0.01,
                [],
                None,
            )

        with tempfile.TemporaryDirectory() as directory:
            code, result = self.run_main_with_worker(
                directory, worker, ["http://127.0.0.1:8080/v1"]
            )
        self.assertEqual(code, 0)
        self.assertEqual(calls, [[0, 1], [0], [1]])
        self.assertEqual([item["outputMetadata"]["artist"] for item in result], ["Artist", "Artist"])

    def test_unavailable_endpoint_requeues_work_to_healthy_endpoint(self):
        endpoints_seen = []

        def worker(batch_number, item_ids, server, *_args, **_kwargs):
            endpoints_seen.append(server.endpoint)
            if server.endpoint.endswith("8080/v1"):
                return BatchOutcome(
                    batch_number,
                    item_ids,
                    server,
                    None,
                    {},
                    0.01,
                    [],
                    "loading",
                    "unavailable",
                )
            result = {}
            for item_id in item_ids:
                result[item_id] = {
                    "title": f"Track {item_id + 1}",
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
            return BatchOutcome(
                batch_number, item_ids, server, result, {}, 0.01, [], None
            )

        with tempfile.TemporaryDirectory() as directory:
            code, result = self.run_main_with_worker(
                directory,
                worker,
                ["http://127.0.0.1:8080/v1", "http://127.0.0.1:8081/v1"],
            )
        self.assertEqual(code, 0)
        self.assertEqual(
            endpoints_seen,
            ["http://127.0.0.1:8080/v1", "http://127.0.0.1:8081/v1"],
        )
        self.assertTrue(all(item["outputMetadata"]["artist"] == "Artist" for item in result))


if __name__ == "__main__":
    unittest.main()
