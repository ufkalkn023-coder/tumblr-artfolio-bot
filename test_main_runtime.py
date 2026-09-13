import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import main
from publication_state import PublicationRecord


class TestMainRuntime(unittest.TestCase):
    def test_disabled_content_types_are_not_enabled(self):
        with patch.dict(main.config.CONTENT_WEIGHTS, {"Painting": 75, "Sculpture": 25, "Drawing": 0}, clear=True):
            self.assertTrue(main.is_content_type_enabled("Painting"))
            self.assertFalse(main.is_content_type_enabled("Drawing"))
            self.assertFalse(main.is_content_type_enabled("Object"))
            self.assertEqual(main.get_enabled_content_types(), ["Painting", "Sculpture"])

    def test_curation_attempts_fall_back_to_enabled_weighted_media(self):
        with patch.dict(main.config.CONTENT_WEIGHTS, {"Painting": 75, "Sculpture": 25, "Drawing": 0}, clear=True):
            self.assertEqual(
                main.build_curation_attempt_media("Sculpture"),
                ["Sculpture", "Painting", "Painting"],
            )
            self.assertEqual(
                main.build_curation_attempt_media("Painting"),
                ["Painting", "Sculpture", "Painting"],
            )
            with self.assertRaises(ValueError):
                main.build_curation_attempt_media("Drawing")

    def test_scheduled_theme_does_not_select_disabled_content_type(self):
        with patch.dict(main.config.CONTENT_WEIGHTS, {"Painting": 75, "Sculpture": 25, "Drawing": 0}, clear=True):
            self.assertEqual(main.apply_scheduled_medium_theme("Painting", weekday=0), "Sculpture")
            self.assertEqual(main.apply_scheduled_medium_theme("Painting", weekday=2), "Painting")

    def test_curation_cycle_can_read_time_at_startup(self):
        museum_client = Mock()
        museum_client.get_random_artwork.return_value = None
        museum_client.last_run_stats = {}

        # Gerçek publication_state.json'a bağımlılık yaratma: bozuk/ölçüm dışı
        # durum dosyası bu testin assert'lerini yanıltmasın (izole geçici dosya).
        with tempfile.TemporaryDirectory() as temp_dir, \
                patch.object(main.config, "PUBLICATION_STATE_FILE", Path(temp_dir) / "publication_state.json"), \
                patch("main.time.monotonic", side_effect=(100.0, 101.0)) as monotonic, \
                patch("main.time.sleep") as sleep, \
                patch("random.choices", return_value=["Painting"]), \
                patch("main.load_posted_ids", return_value={
                    "met": [], "aic": [], "cma": [], "rijksmuseum": [], "smk": [], "harvard": [],
                }), \
                patch("main.MuseumAPIClient", return_value=museum_client), \
                patch("main.export_scoring_telemetry"), \
                self.assertRaises(SystemExit) as exit_context:
            main.run_curation_cycle()

        self.assertEqual(exit_context.exception.code, 1)
        self.assertEqual(monotonic.call_count, 2)
        self.assertEqual(museum_client.get_random_artwork.call_count, 3)
        self.assertEqual([call.args[1] for call in museum_client.get_random_artwork.call_args_list], ["Painting", "Sculpture", "Painting"])
        self.assertEqual(sleep.call_count, 2)
        museum_client.log_scoring_telemetry.assert_called_once()

    def test_curation_cycle_passes_published_history_to_selection_client(self):
        history = [PublicationRecord(
            museum="met",
            artwork_id="old",
            status="published",
            reserved_at="2026-01-01T00:00:00+00:00",
            published_at="2026-01-01T00:01:00+00:00",
            artist_name="Claude Monet",
            artist_key="claude monet",
            medium_type="Painting",
        )]
        store = Mock()
        store.load.return_value = store
        store.active_blocks.return_value = {}
        store.get_recent_published.return_value = history
        museum_client = Mock()
        museum_client.get_random_artwork.return_value = None
        museum_client.last_run_stats = {}

        with patch("main.time.monotonic", side_effect=(100.0, 101.0)), \
                patch("main.time.sleep"), \
                patch("random.choices", return_value=["Painting"]), \
                patch("main.load_posted_ids", return_value={
                    "met": [], "aic": [], "cma": [], "rijksmuseum": [], "smk": [], "harvard": [],
                }), \
                patch("main.PublicationStateStore", return_value=store), \
                patch("main.MuseumAPIClient", return_value=museum_client) as constructor, \
                patch("main.export_scoring_telemetry"), \
                self.assertRaises(SystemExit):
            main.run_curation_cycle()

        constructor.assert_called_once_with(recent_published=history)
        store.get_recent_published.assert_called_once_with(limit=12)

    def test_curation_cycle_aggregates_diversity_stats_across_attempts(self):
        store = Mock()
        store.load.return_value = store
        store.active_blocks.return_value = {}
        store.get_recent_published.return_value = []
        museum_client = Mock()
        museum_client.get_random_artwork.return_value = None
        museum_client.last_run_stats = {
            "rejected_diversity": 1,
            "artist_cooldown": 1,
            "source_consecutive_limit": 0,
            "medium_consecutive_limit": 0,
        }

        with patch("main.time.monotonic", side_effect=(100.0, 101.0)), \
                patch("main.time.sleep"), \
                patch("random.choices", return_value=["Painting"]), \
                patch("main.load_posted_ids", return_value={
                    "met": [], "aic": [], "cma": [], "rijksmuseum": [], "smk": [], "harvard": [],
                }), \
                patch("main.PublicationStateStore", return_value=store), \
                patch("main.MuseumAPIClient", return_value=museum_client), \
                patch("main.export_scoring_telemetry"), \
                self.assertLogs("artfolio_bot", level="INFO") as captured, \
                self.assertRaises(SystemExit):
            main.run_curation_cycle()

        summary = next(line for line in captured.output if "run_summary " in line)
        self.assertIn("rejected_diversity=3", summary)
        self.assertIn("artist_cooldown=3", summary)


if __name__ == "__main__":
    unittest.main()
