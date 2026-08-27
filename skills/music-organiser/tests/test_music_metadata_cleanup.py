import unittest

from populate_music_manifest import (
    clean_artist,
    clean_genre,
    clean_output_metadata,
    clean_title,
    casing_output_reasons,
    build_library_context_index,
    context_title_key,
    first_nested_date,
    ground_catalogue_fields,
    is_populated,
    is_processed,
    is_reviewed,
    library_context_for_item,
    preserve_supported_title_context,
    repair_mojibake,
    repair_misplaced_artist_title,
    suspicious_output_reasons,
)


def output(**values):
    result = {
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
    }
    result.update(values)
    return result


class MetadataCleanupTests(unittest.TestCase):
    def test_preserves_user_exclusion_without_marking_for_review(self):
        cleaned = clean_output_metadata(
            output(
                title="Fixture MC Rap",
                artist="Fixture MC",
                needsReview=True,
                reviewReason="Previously unresolved.",
                excluded=True,
                exclusionReason="Intentionally excluded by user.",
            )
        )

        self.assertTrue(cleaned["excluded"])
        self.assertEqual(cleaned["exclusionReason"], "Intentionally excluded by user.")
        self.assertFalse(cleaned["needsReview"])
        self.assertIsNone(cleaned["reviewReason"])

    def test_repairs_cp1251_mojibake_without_touching_accented_names(self):
        self.assertEqual(repair_mojibake("Òåñòîâàÿ Ïåñíÿ 2"), "Тестовая Песня 2")
        self.assertEqual(
            repair_mojibake("Òåñòîâûé Ñáîðíèê Vol.79"),
            "Тестовый Сборник Vol.79",
        )
        self.assertEqual(repair_mojibake("Renée"), "Renée")
        self.assertEqual(repair_mojibake("Guðrún"), "Guðrún")

    def test_removes_promotional_and_technical_noise(self):
        self.assertEqual(
            clean_title("Track Name [FREE DL] - 320kbps", "Artist"),
            "Track Name",
        )
        self.assertEqual(clean_title("Track Name (YouTube Rip)", "Artist"), "Track Name")
        self.assertEqual(clean_title("Track Name - 128 BPM", "Artist"), "Track Name")

    def test_removes_dj_key_but_preserves_version(self):
        self.assertEqual(
            clean_title("'Fixture Tune' - (Test Remix) - 5A", "Fixture Artist"),
            "Fixture Tune (Test Remix)",
        )
        self.assertEqual(clean_title("Song (Original Mix)", "Artist"), "Song (Original Mix)")
        self.assertEqual(clean_title("Song (VIP)", "Artist"), "Song (VIP)")
        self.assertEqual(
            clean_title("Fixture Song (DIY Acapella) - 10A/4A", "Artist"),
            "Fixture Song (DIY Acapella)",
        )
        self.assertEqual(
            clean_title("Fixture Track (Original Mix) - 1A_5A-1-1", "Artist"),
            "Fixture Track (Original Mix)",
        )
        self.assertEqual(clean_title("Fixture One - 9A-1-1", "Fixture Artist"), "Fixture One")
        self.assertEqual(clean_title("Fixture Two (3B/3A)", "Fixture Artist"), "Fixture Two")
        self.assertEqual(clean_title("Fixture Three - 11A-1-1", "Fixture Artist"), "Fixture Three")
        self.assertEqual(clean_title("Fixture Four (3A-1-1)", "Fixture Artist"), "Fixture Four")
        self.assertEqual(
            clean_title(
                "Fixture Mashup (Fixture Artist - 3B)",
                "Fixture Artist",
            ),
            "Fixture Mashup",
        )

    def test_restores_explicit_version_context_dropped_by_model(self):
        item = {
            "source": r"fixtures\UnknownArtist\UnknownAlbum\Fixture Artist - Fixture Song (Radio Edit).ogg",
            "originalMetadata": {
                "tags": {
                    "TIT2": {"text": ["Fixture Artist - Fixture Song (Radio Edit)"]},
                }
            },
        }
        repaired = preserve_supported_title_context(
            output(title="Fixture Song", artist="Fixture Artist"), item
        )
        self.assertEqual(repaired["title"], "Fixture Song (Radio Edit)")

    def test_restores_version_from_matching_album_when_title_tag_is_plain(self):
        item = {
            "source": r"fixtures\Fixture Artist\Fixture Song (Test Remix)\Fixture Artist - Fixture Song.ogg",
            "originalMetadata": {
                "tags": {
                    "TIT2": {"text": ["Fixture Song"]},
                    "TALB": {"text": ["Fixture Song (Test Remix)"]},
                }
            },
        }
        repaired = preserve_supported_title_context(
            output(
                title="Fixture Song",
                artist="Fixture Artist",
                album="Fixture Song (Test Remix)",
            ),
            item,
        )
        self.assertEqual(repaired["title"], "Fixture Song (Test Remix)")

    def test_does_not_replace_title_for_unrelated_context_evidence(self):
        item = {
            "source": r"fixtures\Artist\Album\Different Track (VIP).ogg",
            "originalMetadata": {
                "tags": {"TIT2": {"text": ["Different Track (VIP)"]}}
            },
        }
        cleaned = preserve_supported_title_context(
            output(title="Actual Track", artist="Artist"), item
        )
        self.assertEqual(cleaned["title"], "Actual Track")

    def test_removes_production_and_catalogue_suffix(self):
        self.assertEqual(
            clean_title(
                "01 Fixture Artist - Fixture Song MASTER TEST001",
                "Fixture Artist",
            ),
            "Fixture Song",
        )

    def test_normalizes_feature_credit_and_rejects_social_genre(self):
        self.assertEqual(clean_artist("Fixture Artist Feat Guest"), "Fixture Artist feat. Guest")
        self.assertEqual(
            clean_artist("Fixture Artist & Guest One & Guest Two"),
            "Fixture Artist & Guest One & Guest Two",
        )
        self.assertEqual(
            clean_artist("FIXTURE ACT X GUEST ONE & GUEST TWO"),
            "FIXTURE ACT X GUEST ONE & GUEST TWO",
        )
        self.assertEqual(clean_artist("T.E.S.T."), "T.E.S.T")
        self.assertEqual(clean_artist("@fixture"), "@fixture")
        self.assertEqual(clean_artist("[ uploader123 ] Fixture Act"), "Fixture Act")
        self.assertEqual(
            clean_artist("Fixture Act - ( feat. Guest One & Guest Two )"),
            "Fixture Act feat. Guest One & Guest Two",
        )
        self.assertEqual(
            clean_artist("Fixture MC feat. Guest (Produced by Test Producer)"),
            "Fixture MC feat. Guest",
        )
        self.assertIsNone(clean_genre("@FixtureLabelOfficial"))

    def test_normalizes_numeric_id3_genre_codes(self):
        self.assertEqual(clean_genre("(13)"), "Pop")
        self.assertEqual(clean_genre("13"), "Pop")
        self.assertIsNone(clean_genre("(255)"))

    def test_removes_domain_handle_bpm_and_technical_contamination(self):
        self.assertIsNone(clean_output_metadata(output(title="Track", artist="Artist", album="[fixture.info]"))["album"])
        self.assertEqual(
            clean_output_metadata(
                output(
                    title="Track",
                    artist="Artist",
                    album="Fixture Label 2015 Holiday Pack ( | @FixtureMedia)",
                )
            )["album"],
            "Fixture Label 2015 Holiday Pack",
        )
        self.assertIsNone(
            clean_output_metadata(
                output(title="Track", artist="Artist", album=">Album Title Goes Here<")
            )["album"]
        )
        self.assertEqual(
            clean_title("Fixture Song (128 BPM Instrumental Mix)", "Fixture Artist"),
            "Fixture Song (Instrumental Mix)",
        )
        self.assertEqual(
            clean_title("Fixture Dance Track (100-92bpm)", "Artist"),
            "Fixture Dance Track",
        )
        self.assertEqual(
            clean_title("Fixture-Track-Remix-demo-7-mp3-320", "Artist"),
            "Fixture-Track-Remix-demo-7",
        )
        self.assertEqual(clean_title("Fixture One [fixture.example.com Exclusive]", "Artist"), "Fixture One")
        self.assertEqual(clean_title("Fixture Two (Premiere)", "Artist"), "Fixture Two")
        self.assertEqual(
            clean_title("Fixture Song (Prod. by Test Producer) (2o1o)", "Fixture Artist"),
            "Fixture Song",
        )
        self.assertEqual(
            clean_title("Fixture Song (1997)", "Fixture Artist"),
            "Fixture Song (1997)",
        )
        self.assertEqual(
            clean_title("Fixture People (Club Mix DRM)", "Artist"),
            "Fixture People (Club Mix)",
        )
        self.assertEqual(
            clean_title("Fixture Song Prod By Test Producer Bonus", "Fixture Artist"),
            "Fixture Song",
        )
        self.assertEqual(
            clean_title("fixture_artist_-_fixture_song_(acapella)", "Fixture Artist"),
            "fixture song (acapella)",
        )
        self.assertEqual(
            clean_title(
                "Fixture Artist ft Guest One vs. Guest Two - Fixture Mashup",
                "Fixture Artist feat. Guest One vs. Guest Two",
            ),
            "Fixture Mashup",
        )
        self.assertIsNone(
            clean_output_metadata(
                output(title="Fixture Song", artist="Fixture Artist", album="Fixture Artist [via EDM.com]")
            )["album"]
        )
        self.assertTrue(
            clean_output_metadata(output(title="Track", artist="TBA"))["needsReview"]
        )

    def test_flags_populated_output_that_still_contains_cleanup_noise(self):
        item = {
            "originalMetadata": {},
            "outputMetadata": output(
                title="Fixture Song (DIY Acapella) - 10A/4A",
                artist="Artist",
            ),
        }
        self.assertEqual(suspicious_output_reasons(item), ["title"])

        item["outputMetadata"]["title"] = "Fixture Song (DIY Acapella)"
        self.assertEqual(suspicious_output_reasons(item), [])

    def test_flags_correlated_display_casing_problems(self):
        dirty = output(
            title="melodic edm",
            artist="fixture artist",
            albumArtist="fixture artist",
            album="cc0 test fixtures",
        )
        self.assertEqual(
            casing_output_reasons(dirty),
            [
                "artist casing",
                "albumArtist casing",
                "title casing",
                "album casing",
            ],
        )
        self.assertEqual(
            casing_output_reasons(
                output(
                    title="Melodic EDM",
                    artist="Fixture Artist",
                    albumArtist="Fixture Artist",
                    album="CC0 Test Fixtures",
                )
            ),
            [],
        )
        self.assertEqual(
            casing_output_reasons(
                output(
                    title="Fixture +1 (feat. Guest)",
                    artist="Fixture Artist",
                    album="Fixture +1 (feat. Guest) - Single",
                )
            ),
            [],
        )
        self.assertEqual(
            casing_output_reasons(
                output(title="Fixture $ong", artist="Fixture Artist", album="Fixture $ong - Single")
            ),
            [],
        )
        self.assertEqual(
            casing_output_reasons(
                output(
                    title="Melodic edm Loop",
                    artist="Fixture Artist",
                    album="CC0 Test Fixtures",
                )
            ),
            ["title casing"],
        )
        self.assertEqual(
            casing_output_reasons(
                output(
                    title="80s Fixture",
                    artist="Fixture Artist",
                    album="CC0 Test Fixtures",
                )
            ),
            [],
        )

    def test_normalizes_nulls_and_loose_track_structure(self):
        cleaned = clean_output_metadata(
            output(
                title="Fixture Initials (Original Mix) [FREE DOWNLOAD]",
                artist="$fixture",
                genre="null",
                trackNumber=154,
                discNumber=2,
                albumArtist="$fixture-album-artist",
            )
        )
        self.assertEqual(cleaned["title"], "Fixture Initials (Original Mix)")
        self.assertEqual(cleaned["artist"], "$fixture")
        self.assertIsNone(cleaned["genre"])
        self.assertIsNone(cleaned["trackNumber"])
        self.assertIsNone(cleaned["discNumber"])
        self.assertIsNone(cleaned["albumArtist"])
        self.assertIsNone(clean_output_metadata(output(title="Track", artist="Artist", genre="Other"))["genre"])

    def test_review_reason_is_a_terminal_nonaccepted_result(self):
        item = {
            "outputMetadata": output(
                title="Fixture Release",
                artist="Fixture Artist & Guest",
                needsReview=True,
                reviewReason="Album/release membership is ambiguous.",
            )
        }
        self.assertTrue(is_reviewed(item))
        self.assertTrue(is_processed(item))
        self.assertFalse(is_populated(item))

    def test_title_matched_library_context_excludes_placeholders(self):
        accepted = {
            "source": r"fixtures\Fupi - Melodic EDM.ogg",
            "outputMetadata": output(
                title="Melodic EDM (Vocal Edit)",
                artist="Fupi",
            ),
        }
        pending = {
            "source": r"fixtures\01 Melodic EDM.ogg",
            "outputMetadata": output(
                title="Melodic EDM",
                artist=None,
                needsReview=True,
                reviewReason="Artist is unknown.",
            ),
        }
        index = build_library_context_index([accepted, pending])
        context = library_context_for_item(pending, index)
        self.assertEqual(context[0]["artist"], "Fupi")
        self.assertEqual(
            context_title_key("Melodic EDM (Vocal Edit)"),
            "melodic edm",
        )
        self.assertIsNone(context_title_key("Track 01"))

    def test_missing_core_identity_is_automatically_reviewed(self):
        cleaned = clean_output_metadata(output(title="4A", artist=None))
        self.assertIsNotNone(cleaned["reviewReason"])

    def test_removes_urls_and_filename_indexes(self):
        self.assertEqual(
            clean_title("01 - Track Name [www.example.com]", "Artist"),
            "Track Name",
        )
        self.assertEqual(clean_title("6-9", "Artist"), "6-9")
        self.assertEqual(clean_title("30 PM", "Artist"), "30 PM")
        self.assertEqual(clean_title("2 (Fixture DJ Remix)", "Artist"), "2 (Fixture DJ Remix)")
        self.assertEqual(clean_title("01 Fixture Artist - Track", "Fixture Artist"), "Track")

    def test_repairs_artist_title_stored_in_artist_tag(self):
        item = {
            "originalMetadata": {
                "tags": {
                    "TIT2": {"text": ["(Test Style Remix)"]},
                    "TPE1": {"text": ["Fixture Artist - Fixture Song"]},
                }
            }
        }
        repaired = repair_misplaced_artist_title(
            clean_output_metadata(
                output(
                    title="(Test Style Remix) - Fixture Art",
                    artist="Fixture Artist",
                    date="2010",
                    genre="House House",
                )
            ),
            item,
        )
        self.assertEqual(repaired["artist"], "Fixture Artist")
        self.assertEqual(repaired["title"], "Fixture Song (Test Style Remix)")
        self.assertEqual(repaired["genre"], "House")

    def test_repairs_dj_key_returned_as_title(self):
        item = {
            "originalMetadata": {
                "tags": {
                    "TIT2": {"text": ["Fixture Song - 2B"]},
                    "TKEY": {"text": ["7m"]},
                    "TPE1": {"text": ["Fixture Artist"]},
                }
            }
        }
        repaired = repair_misplaced_artist_title(
            clean_output_metadata(
                output(
                    title="2B",
                    artist="Fixture Song",
                    albumArtist="Fixture Artist",
                    album="CC0 Test Fixtures",
                    trackNumber=2,
                )
            ),
            item,
        )
        self.assertEqual(repaired["title"], "Fixture Song")
        self.assertEqual(repaired["artist"], "Fixture Artist")
        self.assertFalse(repaired["needsReview"])

    def test_catalogue_fields_must_be_grounded_in_original_tags(self):
        item = {
            "originalMetadata": {
                "tags": {
                    "TDRC": {"text": [{"year": 2010, "month": 1, "day": 1}]},
                    "TCON": {"text": ["House House"]},
                }
            }
        }
        grounded = ground_catalogue_fields(
            clean_output_metadata(
                output(
                    title="Track",
                    artist="Artist",
                    album="Invented Album",
                    date="2024",
                    trackNumber=99,
                    genre="Invented Genre",
                )
            ),
            item,
        )
        self.assertIsNone(grounded["album"])
        self.assertIsNone(grounded["trackNumber"])
        self.assertEqual(grounded["date"], "2010")
        self.assertEqual(grounded["genre"], "House")
        self.assertEqual(
            first_nested_date({"year": 2010, "month": 2, "day": 3}),
            "2010-02-03",
        )

    def test_drops_model_album_artist_without_source_album_artist_tag(self):
        item = {
            "originalMetadata": {
                "tags": {
                    "TALB": {"text": ["Album"]},
                    "TPE1": {"text": ["Track Artist"]},
                }
            }
        }
        grounded = ground_catalogue_fields(
            clean_output_metadata(
                output(
                    title="Track",
                    artist="Track Artist",
                    album="Album",
                    albumArtist="Track Artist",
                )
            ),
            item,
        )
        self.assertIsNone(grounded["albumArtist"])

    def test_reads_nested_mp4_track_position(self):
        item = {
            "originalMetadata": {
                "tags": {
                    "©alb": ["CC0 Test Fixtures"],
                    "aART": ["Fixture Artist"],
                    "trkn": [[1, 16]],
                }
            }
        }
        grounded = ground_catalogue_fields(
            clean_output_metadata(
                output(
                    title="Melodic EDM",
                    artist="Fixture Artist",
                    albumArtist="Fixture Artist",
                    album="CC0 Test Fixtures",
                )
            ),
            item,
        )
        self.assertEqual(grounded["trackNumber"], 1)
        self.assertEqual(grounded["album"], "CC0 Test Fixtures")

    def test_review_can_refuse_ambiguous_release_fields(self):
        item = {
            "originalMetadata": {
                "tags": {
                    "TALB": {"text": ["Fixture Release"]},
                    "TDRC": {"text": [{"year": 2013, "month": 1, "day": 1}]},
                    "TCON": {"text": ["Trance"]},
                    "TRCK": {"text": ["1"]},
                }
            }
        }
        grounded = ground_catalogue_fields(
            clean_output_metadata(
                output(
                    title="Fixture Release",
                    artist="Fixture Artist & Guest",
                    album=None,
                    date=None,
                    trackNumber=None,
                    genre="Trance",
                    needsReview=True,
                    reviewReason="Album/release membership is ambiguous.",
                )
            ),
            item,
        )
        self.assertIsNone(grounded["album"])
        self.assertIsNone(grounded["trackNumber"])
        self.assertIsNone(grounded["date"])
        self.assertEqual(grounded["genre"], "Trance")

    def test_compilation_volume_does_not_use_track_artist_as_album_artist(self):
        item = {
            "originalMetadata": {
                "tags": {
                    "TALB": {"text": ["CC0 Test Compilation Vol.01 [WAV]"]},
                    "TDRC": {"text": [{"year": 2013}]},
                    "TPE1": {"text": ["Fupi"]},
                }
            }
        }
        grounded = ground_catalogue_fields(
            clean_output_metadata(
                output(
                    title="Melodic EDM MASTER TEST001",
                    artist="Fupi",
                    albumArtist="Fupi",
                    album="CC0 Test Compilation Vol.01",
                    date="2013",
                )
            ),
            item,
        )
        self.assertEqual(grounded["title"], "Melodic EDM")
        self.assertEqual(grounded["albumArtist"], "Various Artists")
        self.assertTrue(grounded["compilation"])


if __name__ == "__main__":
    unittest.main()
