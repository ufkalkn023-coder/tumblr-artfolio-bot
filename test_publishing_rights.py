"""
test_publishing_rights.py - Yayın Hakları Güvenlik Katmanı regresyon testleri.

Politika (fail-closed):
  - Açıkça doğrulanmış kamu malı / açık hak  -> yayınlanabilir
  - Bilinmeyen / belirsiz / kısıtlı haklar   -> yayınlanamaz
Tüm ağ erişimleri mock'lanır; gerçek istek yapılmaz.
"""

import unittest
from unittest.mock import Mock, patch

import config
from _mock_tumblr_credentials import apply_mock_tumblr_credentials
from museum_api import Artwork, MuseumAPIClient
from tumblr_poster import TumblrPoster

# Kaynak normalizasyon testleri yalnızca hak alanlarını doğrular; eserlerin
# kalite eşiğini geçmesi bu testlerin konusu değildir. Bu yüzden eşik test
# süresince düşürülür (prod mantığı değişmez, config patch'lenir).
NORMALIZATION_TEST_THRESHOLD = "0"


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


def make_artwork(artwork_id="rights-1", score=90, **rights_overrides):
    """Hak alanları bilinçli olarak override edilebilen temel fixture."""
    fields = {
        "is_public_domain": True,
        "license_name": "CC0",
        "license_url": "",
        "rights_statement": "",
    }
    fields.update(rights_overrides)
    return Artwork(
        museum="met", id=artwork_id, title="Test Artwork", artist="Artist",
        artist_bio="Artist", date="1900", image_url="https://example.com/image.jpg",
        original_source_url="https://example.com/artwork", museum_name="Test Museum",
        location_info="Gallery", dimensions="10 cm", medium_type="Painting",
        raw_medium="Oil on canvas", score=score,
        **fields
    )


class TestPublishingRightsModel(unittest.TestCase):

    def test_backward_compatible_constructor_without_rights_fields(self):
        """Hak alanları verilmeyen mevcut Artwork(...) çağrıları çalışmaya devam eder."""
        artwork = Artwork(
            museum="met", id="legacy", title="Old Style", artist="Artist",
            artist_bio="Artist", date="1900", image_url="https://example.com/image.jpg",
            original_source_url="https://example.com/artwork", museum_name="The Met",
            location_info="Gallery", dimensions="10 cm", medium_type="Painting",
            raw_medium="Oil on canvas", score=90,
        )
        self.assertFalse(artwork.is_public_domain)
        self.assertEqual(artwork.license_name, "")
        self.assertEqual(artwork.license_url, "")
        self.assertEqual(artwork.rights_statement, "")
        self.assertFalse(artwork.is_publishable)

    def test_explicit_public_domain_is_publishable(self):
        self.assertTrue(make_artwork().is_publishable)

    def test_is_publishable_requires_boolean_true(self):
        """Yalnızca True kabul edilir; None, 1 gibi ikame değerler publishable sayılmaz."""
        self.assertFalse(make_artwork(is_public_domain=None).is_publishable)
        self.assertFalse(make_artwork(is_public_domain=1).is_publishable)
        self.assertFalse(make_artwork(is_public_domain=False).is_publishable)


class TestTumblrFinalRightsGate(unittest.TestCase):

    def setUp(self):
        apply_mock_tumblr_credentials(config)
        self.poster = TumblrPoster()
        self.poster.client = Mock()
        self.poster.client.create_photo.return_value = {"id": "post-1"}

    def test_public_domain_artwork_reaches_tumblr(self):
        artwork = make_artwork("pd-ok")
        self.assertTrue(self.poster.post_artwork(artwork).success)
        self.poster.client.create_photo.assert_called_once()

    def test_non_public_domain_artwork_is_blocked(self):
        """Açıkça kamu malı olmayan eser yayınlanamaz ve create_photo hiç çağrılmaz."""
        artwork = make_artwork("restricted", is_public_domain=False, license_name="© Rights Reserved")
        self.assertFalse(self.poster.post_artwork(artwork).success)
        self.poster.client.create_photo.assert_not_called()

    def test_artwork_without_rights_metadata_is_blocked(self):
        """Hak metadata'sı olmayan eser varsayılan olarak yayınlanamaz."""
        artwork = Artwork(
            museum="met", id="no-rights", title="Untitled", artist="Unknown Artist",
            artist_bio="Unknown Artist", date="1900", image_url="https://example.com/image.jpg",
            original_source_url="https://example.com/artwork", museum_name="Test Museum",
            location_info="Gallery", dimensions="10 cm", medium_type="Painting",
            raw_medium="Oil on canvas", score=90,
        )
        self.assertFalse(artwork.is_publishable)
        self.assertFalse(self.poster.post_artwork(artwork).success)
        self.poster.client.create_photo.assert_not_called()

    def test_high_score_does_not_override_rights(self):
        """Yüksek kalite puanı hak kısıtlamasını geçersiz kılamaz."""
        artwork = make_artwork("high-score", score=100, is_public_domain=False)
        self.assertFalse(self.poster.post_artwork(artwork).success)
        self.poster.client.create_photo.assert_not_called()

    def test_image_upload_path_is_also_gated(self):
        """Yüklenmiş görsel dosyalarıyla (data=) yayın denemesi de hak kapısına takılır."""
        artwork = make_artwork("upload-path", is_public_domain=False)
        self.assertFalse(self.poster.post_artwork(artwork, image_paths=["/tmp/one.jpg", "/tmp/two.jpg"]).success)
        self.poster.client.create_photo.assert_not_called()

    def test_gate_blocks_duck_typed_objects_fail_closed(self):
        """is_publishable özniteliği olmayan nesneler fail-closed olur."""
        duck = Mock(spec=["museum", "id", "title"])
        duck.museum = "met"
        duck.id = "duck-1"
        self.assertFalse(self.poster.post_artwork(duck).success)
        self.poster.client.create_photo.assert_not_called()

    def test_blocked_publishing_uses_structured_log(self):
        artwork = make_artwork("log-check", is_public_domain=False)
        with self.assertLogs("artfolio_bot.tumblr_poster", level="ERROR") as captured:
            self.poster.post_artwork(artwork)
        self.assertTrue(
            any("tumblr_publish_blocked source=met object_id=log-check reason=rights_not_publishable" in line
                for line in captured.output),
            captured.output,
        )


class TestSourceRightsNormalization(unittest.TestCase):

    def setUp(self):
        self.client = MuseumAPIClient()

    def test_met_public_domain_fixture_is_normalized(self):
        """The Met: per-obje isPublicDomain=True alanı normalize edilir."""
        self.client.session.get = Mock(side_effect=[
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
                "additionalImages": [],
                "objectURL": "https://www.metmuseum.org/art/collection/search/123",
                "rightsAndReproduction": "Public domain",
            }),
        ])
        with patch.object(config, "MINIMUM_QUALITY_SCORE", int(NORMALIZATION_TEST_THRESHOLD)):
            artwork = self.client.fetch_met_artwork(posted_ids=set())
        self.assertIsNotNone(artwork)
        self.assertTrue(artwork.is_public_domain)
        self.assertTrue(artwork.is_publishable)
        self.assertEqual(artwork.rights_statement, "Public domain")

    def test_met_non_public_domain_object_is_rejected_at_fetch_level(self):
        """The Met: isPublicDomain=False objesi fetch aşamasında reddedilir (None)."""
        self.client.session.get = Mock(side_effect=[
            FakeResponse({"objectIDs": [124]}),
            FakeResponse({
                "isPublicDomain": False,
                "primaryImage": "https://example.com/restricted.jpg",
                "title": "Restricted Work",
                "artistDisplayName": "Some Artist",
                "objectDate": "1900",
                "department": "European Paintings",
                "medium": "Oil on canvas",
                "classification": "Paintings",
                "objectName": "Painting",
                "isHighlight": True,
                "additionalImages": [],
                "objectURL": "https://www.metmuseum.org/art/collection/search/124",
            }),
        ])
        artwork = self.client.fetch_met_artwork(posted_ids=set())
        self.assertIsNone(artwork)
        self.assertEqual(self.client.last_fetch_stats["rejected_rights"], 1)

    def test_aic_non_public_domain_fixture_is_not_publishable(self):
        """AIC fixture'ı is_public_domain=False → normalize edilen eser yayınlanamaz."""
        self.client.session.post = Mock(return_value=FakeResponse({
            "data": [{
                "id": 457, "title": "The Test Painting", "artist_display": "Leonardo da Vinci",
                "date_display": "1503", "image_id": "nonpublic-image", "artwork_type_title": "Painting",
                "medium_display": "Oil on canvas", "classification_title": "Paintings",
                "is_public_domain": False, "is_boosted": True, "is_on_view": True,
            }],
            "config": {"iiif_url": "https://images.example/iiif/2"},
        }))
        with patch.object(config, "MINIMUM_QUALITY_SCORE", int(NORMALIZATION_TEST_THRESHOLD)):
            artwork = self.client.fetch_aic_artwork(posted_ids=set())
        self.assertIsNotNone(artwork)
        self.assertFalse(artwork.is_public_domain)
        self.assertFalse(artwork.is_publishable)

    def test_aic_public_domain_fixture_is_publishable(self):
        """AIC fixture'ı is_public_domain=True → eser yayınlanabilir kalır."""
        self.client.session.post = Mock(return_value=FakeResponse({
            "data": [{
                "id": 456, "title": "The Test Painting", "artist_display": "Leonardo da Vinci",
                "date_display": "1503", "image_id": "test-image", "artwork_type_title": "Painting",
                "medium_display": "Oil on canvas", "classification_title": "Paintings",
                "is_public_domain": True, "is_boosted": True, "is_on_view": True,
                "style_title": "Renaissance",
            }],
            "config": {"iiif_url": "https://images.example/iiif/2"},
        }))
        with patch.object(config, "MINIMUM_QUALITY_SCORE", int(NORMALIZATION_TEST_THRESHOLD)):
            artwork = self.client.fetch_aic_artwork(posted_ids=set())
        self.assertIsNotNone(artwork)
        self.assertTrue(artwork.is_public_domain)
        self.assertTrue(artwork.is_publishable)

    def test_cma_cc0_is_publishable_and_other_license_is_not(self):
        """CMA: yalnızca share_license_status=CC0 kamu malı sayılır; CC BY fail-closed."""
        base_item = {
            "id": 789, "title": "The Test Sculpture",
            "creators": [{"description": "Auguste Rodin (French, 1840-1917)"}],
            "creation_date": "1904", "technique": "Bronze", "type": "Sculpture",
            "department": "Sculpture", "culture": ["French"],
            "images": {"web": {"url": "https://example.com/thinker.jpg"}},
            "current_location": "Gallery 1",
            "url": "https://www.clevelandart.org/art/collection/789",
        }
        self.client.session.get = Mock(return_value=FakeResponse({"data": [dict(base_item, share_license_status="CC0")]}))
        with patch.object(config, "MINIMUM_QUALITY_SCORE", int(NORMALIZATION_TEST_THRESHOLD)):
            artwork = self.client.fetch_cma_artwork(posted_ids=set())
        self.assertIsNotNone(artwork)
        self.assertTrue(artwork.is_publishable)
        self.assertEqual(artwork.license_name, "CC0")

        self.client.session.get = Mock(return_value=FakeResponse({"data": [dict(base_item, share_license_status="CC BY")]}))
        with patch.object(config, "MINIMUM_QUALITY_SCORE", int(NORMALIZATION_TEST_THRESHOLD)):
            artwork = self.client.fetch_cma_artwork(posted_ids=set())
        # CC BY, CC0 doğrulaması değildir: aday fetch aşamasında elenir.
        self.assertIsNone(artwork)
        self.assertEqual(self.client.last_fetch_stats["rejected_rights"], 1)

    def test_cma_missing_license_status_fails_closed(self):
        self.client.session.get = Mock(return_value=FakeResponse({"data": [{
            "id": 790, "title": "No License", "creators": [],
            "creation_date": "1900", "technique": "Bronze", "type": "Sculpture",
            "department": "Sculpture", "culture": ["French"],
            "images": {"web": {"url": "https://example.com/x.jpg"}},
            "url": "https://www.clevelandart.org/art/collection/790",
        }]}))
        with patch.object(config, "MINIMUM_QUALITY_SCORE", int(NORMALIZATION_TEST_THRESHOLD)):
            artwork = self.client.fetch_cma_artwork(posted_ids=set())
        self.assertIsNone(artwork)
        self.assertEqual(self.client.last_fetch_stats["rejected_rights"], 1)

    def test_smk_public_domain_flag_is_normalized(self):
        """SMK: eser bazlı public_domain alanı doğrulanır; license_text saklanır."""
        self.client.session.get = Mock(return_value=FakeResponse({"items": [{
            "object_number": "smk-1", "image_native": "https://example.com/smk.jpg",
            "titles": [{"title": "Danish Painting"}], "production": [],
            "production_date": [], "techniques": ["Oil on canvas"],
            "object_names": [{"name": "Painting"}], "on_display": True,
            "public_domain": True, "license_text": "Creative Commons Zero (CC0)",
        }]}))
        with patch.object(config, "MINIMUM_QUALITY_SCORE", int(NORMALIZATION_TEST_THRESHOLD)):
            artwork = self.client.fetch_smk_artwork(posted_ids=set())
        self.assertIsNotNone(artwork)
        self.assertTrue(artwork.is_publishable)
        self.assertEqual(artwork.license_name, "Creative Commons Zero (CC0)")

    def test_smk_missing_public_domain_flag_fails_closed(self):
        """SMK: public_domain alanı yoksa eser fail-closed olur."""
        self.client.session.get = Mock(return_value=FakeResponse({"items": [{
            "object_number": "smk-2", "image_native": "https://example.com/smk2.jpg",
            "titles": [{"title": "Unclear Rights"}], "production": [],
            "production_date": [], "techniques": ["Wood"],
            "object_names": [{"name": "Object"}], "on_display": False,
        }]}))
        with patch.object(config, "MINIMUM_QUALITY_SCORE", int(NORMALIZATION_TEST_THRESHOLD)):
            artwork = self.client.fetch_smk_artwork(posted_ids=set())
        self.assertIsNone(artwork)
        self.assertEqual(self.client.last_fetch_stats["rejected_rights"], 1)

    def test_harvard_remains_fail_closed(self):
        """Harvard: API eser bazlı CC0/kamu malı bildirimi içermez → her aday fetch aşamasında elenir."""
        self.client.session.get = Mock(return_value=FakeResponse({"records": [{
            "id": 1, "title": "Harvard Object", "images": [
                {"baseimageurl": "https://example.com/harvard.jpg"}],
            "dated": "1900", "medium": "Oil on canvas", "classification": "Paintings",
            "imagepermissionlevel": 0, "permissionlevel": 0,
        }]}))
        with patch.object(config, "HARVARD_API_KEY", "test-key"), \
                patch.object(config, "MINIMUM_QUALITY_SCORE", int(NORMALIZATION_TEST_THRESHOLD)):
            artwork = self.client.fetch_harvard_artwork(posted_ids=set())
        self.assertIsNone(artwork)
        self.assertEqual(self.client.last_fetch_stats["rejected_rights"], 1)


class TestSelectionRightsGate(unittest.TestCase):

    def setUp(self):
        self.client = MuseumAPIClient()

    def _configure_single_source(self, source, artwork, eligible=0, rejected_quality=0):
        for other in ("met", "aic", "cma", "smk", "harvard"):
            if other == source:
                def fetch(_posted_ids, _target_medium):
                    self.client.last_fetch_stats = {
                        "source": source, "candidates": 1, "duplicates": 0,
                        "rejected_image": 0, "rejected_quality": rejected_quality,
                        "rejected_other": 0, "eligible": eligible,
                    }
                    return artwork
                setattr(self.client, f"fetch_{other}_artwork", fetch)
            else:
                def empty(_posted_ids, _target_medium, _source=other):
                    self.client.last_fetch_stats = {
                        "source": _source, "candidates": 0, "duplicates": 0,
                        "rejected_image": 0, "rejected_quality": 0,
                        "rejected_other": 0, "eligible": 0,
                    }
                    return None
                setattr(self.client, f"fetch_{other}_artwork", empty)

    def test_rights_rejection_is_not_counted_as_quality_rejection(self):
        """Hak reddi rejected_rights'a yazılır; rejected_quality ve kalite telemetrisi bozulmaz."""
        restricted = make_artwork("restricted-1", score=100, is_public_domain=False)
        self._configure_single_source("met", restricted, eligible=1)

        with patch("random.shuffle", side_effect=lambda values: None):
            result = self.client.get_random_artwork({})

        self.assertIsNone(result)
        run_stats = self.client.last_run_stats
        self.assertEqual(run_stats["rejected_rights"], 1)
        self.assertEqual(run_stats["rejected_quality"], 0)
        # Telemetri: eser kalite eşiğini geçti ama seçilmedi.
        self.assertEqual(self.client.scoring_telemetry.sources["met"]["first_eligible"], 0)

    def test_rights_rejection_uses_structured_log(self):
        restricted = make_artwork("restricted-2", score=100, is_public_domain=False)
        self._configure_single_source("cma", restricted, eligible=1)

        with patch("random.shuffle", side_effect=lambda values: None), \
                self.assertLogs("artfolio_bot.museum_api", level="WARNING") as captured:
            result = self.client.get_random_artwork({})

        self.assertIsNone(result)
        self.assertTrue(
            any("rights_rejection source=cma object_id=restricted-2 reason=rights_not_publishable" in line
                for line in captured.output),
            captured.output,
        )

    def test_quality_rejection_does_not_count_as_rights_rejection(self):
        """Yayınlanabilir ama düşük puanlı eser kalite istatistiğine yazılır, hak reddine değil."""
        low_score = make_artwork("low-score", score=70, is_public_domain=True)
        self._configure_single_source("met", low_score, eligible=0, rejected_quality=1)

        with patch("random.shuffle", side_effect=lambda values: None):
            result = self.client.get_random_artwork({})

        self.assertIsNone(result)
        run_stats = self.client.last_run_stats
        self.assertEqual(run_stats["rejected_rights"], 0)
        self.assertEqual(run_stats["rejected_quality"], 1)

    def test_publishable_artwork_still_selected(self):
        artwork = make_artwork("publishable-1", score=90, is_public_domain=True)
        self._configure_single_source("smk", artwork, eligible=1)

        with patch("random.shuffle", side_effect=lambda values: None):
            result = self.client.get_random_artwork({})

        self.assertIsNotNone(result)
        self.assertEqual(result.id, "publishable-1")
        self.assertEqual(self.client.last_run_stats["rejected_rights"], 0)


if __name__ == "__main__":
    unittest.main()
