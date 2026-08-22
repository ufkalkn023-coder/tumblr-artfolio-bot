import unittest
from unittest.mock import Mock, patch

import main


class TestMainRuntime(unittest.TestCase):
    def test_curation_cycle_can_read_time_at_startup(self):
        museum_client = Mock()
        museum_client.get_random_artwork.return_value = None
        museum_client.last_run_stats = {}

        with patch("main.time.monotonic", side_effect=(100.0, 101.0)) as monotonic, \
                patch("main.time.sleep"), \
                patch("main.load_posted_ids", return_value={
                    "met": [], "aic": [], "cma": [], "rijksmuseum": [], "smk": [], "harvard": [],
                }), \
                patch("main.MuseumAPIClient", return_value=museum_client), \
                patch("main.export_scoring_telemetry"), \
                self.assertRaises(SystemExit) as exit_context:
            main.run_curation_cycle()

        self.assertEqual(exit_context.exception.code, 1)
        self.assertEqual(monotonic.call_count, 2)
        museum_client.get_random_artwork.assert_called()
        museum_client.log_scoring_telemetry.assert_called_once()


if __name__ == "__main__":
    unittest.main()
