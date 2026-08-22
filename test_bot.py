"""
test_bot.py - 85/100 Kalite Puanlama Sistemi, Müze API'leri ve Formatlama Testleri
"""

import unittest
from unittest.mock import Mock, patch

import requests

from museum_api import MuseumAPIClient, Artwork, ArtworkScorer, MINIMUM_QUALITY_SCORE
from tumblr_poster import (
    TumblrPoster,
    artist_internal_tag,
    century_tag,
    generate_public_tags,
    normalize_artist_name,
    normalize_dimensions,
    normalize_medium_display,
)
import config
from http_requests import JSON_REQUEST_HEADERS, MAX_TRANSIENT_HTTP_ATTEMPTS


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class TestTumblrBot(unittest.TestCase):

    def setUp(self):
        self.museum_client = MuseumAPIClient()
        config.TUMBLR_CONSUMER_KEY = "mock_key"
        config.TUMBLR_CONSUMER_SECRET = "mock_secret"
        config.TUMBLR_OAUTH_TOKEN = "mock_token"
        config.TUMBLR_OAUTH_SECRET = "mock_token_secret"
        config.TUMBLR_BLOG_NAME = "artfolio-db.tumblr.com"

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
        """The Met fixture'ından geçerli ve yeterli puanlı eser seçilir."""
        self.museum_client.session.get = Mock(side_effect=[
            FakeResponse({"objectIDs": [123]}),
            FakeResponse({
                "isPublicDomain": True,
                "primaryImage": "https://example.com/mona-lisa.jpg",
                "title": "Mona Lisa",
                "artistDisplayName": "Leonardo da Vinci",
                "objectDate": "1503",
                "department": "European Paintings",
                "medium": "Oil on panel",
                "classification": "Paintings",
                "objectName": "Painting",
                "isHighlight": True,
                "additionalImages": ["detail.jpg"],
                "objectURL": "https://www.metmuseum.org/art/collection/search/123",
            }),
        ])

        artwork = self.museum_client.fetch_met_artwork(posted_ids=set())

        self.assertIsNotNone(artwork)
        self.assertEqual(artwork.museum, "met")
        self.assertGreaterEqual(artwork.score, MINIMUM_QUALITY_SCORE)
        self.assertTrue(artwork.image_url.startswith("http"))

    def test_aic_api_fetch_with_score(self):
        """AIC fixture'ından geçerli ve yeterli puanlı eser seçilir."""
        self.museum_client.session.post = Mock(return_value=FakeResponse({
            "data": [{
                "id": 456,
                "title": "The Test Painting",
                "artist_display": "Leonardo da Vinci",
                "date_display": "1503",
                "image_id": "test-image",
                "artwork_type_title": "Painting",
                "medium_display": "Oil on canvas",
                "classification_title": "Paintings",
                "is_boosted": True,
                "is_on_view": True,
                "style_title": "Renaissance",
            }]
        }))

        artwork = self.museum_client.fetch_aic_artwork(posted_ids=set())

        self.assertIsNotNone(artwork)
        self.assertEqual(artwork.museum, "aic")
        self.assertGreaterEqual(artwork.score, MINIMUM_QUALITY_SCORE)
        self.assertTrue(artwork.image_url.startswith("https://www.artic.edu/iiif/2/"))

    def test_aic_metadata_request_retries_transient_403_with_json_headers(self):
        blocked = Mock(status_code=403)
        success = FakeResponse({
            "data": [{
                "id": 456, "title": "The Test Painting", "artist_display": "Leonardo da Vinci",
                "date_display": "1503", "image_id": "test-image", "artwork_type_title": "Painting",
                "medium_display": "Oil on canvas", "classification_title": "Paintings",
                "is_boosted": True, "is_on_view": True,
            }]
        })
        self.museum_client.session.post = Mock(side_effect=[blocked, success])

        with patch("http_requests.time.sleep") as sleep, \
                patch("museum_api.random.shuffle", side_effect=lambda values: None):
            artwork = self.museum_client.fetch_aic_artwork(set())

        self.assertIsNotNone(artwork)
        self.assertEqual(self.museum_client.session.post.call_count, 2)
        self.assertEqual(self.museum_client.session.post.call_args.kwargs["headers"], JSON_REQUEST_HEADERS)
        sleep.assert_called_once()

    def test_aic_metadata_persistent_403_fails_cleanly_after_bounded_retries(self):
        blocked_responses = [Mock(status_code=403) for _ in range(MAX_TRANSIENT_HTTP_ATTEMPTS)]
        self.museum_client.session.post = Mock(side_effect=blocked_responses)

        with patch("http_requests.time.sleep") as sleep:
            artwork = self.museum_client.fetch_aic_artwork(set())

        self.assertIsNone(artwork)
        self.assertEqual(self.museum_client.session.post.call_count, MAX_TRANSIENT_HTTP_ATTEMPTS)
        self.assertEqual(sleep.call_count, MAX_TRANSIENT_HTTP_ATTEMPTS - 1)

    def test_cma_api_fetch_with_score(self):
        """CMA fixture'ından geçerli ve yeterli puanlı eser seçilir."""
        self.museum_client.session.get = Mock(return_value=FakeResponse({
            "data": [{
                "id": 789,
                "title": "The Test Sculpture",
                "creators": [{"description": "Auguste Rodin (French, 1840-1917)"}],
                "creation_date": "1904",
                "technique": "Bronze",
                "type": "Sculpture",
                "department": "Sculpture",
                "culture": ["French"],
                "images": {"web": {"url": "https://example.com/thinker.jpg"}},
                "current_location": "Gallery 1",
                "share_license_status": "CC0",
                "url": "https://www.clevelandart.org/art/collection/789",
            }]
        }))

        artwork = self.museum_client.fetch_cma_artwork(posted_ids=set())

        self.assertIsNotNone(artwork)
        self.assertEqual(artwork.museum, "cma")
        self.assertGreaterEqual(artwork.score, MINIMUM_QUALITY_SCORE)
        self.assertTrue(artwork.image_url.startswith("http"))

    def test_api_network_error_is_not_success(self):
        """Ağ hatası, sessizce başarılı kabul edilmemelidir."""
        self.museum_client.session.get = Mock(side_effect=requests.ConnectionError("offline"))

        artwork = self.museum_client.fetch_met_artwork(posted_ids=set())

        self.assertIsNone(artwork)

    def test_api_invalid_response_is_not_success(self):
        """Geçersiz API cevabı, sessizce başarılı kabul edilmemelidir."""
        self.museum_client.session.post = Mock(return_value=FakeResponse([]))

        artwork = self.museum_client.fetch_aic_artwork(posted_ids=set())

        self.assertIsNone(artwork)

    def test_multi_medium_tag_generation(self):
        """Farklı türler (Heykel, Çizim, Resim, Obje) için etiketlerin tam 5 adet olduğunu test eder."""
        poster = TumblrPoster()

        # Heykel Testi
        sculpture = Artwork(
            museum="met", id="1", title="Bust of Zeus", artist="Unknown Greek Master",
            artist_bio="Unknown Greek Master", date="4th Century BC", image_url="https://example.com/zeus.jpg",
            original_source_url="https://example.com/zeus", location_info="Gallery 1", dimensions="Unknown dimensions",
            museum_name="The Met", medium_type="Sculpture", raw_medium="Stone",
            score=90, style_or_era="Classical Antiquity"
        )
        sculpture_tags = poster.generate_tags(sculpture)
        self.assertEqual(len(sculpture_tags), 5)
        self.assertIn("sculpture", sculpture_tags)
        self.assertNotIn("unknown greek master", sculpture_tags)
        print(f"\n[Heykel Etiketleri (5 adet)]: {sculpture_tags}")

        # Çizim Testi
        drawing = Artwork(
            museum="aic", id="2", title="Study of Hands", artist="Leonardo da Vinci",
            artist_bio="Leonardo da Vinci", date="1490", image_url="https://example.com/hands.jpg",
            original_source_url="https://example.com/hands", location_info="Gallery 2", dimensions="Unknown dimensions",
            museum_name="AIC", medium_type="Drawing", raw_medium="Ink and silverpoint on paper",
            score=95, style_or_era="Renaissance"
        )
        drawing_tags = poster.generate_tags(drawing)
        self.assertEqual(len(drawing_tags), 5)
        self.assertIn("drawing", drawing_tags)
        print(f"[Çizim Etiketleri (5 adet)]: {drawing_tags}")

    def test_caption_escapes_dynamic_values_without_escaping_markup(self):
        """Dinamik caption alanları escape edilir, bot HTML'i korunur."""
        poster = TumblrPoster()
        artwork = Artwork(
            museum="met", id="3", title='A & <Title> "One"', artist="A & <Artist>",
            artist_bio='A & <Artist> "One"', date="18<20 & 'era'",
            image_url="https://example.com/image.jpg", original_source_url="https://example.com/source",
            museum_name="The Met", location_info="Gallery", dimensions='10" & <20',
            medium_type="Painting & Art", raw_medium="Oil <on> canvas", score=90,
            style_or_era="Renaissance"
        )

        caption = poster.format_caption(artwork)

        self.assertIn("<p><b>Title:</b> A &amp; &lt;Title&gt; &quot;One&quot;</p>", caption)
        self.assertIn("<p><b>Artist:</b> A &amp; &lt;Artist&gt; &quot;One&quot;</p>", caption)
        self.assertIn("<p><b>Date:</b> 18&lt;20 &amp; &#x27;era&#x27;</p>", caption)
        self.assertIn('<p><b>Dimensions:</b> 10&quot; &amp; &lt;20</p>', caption)
        self.assertIn("<p><b>Type:</b> Painting &amp; Art (Oil &lt;on&gt; canvas)</p>", caption)
        self.assertIn("<br><p>More from this artist: <a href=", caption)

    def test_artist_name_normalization_is_conservative(self):
        self.assertEqual(normalize_artist_name("Christoffersen, Frede"), "Frede Christoffersen")
        self.assertEqual(normalize_artist_name("Frede Christoffersen"), "Frede Christoffersen")
        self.assertEqual(normalize_artist_name("Workshop of, Frede"), "Workshop of, Frede")
        self.assertEqual(normalize_artist_name("Anonymous, Unknown"), "Anonymous, Unknown")
        self.assertEqual(normalize_artist_name("After, Frede"), "After, Frede")
        self.assertEqual(normalize_artist_name("Artist Name (French, 1900-1980)"), "Artist Name (French, 1900-1980)")

    def test_dimension_normalization_is_metric_and_explicit_only(self):
        self.assertEqual(normalize_dimensions("31 centimeter"), "31 cm")
        self.assertEqual(normalize_dimensions("31 centimeters"), "31 cm")
        self.assertEqual(normalize_dimensions("31 centimetres"), "31 cm")
        self.assertEqual(normalize_dimensions("24.5 centimeter"), "24.5 cm")
        self.assertEqual(normalize_dimensions("24.5 centimeters x 31 centimeters"), "24.5 × 31 cm")
        self.assertEqual(normalize_dimensions("10 millimeters"), "10 mm")
        self.assertEqual(normalize_dimensions("1.2 meters"), "1.2 m")
        self.assertEqual(normalize_dimensions("12 inches"), "12 in")
        self.assertEqual(normalize_dimensions("12 in x 20 in"), "12 in x 20 in")
        self.assertEqual(normalize_dimensions("H. 31 centimeters; W. 20 centimeters"), "H. 31 cm; W. 20 cm")
        self.assertEqual(
            normalize_dimensions("Overall: 31 × 42 cm; framed: 50 × 61 cm"),
            "Overall: 31 × 42 cm; framed: 50 × 61 cm",
        )

    def test_medium_display_preserves_unmapped_source_text(self):
        self.assertEqual(normalize_medium_display("Painting", "Oil on canvas"), "Painting (Oil on canvas)")
        self.assertEqual(normalize_medium_display("Painting", "Olie på lærred"), "Painting (Olie på lærred)")
        self.assertEqual(normalize_medium_display("Painting", "Painting"), "Painting")

    def test_source_url_and_artist_tag_use_original_values(self):
        poster = TumblrPoster()
        poster.client.create_photo = Mock(return_value={"id": "post-1"})
        artwork = Artwork(
            museum="met", id="4", title="Christoffersen, Frede", artist="Christoffersen, Frede",
            artist_bio="Christoffersen, Frede", date="1900", image_url="https://example.com/image.jpg",
            original_source_url="https://museum.example/art/4", museum_name="The Met", location_info="Gallery",
            dimensions="31 centimeter", medium_type="Painting", raw_medium="Oil on canvas", score=90,
        )

        self.assertTrue(poster.post_artwork(artwork))
        kwargs = poster.client.create_photo.call_args.kwargs
        self.assertEqual(kwargs["link"], "https://museum.example/art/4")
        self.assertIn('/tagged/my:christoffersenfrede', kwargs["caption"])
        self.assertIn("Frede Christoffersen", kwargs["caption"])
        self.assertIn("31 cm", kwargs["caption"])

    def test_caption_omits_empty_display_labels(self):
        poster = TumblrPoster()
        artwork = Artwork(
            museum="met", id="7", title="", artist="", artist_bio=None, date=" ",
            image_url="https://example.com/image.jpg", original_source_url="",
            museum_name="The Met", location_info="", dimensions=" ", medium_type=None,
            raw_medium="", score=90,
        )

        caption = poster.format_caption(artwork)

        self.assertEqual(caption, "")
        self.assertNotIn("Title:", caption)
        self.assertNotIn("Artist:", caption)
        self.assertNotIn("Date:", caption)
        self.assertNotIn("Dimensions:", caption)
        self.assertNotIn("Type:", caption)

    def test_tag_generation_is_deterministic_and_metadata_backed(self):
        artwork = Artwork(
            museum="smk", id="5", title="A & Portrait", artist="Christoffersen, Frede",
            artist_bio="Christoffersen, Frede", date="1966", image_url="https://example.com/image.jpg",
            original_source_url="https://museum.example/art/5", museum_name="SMK", location_info="Gallery",
            dimensions="31 centimeter", medium_type="Painting", raw_medium="Oil on canvas", score=90,
            style_or_era="Danish Art",
        )

        first = TumblrPoster().generate_tags(artwork)
        second = TumblrPoster().generate_tags(artwork)

        self.assertEqual(first, second)
        self.assertEqual(len(first), 5)
        self.assertIn("frede christoffersen", first)
        self.assertIn("painting", first)
        self.assertIn("oil painting", first)
        self.assertNotIn("20th century art", first)  # metadata priority and five-tag cap
        self.assertEqual(len({tag.casefold() for tag in first}), len(first))

    def test_internal_artist_tag_preserves_legacy_raw_slug(self):
        self.assertEqual(artist_internal_tag("Christoffersen, Frede"), "christoffersenfrede")
        self.assertEqual(artist_internal_tag("Frede Christoffersen"), "fredechristoffersen")
        self.assertEqual(artist_internal_tag("Leonardo da Vinci"), "leonardodavinci")

    def test_attribution_internal_slugs_are_not_reordered(self):
        expected = {
            "Workshop of ...": "workshopof",
            "Attributed to ...": "attributedto",
            "Circle of ...": "circleof",
            "School of ...": "schoolof",
            "After ...": "after",
            "Follower of ...": "followerof",
            "Anonymous": "anonymous",
            "Unknown": "unknown",
        }
        for artist, slug in expected.items():
            with self.subTest(artist=artist):
                self.assertEqual(artist_internal_tag(artist), slug)

    def test_empty_metadata_does_not_create_empty_tags(self):
        artwork = Artwork(
            museum="met", id="6", title="Untitled", artist="Unknown Artist",
            artist_bio=None, date="Unknown Date", image_url="https://example.com/image.jpg",
            original_source_url="", museum_name="The Met", location_info="", dimensions=None,
            medium_type=None, raw_medium=None, score=60,
        )

        tags = generate_public_tags(artwork)

        self.assertTrue(tags)
        self.assertLessEqual(len(tags), 5)
        self.assertTrue(all(tag.strip() for tag in tags))
        self.assertNotIn("none", tags)
        self.assertNotIn("unknown", tags)

    def test_century_tag_only_accepts_simple_years(self):
        self.assertEqual(century_tag("1966"), "20th century art")
        self.assertEqual(century_tag("1889"), "19th century art")
        self.assertEqual(century_tag("ca. 1889"), "")
        self.assertEqual(century_tag("1889-1890"), "")
        self.assertEqual(century_tag("1889 BCE"), "")


if __name__ == "__main__":
    unittest.main()
