"""
test_artfolio_package.py - artfolio paket yapısı ve uyumluluk cephesi testleri.

Kapsam:
  - museum_api uyumluluk cephesi: import uyumluluğu + kimlik (identity) garantileri
  - Bağımlılık yönü: kanonik modüller asla cepheyi (museum_api) import etmez
  - Doğrudan adaptör testleri: her kaynak sınıfı kendi modülünden kurulur,
    sağlayıcı HTTP'si mock'lanır (gerçek ağ yok)
  - AIC devre dışı bayrağının yeni kanonik patch yolu
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import config
import museum_api

import artfolio.curation.museum_client as museum_client_module
import artfolio.domain.artwork as artwork_module
import artfolio.scoring.scorer as scorer_module
import artfolio.scoring.telemetry as telemetry_module
from artfolio.curation import MuseumAPIClient
from artfolio.domain import Artwork
from artfolio.scoring import ArtworkScorer
from artfolio.sources import (
    AICSource,
    CMASource,
    HarvardSource,
    MetSource,
    SMKSource,
)
from artfolio.sources.aic import build_aic_iiif_image_url


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


def build_client():
    return MuseumAPIClient()


class TestCompatibilityFacade(unittest.TestCase):

    def test_facade_reexports_public_symbols(self):
        """Kök museum_api import'ları değişmeden çalışmaya devam eder."""
        from museum_api import Artwork as FArtwork
        from museum_api import ArtworkScorer as FScorer
        from museum_api import MuseumAPIClient as FClient
        from museum_api import ScoringTelemetry as FTelemetry
        from museum_api import CandidatePoolTelemetry as FPool
        from museum_api import build_aic_iiif_image_url as FBuilder

        self.assertIs(FArtwork, Artwork)
        self.assertIs(FScorer, ArtworkScorer)
        self.assertIs(FClient, MuseumAPIClient)
        self.assertIs(FTelemetry, museum_client_module.ScoringTelemetry)
        self.assertIs(FPool, museum_client_module.CandidatePoolTelemetry)
        self.assertIs(FBuilder, build_aic_iiif_image_url)

    def test_facade_symbols_are_canonical_module_objects(self):
        """Cephe sembolleri kanonik modül nesneleriyle kimliktir (aynı sınıf)."""
        self.assertIs(museum_api.Artwork, artwork_module.Artwork)
        self.assertIs(museum_api.ArtworkScorer, scorer_module.ArtworkScorer)
        self.assertIs(museum_api.ScoringTelemetry, telemetry_module.ScoringTelemetry)
        self.assertIs(museum_api.MuseumAPIClient, museum_client_module.MuseumAPIClient)

    def test_artwork_semantics_preserved(self):
        """Alan modeli: rights alanları ve is_publishable semantiği aynen korunur."""
        artwork = Artwork(
            museum="met", id="1", title="T", artist="A", artist_bio="A", date="1900",
            image_url="https://example.com/x.jpg", original_source_url="https://example.com/o",
            museum_name="M", location_info="G", dimensions="10 cm", medium_type="Painting",
            raw_medium="Oil on canvas", score=90,
        )
        self.assertFalse(artwork.is_publishable)  # fail-closed default
        artwork.is_public_domain = True
        self.assertTrue(artwork.is_publishable)
        artwork.is_public_domain = 1
        self.assertFalse(artwork.is_publishable)  # yalnızca `is True`

    def test_canonical_modules_never_import_facade(self):
        """Bağımlılık yönü: kanonik artfolio.* modülleri museum_api cephesini import etmez."""
        package_files = list(Path("artfolio").rglob("*.py"))
        self.assertTrue(package_files)
        for py_file in package_files:
            text = py_file.read_text(encoding="utf-8")
            self.assertNotIn("import museum_api", text, f"{py_file} imports the facade")
            self.assertNotIn("from museum_api", text, f"{py_file} imports the facade")


class TestSourceAdapters(unittest.TestCase):
    """Doğrudan adaptör testleri: kaynak sınıfları kendi modüllerinden kurulur."""

    def setUp(self):
        self.client = build_client()

    def _adapter(self, cls):
        return self.client._sources[cls.source_id]

    def test_client_owns_shared_session_and_telemetry(self):
        """Orkestratör tek bir oturum + telemetri kurar; adaptörlerle paylaşır."""
        met = self._adapter(MetSource)
        aic = self._adapter(AICSource)
        self.assertIs(met.session, aic.session)
        self.assertIs(self.client.session, met.session)
        self.assertIs(met.scoring_telemetry, self.client.scoring_telemetry)
        self.assertIs(aic.pool_telemetry, self.client.pool_telemetry)
        self.assertIs(self.client.pool_coverage, met.pool_coverage)

    def test_met_source_direct_normalization(self):
        met = self._adapter(MetSource)
        met.session.get = Mock(side_effect=[
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
                "rightsAndReproduction": "Public domain",
            }),
        ])
        artwork = met.fetch_artwork(set())
        self.assertIsNotNone(artwork)
        self.assertEqual(artwork.museum, "met")
        self.assertEqual(artwork.museum_name, "The Metropolitan Museum of Art, New York")
        self.assertTrue(artwork.is_publishable)
        self.assertEqual(artwork.rights_statement, "Public domain")
        self.assertEqual(met.last_fetch_stats["source"], "met")

    def test_aic_source_direct_normalization(self):
        aic = self._adapter(AICSource)
        aic.session.post = Mock(return_value=FakeResponse({
            "data": [{
                "id": 456, "title": "The Test Painting", "artist_display": "Leonardo da Vinci",
                "date_display": "1503", "image_id": "test-image", "artwork_type_title": "Painting",
                "medium_display": "Oil on canvas", "classification_title": "Paintings",
                "is_public_domain": True, "is_boosted": True, "is_on_view": True,
            }],
            "config": {"iiif_url": "https://images.example/iiif/2"},
        }))
        artwork = aic.fetch_artwork(set())
        self.assertIsNotNone(artwork)
        self.assertEqual(artwork.image_url, "https://images.example/iiif/2/test-image/full/1686,/0/default.jpg")
        self.assertTrue(artwork.is_publishable)
        self.assertEqual(aic.last_fetch_stats["eligible"], 1)

    def test_cma_source_direct_rights_gate(self):
        cma = self._adapter(CMASource)
        cma.session.get = Mock(return_value=FakeResponse({"data": [{
            "id": 789, "title": "The Test Sculpture",
            "creators": [{"description": "Auguste Rodin (French, 1840-1917)"}],
            "creation_date": "1904", "technique": "Bronze", "type": "Sculpture",
            "department": "Sculpture", "culture": ["French"],
            "images": {"web": {"url": "https://example.com/thinker.jpg"}},
            "current_location": "Gallery 1", "share_license_status": "CC0",
            "url": "https://www.clevelandart.org/art/collection/789",
        }]}))
        artwork = cma.fetch_artwork(set())
        self.assertIsNotNone(artwork)
        self.assertEqual(artwork.license_name, "CC0")
        self.assertTrue(artwork.is_publishable)

    def test_smk_source_direct_fail_closed_without_flag(self):
        smk = self._adapter(SMKSource)
        smk.session.get = Mock(return_value=FakeResponse({"items": [{
            "object_number": "smk-1", "image_native": "https://example.com/s.jpg",
            "titles": [{"title": "X"}], "production": [], "production_date": [],
            "techniques": ["Wood"], "object_names": [{"name": "Object"}], "on_display": False,
        }]}))
        with patch.object(config, "MINIMUM_QUALITY_SCORE", 0):
            artwork = smk.fetch_artwork(set())
        self.assertIsNone(artwork)  # public_domain alanı yok -> fetch düzeyinde hak reddi
        self.assertEqual(smk.last_fetch_stats["rejected_rights"], 1)

    def test_harvard_source_direct_fail_closed(self):
        harvard = self._adapter(HarvardSource)
        harvard.session.get = Mock(return_value=FakeResponse({"records": [{
            "id": 1, "title": "H", "images": [{"baseimageurl": "https://example.com/h.jpg"}],
            "dated": "1900", "medium": "Oil on canvas", "classification": "Paintings",
            "imagepermissionlevel": 0,
        }]}))
        with patch.object(config, "HARVARD_API_KEY", "test-key"), \
                patch.object(config, "MINIMUM_QUALITY_SCORE", 0):
            artwork = harvard.fetch_artwork(set())
        self.assertIsNone(artwork)  # sağlayıcı hak bildirimi yok -> her zaman fail-closed
        self.assertEqual(harvard.last_fetch_stats["rejected_rights"], 1)

    def test_aic_disable_flag_patched_at_canonical_location(self):
        """AIC bayrağı artık artfolio.sources.aic üzerinden patch'lenir; bayrak
        açıkken orkestratör AIC fetcher'ını rotasyona dahil eder."""
        import requests

        client = build_client()
        aic_mock = Mock(return_value=None)
        client.fetch_aic_artwork = aic_mock
        # Gerçek ağ erişimini tamamen kes: paylaşılan oturum ve adaptör oturumları mock.
        offline_session = Mock(side_effect=requests.ConnectionError("offline"))
        client.session = offline_session
        for source in client._sources.values():
            source.session = offline_session

        with patch("artfolio.sources.aic.AIC_PUBLISHING_ENABLED", True), \
                patch("random.shuffle", side_effect=lambda values: None), \
                patch("http_requests.time.sleep"):
            client.get_random_artwork({})
        self.assertTrue(aic_mock.called)

    def test_pool_telemetry_delegation_preserves_client_contract(self):
        """client._record_aic_pool çağrıları adaptörün record_pool'una delege edilir."""
        client = build_client()
        with tempfile.TemporaryDirectory() as tmp:
            client._record_aic_pool([{
                "id": 1, "title": "P", "artist_display": "Public artist", "date_display": "1900",
                "image_id": "img", "artwork_type_title": "Painting", "medium_display": "Oil on canvas",
                "classification_title": "Paintings",
            }], {"9"})
            self.assertEqual(client.pool_coverage["aic"]["coverage"], "full")
            path = Path(tmp) / "scoring.json"
            client.write_scoring_telemetry(path, publish_success=True, run_timestamp="ts")
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 1)
        self.assertIn("aic", payload["pool"]["sources"])


class TestRijksmuseumStatus(unittest.TestCase):

    def test_no_rijksmuseum_adapter_is_invented(self):
        """Rijksmuseum yalnızca config/state iskeletidir; uydurma adaptör yoktur."""
        from artfolio import sources
        self.assertFalse(hasattr(sources, "RijksmuseumSource"))
        self.assertFalse(hasattr(museum_api, "RijksmuseumSource"))


if __name__ == "__main__":
    unittest.main()
