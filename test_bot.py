"""
test_bot.py - 85/100 Kalite Puanlama Sistemi, Müze API'leri ve Formatlama Testleri
"""

import unittest
from museum_api import MuseumAPIClient, Artwork, ArtworkScorer, MINIMUM_QUALITY_SCORE
from tumblr_poster import TumblrPoster
import config


class TestTumblrBot(unittest.TestCase):

    def setUp(self):
        self.museum_client = MuseumAPIClient()

    def test_scoring_system_rules(self):
        """Puanlama sisteminin 85/100 eşiğini doğru uyguladığını test eder."""
        # 1. Başyapıt Resim (Mona Lisa - Da Vinci): 85+ almalı
        score_master, reason_master = ArtworkScorer.calculate_score(
            title="Mona Lisa",
            artist="Leonardo da Vinci",
            date_str="1503",
            raw_medium="Oil on poplar panel",
            classification="Paintings",
            object_name="Painting",
            image_url="https://example.com/highres_monalisa.jpg",
            is_highlight=True,
            has_additional_images=True,
            on_view=True
        )
        print(f"\n[Test Master Painting] {reason_master}")
        self.assertGreaterEqual(score_master, MINIMUM_QUALITY_SCORE)

        # 2. Ünlü Mermer Heykel (Michelangelo David veya Rodin): 85+ almalı
        score_sculpture, reason_sculpture = ArtworkScorer.calculate_score(
            title="The Thinker",
            artist="Auguste Rodin",
            date_str="1904",
            raw_medium="Bronze",
            classification="Sculpture",
            object_name="Sculpture",
            image_url="https://example.com/the_thinker.jpg",
            is_highlight=True,
            has_additional_images=True,
            on_view=True
        )
        print(f"[Test Master Sculpture] {reason_sculpture}")
        self.assertGreaterEqual(score_sculpture, MINIMUM_QUALITY_SCORE)

        # 3. Düşük Kalite / Fragman / Önemsiz Parça: 85'in altında kalmalı ve elenmeli
        score_fragment, reason_fragment = ArtworkScorer.calculate_score(
            title="Fragment of a jar rim",
            artist="Unknown Artist",
            date_str="Unknown Date",
            raw_medium="Terracotta fragment",
            classification="Ceramics",
            object_name="Vessel fragment",
            image_url="https://example.com/fragment.jpg",
            is_highlight=False,
            has_additional_images=False,
            on_view=False
        )
        print(f"[Test Low Quality Fragment] {reason_fragment}")
        self.assertLess(score_fragment, MINIMUM_QUALITY_SCORE)

    def test_met_api_fetch_with_score(self):
        """The Met API'sinden 85+ puanlı eser çekildiğini doğrular."""
        artwork = self.museum_client.fetch_met_artwork(posted_ids=set())
        if artwork:
            self.assertEqual(artwork.museum, "met")
            self.assertGreaterEqual(artwork.score, MINIMUM_QUALITY_SCORE)
            self.assertTrue(artwork.image_url.startswith("http"))
            print(f"\n[The Met 85+ Eser] {artwork.title} by {artwork.artist} [{artwork.medium_type}, Skor: {artwork.score}/100]")

    def test_aic_api_fetch_with_score(self):
        """AIC API'sinden 85+ puanlı eser çekildiğini doğrular."""
        artwork = self.museum_client.fetch_aic_artwork(posted_ids=set())
        if artwork:
            self.assertEqual(artwork.museum, "aic")
            self.assertGreaterEqual(artwork.score, MINIMUM_QUALITY_SCORE)
            self.assertTrue(artwork.image_url.startswith("https://www.artic.edu/iiif/2/"))
            print(f"\n[AIC 85+ Eser] {artwork.title} by {artwork.artist} [{artwork.medium_type}, Skor: {artwork.score}/100]")

    def test_cma_api_fetch_with_score(self):
        """CMA API'sinden 85+ puanlı eser çekildiğini doğrular."""
        artwork = self.museum_client.fetch_cma_artwork(posted_ids=set())
        if artwork:
            self.assertEqual(artwork.museum, "cma")
            self.assertGreaterEqual(artwork.score, MINIMUM_QUALITY_SCORE)
            self.assertTrue(artwork.image_url.startswith("http"))
            print(f"\n[CMA 85+ Eser] {artwork.title} by {artwork.artist} [{artwork.medium_type}, Skor: {artwork.score}/100]")

    def test_multi_medium_tag_generation(self):
        """Farklı türler (Heykel, Çizim, Resim, Obje) için etiketlerin tam 5 adet olduğunu test eder."""
        config.TUMBLR_CONSUMER_KEY = "mock_key"
        config.TUMBLR_CONSUMER_SECRET = "mock_secret"
        config.TUMBLR_OAUTH_TOKEN = "mock_token"
        config.TUMBLR_OAUTH_SECRET = "mock_token_secret"
        config.TUMBLR_BLOG_NAME = "artfolio-db.tumblr.com"

        poster = TumblrPoster()

        # Heykel Testi
        sculpture = Artwork(
            museum="met", id="1", title="Bust of Zeus", artist="Unknown Greek Master",
            date="4th Century BC", image_url="https://example.com/zeus.jpg",
            museum_name="The Met", medium_type="Sculpture", raw_medium="Marble",
            score=90, style_or_era="Classical Antiquity"
        )
        sculpture_tags = poster.generate_tags(sculpture)
        self.assertEqual(len(sculpture_tags), 5)
        self.assertIn("sculpture", sculpture_tags)
        print(f"\n[Heykel Etiketleri (5 adet)]: {sculpture_tags}")

        # Çizim Testi
        drawing = Artwork(
            museum="aic", id="2", title="Study of Hands", artist="Leonardo da Vinci",
            date="1490", image_url="https://example.com/hands.jpg",
            museum_name="AIC", medium_type="Drawing", raw_medium="Ink and silverpoint on paper",
            score=95, style_or_era="Renaissance"
        )
        drawing_tags = poster.generate_tags(drawing)
        self.assertEqual(len(drawing_tags), 5)
        self.assertIn("drawing", drawing_tags)
        print(f"[Çizim Etiketleri (5 adet)]: {drawing_tags}")


if __name__ == "__main__":
    unittest.main()
