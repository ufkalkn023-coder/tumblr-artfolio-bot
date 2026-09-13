"""
test_quality_threshold.py - Kalite eşiği politikası regresyon testleri.

Üretim eşiği tek bir yerde tanımlanır (config.MINIMUM_QUALITY_SCORE).
Bu testler eşiğin yanlışlıkla kaymasını (drift) engeller:
burada asla `assert score >= config.MINIMUM_QUALITY_SCORE` kalıbıyla
kendi kendini doğrulayan test yazılmaz; politika değeri (80) açıkça
belirtilir veya config bilinçli olarak patch'lenir.
"""

import importlib
import os
import unittest
from unittest.mock import patch

import config
from museum_api import Artwork, MuseumAPIClient, ScoringTelemetry

POLICY_THRESHOLD = 80


def make_artwork(artwork_id, score):
    return Artwork(
        museum="met", id=artwork_id, title="Test", artist="Artist",
        artist_bio="Artist", date="1900", image_url="https://example.com/image.jpg",
        original_source_url="https://example.com/artwork", museum_name="Test Museum",
        location_info="Gallery", dimensions="", medium_type="Painting",
        raw_medium="Oil on canvas", score=score,
        is_public_domain=True,
    )


class TestQualityThresholdPolicy(unittest.TestCase):

    def test_config_default_threshold_is_80(self):
        """Varsayılan üretim eşiği 80'dir (ortam değişkeni ayarlı olmasa bile)."""
        env_without_override = {
            key: value for key, value in os.environ.items()
            if key != "MINIMUM_QUALITY_SCORE"
        }
        try:
            with patch.dict(os.environ, env_without_override, clear=True):
                reloaded_config = importlib.reload(config)
            self.assertEqual(reloaded_config.MINIMUM_QUALITY_SCORE, POLICY_THRESHOLD)
        finally:
            importlib.reload(config)

    def test_config_rejects_out_of_range_threshold(self):
        """0-100 dışındaki eşik değerleri startup'ta ValueError üretir."""
        try:
            with patch.dict(os.environ, {"MINIMUM_QUALITY_SCORE": "101"}):
                with self.assertRaises(ValueError):
                    importlib.reload(config)
        finally:
            importlib.reload(config)

    def test_score_79_is_rejected(self):
        """79 puan varsayılan 80 eşiğinin altında kalır ve uygun sayılmaz."""
        telemetry = ScoringTelemetry()
        telemetry.record_scored(
            "met", "Oil on canvas", "Paintings", "Painting",
            "Unknown Artist", 79, False, False, False,
        )
        self.assertEqual(telemetry.sources["met"]["eligible"], 0)

    def test_score_80_is_accepted(self):
        """80 puan varsayılan eşiği tam olarak karşılar ve uygundur."""
        telemetry = ScoringTelemetry()
        telemetry.record_scored(
            "met", "Oil on canvas", "Paintings", "Painting",
            "Unknown Artist", 80, False, False, False,
        )
        self.assertEqual(telemetry.sources["met"]["eligible"], 1)

    def test_score_81_is_accepted(self):
        """81 puan varsayılan eşiğin üzerindedir ve uygundur."""
        telemetry = ScoringTelemetry()
        telemetry.record_scored(
            "met", "Oil on canvas", "Paintings", "Painting",
            "Unknown Artist", 81, False, False, False,
        )
        self.assertEqual(telemetry.sources["met"]["eligible"], 1)

    def test_telemetry_eligibility_follows_patched_config(self):
        """Telemetri uygunluğu sabit bir sayı değil, config değerini okur."""
        telemetry = ScoringTelemetry()
        with patch.object(config, "MINIMUM_QUALITY_SCORE", 90):
            telemetry.record_scored(
                "met", "Oil on canvas", "Paintings", "Painting",
                "Unknown Artist", 85, False, False, False,
            )
        self.assertEqual(telemetry.sources["met"]["eligible"], 0)
        with patch.object(config, "MINIMUM_QUALITY_SCORE", 80):
            telemetry.record_scored(
                "met", "Oil on canvas", "Paintings", "Painting",
                "Unknown Artist", 85, False, False, False,
            )
        self.assertEqual(telemetry.sources["met"]["eligible"], 1)

    def _configure_single_source_client(self, artwork):
        client = MuseumAPIClient()

        def fetch(_posted_ids, _target_medium):
            client.last_fetch_stats = {
                "source": "met", "candidates": 1, "duplicates": 0,
                "rejected_image": 0, "rejected_quality": 0,
                "rejected_other": 0, "eligible": int(artwork is not None),
            }
            return artwork

        client.fetch_met_artwork = fetch
        for source in ("aic", "cma", "smk", "harvard"):
            setattr(client, f"fetch_{source}_artwork",
                    lambda _posted_ids, _target_medium: None)
        return client

    def test_selection_rejects_artwork_below_patched_threshold(self):
        """Seçim mantığı eşiği config'ten okur: 85 puan, eşik 90 iken elenir."""
        client = self._configure_single_source_client(make_artwork("below-threshold", 85))

        with patch.object(config, "MINIMUM_QUALITY_SCORE", 90), \
                patch("random.shuffle", side_effect=lambda values: None):
            result = client.get_random_artwork({})

        self.assertIsNone(result)

    def test_selection_accepts_artwork_at_patched_threshold(self):
        """Seçim mantığı düşürülen eşiği de config'ten okur: 75 puan, eşik 70 iken seçilir."""
        artwork = make_artwork("above-threshold", 75)
        client = self._configure_single_source_client(artwork)

        with patch.object(config, "MINIMUM_QUALITY_SCORE", 70), \
                patch("random.shuffle", side_effect=lambda values: None):
            result = client.get_random_artwork({})

        self.assertIsNotNone(result)
        self.assertEqual(result.id, "above-threshold")


if __name__ == "__main__":
    unittest.main()
