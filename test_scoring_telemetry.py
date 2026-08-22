import unittest
import random
import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import config
from main import export_scoring_telemetry
from museum_api import Artwork, MuseumAPIClient, ScoringTelemetry


class TestScoringTelemetry(unittest.TestCase):
    def test_source_and_score_band_aggregation(self):
        telemetry = ScoringTelemetry()
        telemetry.record_duplicate("aic", 2)
        telemetry.record_evaluated("aic")
        telemetry.record_scored("aic", "Oil on canvas", "Paintings", "Painting", "Leonardo da Vinci", 65, False, False, True)
        telemetry.record_evaluated("aic")
        telemetry.record_scored("aic", "Oil on canvas", "Paintings", "Painting", "Unknown Artist", 85, True, True, True)

        bucket = telemetry.sources["aic"]
        self.assertEqual(bucket["evaluated"], 2)
        self.assertEqual(bucket["scored"], 2)
        self.assertEqual(bucket["duplicates"], 2)
        self.assertEqual(bucket["eligible"], 2)
        self.assertEqual(bucket["min"], 65)
        self.assertEqual(bucket["max"], 85)
        self.assertEqual(bucket["bands"]["60-79"], 1)
        self.assertEqual(bucket["bands"]["80-100"], 1)

    def test_category_artist_and_flag_aggregation(self):
        telemetry = ScoringTelemetry()
        telemetry.record_evaluated("met")
        telemetry.record_scored("met", "Marble", "Sculpture", "Sculpture", "Unknown Artist", 55, True, False, False)
        telemetry.record_evaluated("met")
        telemetry.record_scored("met", "Marble", "Sculpture", "Sculpture", "Auguste Rodin", 90, True, True, True)

        sculpture = telemetry.categories["Sculpture"]
        self.assertEqual(sculpture["evaluated"], 2)
        self.assertEqual(sculpture["eligible"], 1)
        self.assertEqual(sculpture["score_sum"], 145)
        self.assertEqual(telemetry.artists["unknown"]["evaluated"], 1)
        self.assertEqual(telemetry.artists["unknown"]["eligible"], 0)
        self.assertEqual(telemetry.artists["known"]["eligible"], 1)
        self.assertEqual(telemetry.flags["highlight"]["evaluated"], 2)
        self.assertEqual(telemetry.flags["on_view"]["evaluated"], 1)
        self.assertEqual(telemetry.flags["additional_images"]["evaluated"], 1)

    def test_empty_telemetry_is_reported_without_failure(self):
        telemetry = ScoringTelemetry()
        logger = Mock()

        telemetry.log(logger)

        logger.info.assert_called_once_with("selection_path_stats empty=true")

    def test_selection_telemetry_does_not_change_selected_artwork(self):
        def make_artwork(artwork_id):
            return Artwork(
                museum="met", id=artwork_id, title="Test", artist="Artist",
                artist_bio="Artist", date="1900", image_url="https://example.com/image.jpg",
                original_source_url="https://example.com/object", museum_name="Test Museum",
                location_info="Gallery", dimensions="", medium_type="Painting", raw_medium="Painting",
                score=65, style_or_era="",
            )

        def configure(client):
            def fetch(_posted_ids, _target_medium):
                client.last_fetch_stats = {
                    "source": "met", "candidates": 1, "duplicates": 0,
                    "rejected_image": 0, "rejected_quality": 0, "rejected_other": 0, "eligible": 1,
                }
                return make_artwork("same-selection")

            client.fetch_met_artwork = fetch
            for source in ("aic", "cma", "smk", "harvard"):
                setattr(client, f"fetch_{source}_artwork", lambda _posted_ids, _target_medium: None)

        enabled = MuseumAPIClient()
        disabled = MuseumAPIClient()
        configure(enabled)
        configure(disabled)

        class NoopTelemetry:
            def record_attempt(self, *_args): pass
            def record_first_eligible(self, *_args): pass
            def record_selected(self, *_args): pass

        disabled.scoring_telemetry = NoopTelemetry()
        with patch("museum_api.random.shuffle", side_effect=lambda values: None):
            enabled_result = enabled.get_random_artwork({})
            disabled_result = disabled.get_random_artwork({})

        self.assertEqual(enabled_result.id, disabled_result.id)

    def test_full_materialized_pool_includes_candidates_after_first_eligible(self):
        client = MuseumAPIClient()
        response = Mock(status_code=200)
        response.json.return_value = {
            "data": [
                {
                    "id": 1, "title": "Low", "artist_display": "Unknown Artist", "date_display": "",
                    "image_id": "low", "artwork_type_title": "Painting", "medium_display": "Oil on canvas",
                    "classification_title": "Paintings",
                },
                {
                    "id": 2, "title": "Eligible", "artist_display": "Leonardo da Vinci", "date_display": "1503",
                    "image_id": "eligible", "artwork_type_title": "Painting", "medium_display": "Oil on canvas",
                    "classification_title": "Paintings", "is_boosted": True, "is_on_view": True,
                },
                {
                    "id": 3, "title": "After", "artist_display": "Unknown Artist", "date_display": "",
                    "image_id": "after", "artwork_type_title": "Painting", "medium_display": "Oil on canvas",
                    "classification_title": "Paintings",
                },
            ]
        }
        client.session.post = Mock(return_value=response)

        with patch("museum_api.random.shuffle", side_effect=lambda values: None):
            artwork = client.fetch_aic_artwork(set())

        self.assertEqual(artwork.id, "2")
        self.assertEqual(client.pool_coverage["aic"]["coverage"], "full")
        self.assertEqual(client.pool_telemetry.sources["aic"]["evaluated"], 3)
        self.assertEqual(client.pool_telemetry.sources["aic"]["scored"], 3)

    def test_lazy_met_pool_is_partial_without_extra_detail_requests(self):
        client = MuseumAPIClient()
        client.session.get = Mock(side_effect=[
            Mock(status_code=200, json=lambda: {"objectIDs": [1, 2]}),
            Mock(status_code=200, json=lambda: {
                "isPublicDomain": True,
                "primaryImage": "https://example.com/one.jpg",
                "title": "One", "artistDisplayName": "Leonardo da Vinci", "objectDate": "1503",
                "medium": "Oil on canvas", "classification": "Paintings", "objectName": "Painting",
                "isHighlight": True, "additionalImages": [],
            }),
        ])

        with patch("museum_api.random.random", return_value=0.5), \
                patch("museum_api.random.choice", return_value="painting"), \
                patch("museum_api.random.shuffle", side_effect=lambda values: None):
            artwork = client.fetch_met_artwork(set())

        self.assertIsNotNone(artwork)
        self.assertEqual(client.session.get.call_count, 2)
        self.assertEqual(client.pool_coverage["met"]["coverage"], "partial")
        self.assertEqual(client.pool_coverage["met"]["materialized"], 1)

    def test_pool_telemetry_does_not_consume_random_state(self):
        client = MuseumAPIClient()
        candidates = [{
            "id": 1, "title": "Test", "artist_display": "Artist", "date_display": "1900",
            "image_id": "image", "artwork_type_title": "Painting", "medium_display": "Oil on canvas",
            "classification_title": "Paintings",
        }]
        state_before = random.getstate()
        client._record_aic_pool(candidates, set())
        state_after = random.getstate()
        self.assertEqual(state_before, state_after)

    def test_empty_pool_is_reported_without_failure(self):
        telemetry = ScoringTelemetry()
        logger = Mock()

        telemetry.log_pool(logger, {})

        logger.info.assert_called_once_with("pool_stats empty=true")

    def test_pool_logs_keep_dimensions_scoped_to_source(self):
        client = MuseumAPIClient()
        client._record_aic_pool([{
            "id": 1, "title": "Painting", "artist_display": "Known Artist", "date_display": "1900",
            "image_id": "image", "artwork_type_title": "Painting", "medium_display": "Oil on canvas",
            "classification_title": "Paintings",
        }], set())
        logger = Mock()

        client.pool_telemetry.log_pool(logger, client.pool_coverage)

        messages = [call.args[0] % call.args[1:] if len(call.args) > 1 else call.args[0] for call in logger.info.call_args_list]
        self.assertTrue(any(message.startswith("pool_stats source=aic coverage=full") for message in messages))
        self.assertTrue(any(message.startswith("pool_category_stats source=aic category=Painting") for message in messages))
        self.assertTrue(any(message.startswith("pool_artist_stats source=aic") for message in messages))

    def test_pool_source_dimensions_are_isolated_and_repeated_coverage_is_totaled(self):
        client = MuseumAPIClient()
        client._record_aic_pool([{
            "id": 1, "title": "Eligible AIC", "artist_display": "Leonardo da Vinci",
            "date_display": "1503", "image_id": "aic-image", "artwork_type_title": "Painting",
            "medium_display": "Oil on canvas", "classification_title": "Paintings",
            "is_boosted": True, "is_on_view": True,
        }], set())
        smk_artwork = {
            "object_number": "smk-1", "image_native": "https://example.com/smk.jpg",
            "titles": [{"title": "Low SMK Object"}], "production": [], "production_date": [],
            "techniques": ["Wood"], "object_names": [{"name": "Object"}], "on_display": False,
        }
        second_smk_artwork = dict(smk_artwork, object_number="smk-2")
        client._record_smk_pool([smk_artwork], set())
        client._record_smk_pool([second_smk_artwork], set())

        aic = client.pool_telemetry.sources["aic"]
        smk = client.pool_telemetry.sources["smk"]
        self.assertEqual(client.pool_coverage["aic"]["materialized"], 1)
        self.assertEqual(client.pool_coverage["smk"]["materialized"], 2)
        self.assertEqual(smk["evaluated"], 2)
        self.assertEqual(smk["scored"], 2)
        self.assertEqual(smk["eligible"], 0)
        self.assertEqual(aic["eligible"], 1)
        self.assertEqual(client.pool_telemetry.source_flags["aic"]["on_view"]["eligible"], 1)
        self.assertNotIn("on_view", client.pool_telemetry.source_flags.get("smk", {}))
        self.assertEqual(client.pool_telemetry.source_categories["smk"]["Object"]["eligible"], 0)

        logger = Mock()
        client.pool_telemetry.log_pool(logger, client.pool_coverage)
        messages = [call.args[0] % call.args[1:] if len(call.args) > 1 else call.args[0] for call in logger.info.call_args_list]
        self.assertIn("pool_flag_stats source=smk flag=on_view evaluated=0 eligible=0 avg=n/a", messages)

    def test_json_export_has_aggregate_schema_and_publish_status(self):
        client = MuseumAPIClient()
        client.scoring_telemetry.record_attempt("aic")
        client.scoring_telemetry.record_evaluated("aic")
        client.scoring_telemetry.record_first_eligible("aic", 80)
        client.scoring_telemetry.record_selected("aic", 80)
        client.pool_telemetry.record_duplicate("aic", 1)
        client._record_aic_pool([{
            "id": 1, "title": "Public title", "artist_display": "Public artist", "date_display": "1900",
            "image_id": "image", "artwork_type_title": "Painting", "medium_display": "Oil on canvas",
            "classification_title": "Paintings",
        }], {"9"})

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "scoring.json"
            client.write_scoring_telemetry(path, publish_success=True, run_timestamp="2026-08-22T10:00:00+00:00")
            with path.open(encoding="utf-8") as telemetry_file:
                payload = json.load(telemetry_file)

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["run_timestamp"], "2026-08-22T10:00:00+00:00")
        self.assertEqual(payload["publish"], {"success": True})
        self.assertEqual(payload["selection_path"]["selected_score"], 80)
        self.assertIn("aic", payload["pool"]["sources"])
        self.assertIn("score_bands", payload["pool"]["sources"]["aic"])
        self.assertIn("primary_category_stats", payload["pool"]["sources"]["aic"])
        self.assertIn("artist_stats", payload["pool"]["sources"]["aic"])

    def test_empty_export_and_partial_full_coverage_are_serialized(self):
        client = MuseumAPIClient()
        empty_payload = client.build_scoring_telemetry_export(publish_success=False, run_timestamp="timestamp")
        self.assertEqual(empty_payload["selection_path"]["sources"], {})
        self.assertEqual(empty_payload["pool"]["sources"], {})

        client.pool_telemetry.record_duplicate("met", 1)
        client.pool_coverage["met"] = {"coverage": "partial", "materialized": 2}
        client._record_aic_pool([{
            "id": 1, "title": "Painting", "artist_display": "Artist", "date_display": "1900",
            "image_id": "image", "artwork_type_title": "Painting", "medium_display": "Oil on canvas",
            "classification_title": "Paintings",
        }], set())
        payload = client.build_scoring_telemetry_export(publish_success=None, run_timestamp="timestamp")
        self.assertEqual(payload["pool"]["sources"]["met"]["coverage"], "partial")
        self.assertEqual(payload["pool"]["sources"]["met"]["materialized"], 2)
        self.assertEqual(payload["pool"]["sources"]["aic"]["coverage"], "full")

    def test_export_does_not_include_configured_secrets(self):
        original_token = config.TUMBLR_OAUTH_TOKEN
        original_key = config.TUMBLR_CONSUMER_KEY
        try:
            config.TUMBLR_OAUTH_TOKEN = "sensitive-test-token"
            config.TUMBLR_CONSUMER_KEY = "sensitive-test-key"
            payload = MuseumAPIClient().build_scoring_telemetry_export(publish_success=True, run_timestamp="timestamp")
        finally:
            config.TUMBLR_OAUTH_TOKEN = original_token
            config.TUMBLR_CONSUMER_KEY = original_key

        serialized = json.dumps(payload)
        self.assertNotIn("sensitive-test-token", serialized)
        self.assertNotIn("sensitive-test-key", serialized)
        self.assertNotIn("Authorization", serialized)

    def test_export_failure_is_best_effort(self):
        class FailingClient:
            def write_scoring_telemetry(self, *_args, **_kwargs):
                raise OSError("secret must not be logged")

        with tempfile.TemporaryDirectory() as temp_dir:
            export_scoring_telemetry(FailingClient(), publish_success=True, output_dir=Path(temp_dir) / "telemetry")


if __name__ == "__main__":
    unittest.main()
