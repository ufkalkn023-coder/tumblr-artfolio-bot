"""
test_source_health.py - Kaynak sağlığı + devre kesici regresyon testleri.

Kapsam: durum makinesi (CLOSED/OPEN/HALF_OPEN), arıza sınıflandırması
(operasyonel arıza vs içerik reddi vs yapılandırma), orkestratör entegrasyonu,
AIC/Harvard özel durumları, HTTP retry etkileşimi ve test izolasyonu.
Sağlık state'i işlem başınadır: kalıcılık yok, run'lar arası sızıntı yok.
Tüm saatler sahte, tüm HTTP mock; gerçek ağ çağrısı yapılmaz.
"""

import json
import requests
import unittest
from unittest.mock import Mock, patch

import config
from artfolio.curation import MuseumAPIClient
from artfolio.curation.source_health import CircuitState, SourceHealthManager
from http_requests import MAX_TRANSIENT_HTTP_ATTEMPTS


class FakeClock:
    """Kontrol edilebilir monotonic saat."""

    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.headers = {}

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def build_client():
    return MuseumAPIClient()


def session_returning(response, method="get"):
    """session.get/post her çağrıda aynı yanıtı döndüren mock oturum."""
    session = Mock()
    getattr(session, method).return_value = response
    return session


def failing_session(exception, method="get"):
    """session.get/post her çağrıda verilen istisnayı yükselten mock oturum."""
    session = Mock()
    getattr(session, method).side_effect = exception
    return session


def session_sequence(responses, method="get"):
    """session.get/post çağrılarını sırayla verilen yanıtlara yönlendiren mock oturum."""
    session = Mock()
    getattr(session, method).side_effect = responses
    return session


class TestCircuitStateMachine(unittest.TestCase):
    """Durum makinesi: manager doğrudan, sahte saatle (görev testleri 1-10)."""

    def setUp(self):
        self.clock = FakeClock()
        self.health = SourceHealthManager(
            clock=self.clock, failure_threshold=3, cooldown_seconds=300
        )

    def test_new_source_starts_closed(self):
        self.assertTrue(self.health.can_attempt("met"))
        self.health.record_success("met")
        snapshot = self.health.snapshot()
        self.assertEqual(snapshot["met"]["state"], CircuitState.CLOSED.value)

    def test_first_operational_failure_increments_count(self):
        self.health.record_failure("met", "timeout")
        snapshot = self.health.snapshot()
        self.assertEqual(snapshot["met"]["consecutive_failures"], 1)
        self.assertEqual(snapshot["met"]["total_failures"], 1)
        self.assertEqual(snapshot["met"]["state"], "closed")

    def test_below_threshold_remains_closed(self):
        self.health.record_failure("met", "timeout")
        self.health.record_failure("met", "timeout")
        self.assertTrue(self.health.can_attempt("met"))
        self.assertEqual(self.health.snapshot()["met"]["state"], "closed")

    def test_threshold_failure_opens_circuit(self):
        for _ in range(3):
            self.health.record_failure("met", "timeout")
        self.assertEqual(self.health.snapshot()["met"]["state"], "open")

    def test_open_source_cannot_attempt_before_cooldown(self):
        for _ in range(3):
            self.health.record_failure("met", "timeout")
        self.clock.advance(299)
        self.assertFalse(self.health.can_attempt("met"))
        self.assertEqual(self.health.snapshot()["met"]["circuit_skips"], 1)

    def test_cooldown_expiry_allows_half_open_probe(self):
        for _ in range(3):
            self.health.record_failure("met", "timeout")
        self.clock.advance(300)
        self.assertTrue(self.health.can_attempt("met"))
        self.assertEqual(self.health.snapshot()["met"]["state"], "half_open")

    def test_half_open_success_closes_circuit(self):
        for _ in range(3):
            self.health.record_failure("met", "timeout")
        self.clock.advance(300)
        self.health.can_attempt("met")  # OPEN -> HALF_OPEN
        self.health.record_success("met")
        snapshot = self.health.snapshot()
        self.assertEqual(snapshot["met"]["state"], "closed")
        self.assertEqual(snapshot["met"]["consecutive_failures"], 0)

    def test_half_open_failure_reopens_circuit_and_restarts_cooldown(self):
        for _ in range(3):
            self.health.record_failure("met", "timeout")
        self.clock.advance(300)
        self.health.can_attempt("met")  # HALF_OPEN probe
        self.clock.advance(10)
        self.health.record_failure("met", "connection_error")
        snapshot = self.health.snapshot()
        self.assertEqual(snapshot["met"]["state"], "open")
        # cooldown yeniden başladı: 290s daha beklemek yetmez
        self.clock.advance(290)
        self.assertFalse(self.health.can_attempt("met"))
        self.clock.advance(10)  # toplam 300s reopen sonrası
        self.assertTrue(self.health.can_attempt("met"))

    def test_successful_closed_operation_resets_consecutive_failures(self):
        self.health.record_failure("met", "timeout")
        self.health.record_failure("met", "timeout")
        self.health.record_success("met")
        self.assertEqual(self.health.snapshot()["met"]["consecutive_failures"], 0)
        # Tek arıza artık devre açmaz (seri kırıldı).
        self.health.record_failure("met", "timeout")
        self.assertEqual(self.health.snapshot()["met"]["state"], "closed")

    def test_totals_remain_correct_across_transitions(self):
        self.health.record_failure("met", "timeout")
        self.health.record_failure("met", "timeout")
        self.health.record_failure("met", "timeout")  # OPEN
        self.health.record_success("met")             # dışarıdan success (savunma): kapanır
        self.health.record_success("met")
        self.health.record_failure("met", "timeout")
        snapshot = self.health.snapshot()
        self.assertEqual(snapshot["met"]["total_failures"], 4)
        self.assertEqual(snapshot["met"]["total_successes"], 2)
        self.assertEqual(snapshot["met"]["consecutive_failures"], 1)


class TestFailureClassification(unittest.TestCase):
    """Sınıflandırma: operasyonel arıza vs içerik reddi (görev testleri 11-18)."""

    def setUp(self):
        self.client = build_client()
        self.offline = failing_session(requests.ConnectionError("offline"))
        for source in self.client._sources.values():
            source.session = self.offline

    def _health(self, source_id):
        return self.client.get_source_health().get(source_id, {"total_failures": 0, "total_successes": 0, "state": "closed"})

    def test_timeout_after_retry_exhaustion_counts_once(self):
        met = self.client._sources["met"]
        met.session = failing_session(requests.Timeout("t"))
        with patch("http_requests.time.sleep"):
            self.client.fetch_met_artwork(set())
        self.assertEqual(met.session.get.call_count, MAX_TRANSIENT_HTTP_ATTEMPTS)
        snapshot = self._health("met")
        self.assertEqual(snapshot["total_failures"], 1)  # 3 retry = 1 mantıksal arıza
        self.assertEqual(snapshot["consecutive_failures"], 1)
        self.assertEqual(snapshot["last_failure_reason"], "timeout")

    def test_connection_error_exhaustion_counts_once(self):
        smk = self.client._sources["smk"]
        smk.session = failing_session(requests.ConnectionError("reset"))
        with patch("http_requests.time.sleep"):
            self.client.fetch_smk_artwork(set())
        snapshot = self._health("smk")
        self.assertEqual(snapshot["total_failures"], 1)
        self.assertEqual(snapshot["last_failure_reason"], "connection_error")

    def test_http_5xx_after_retries_counts_once(self):
        cma = self.client._sources["cma"]
        cma.session = session_returning(FakeResponse({}, status_code=503))
        with patch("http_requests.time.sleep"):
            self.client.fetch_cma_artwork(set())
        snapshot = self._health("cma")
        self.assertEqual(snapshot["total_failures"], 1)
        self.assertEqual(snapshot["last_failure_reason"], "http_503")

    def test_malformed_json_counts_as_operational_failure(self):
        met = self.client._sources["met"]
        met.session = session_returning(FakeResponse(json.JSONDecodeError("bad", "<doc>", 0)))
        self.client.fetch_met_artwork(set())
        snapshot = self._health("met")
        self.assertEqual(snapshot["total_failures"], 1)
        self.assertEqual(snapshot["last_failure_reason"], "malformed_json")

    def test_normal_zero_result_response_is_not_failure(self):
        met = self.client._sources["met"]
        met.session = session_returning(FakeResponse({"objectIDs": None}))
        artwork = self.client.fetch_met_artwork(set())
        self.assertIsNone(artwork)
        snapshot = self._health("met")
        self.assertEqual(snapshot["total_failures"], 0)
        self.assertEqual(snapshot["total_successes"], 1)
        self.assertEqual(snapshot["state"], "closed")

    def test_quality_rejection_is_not_failure(self):
        met = self.client._sources["met"]
        met.session = session_sequence([
            FakeResponse({"objectIDs": [123]}),
            FakeResponse({
                "isPublicDomain": True,
                "primaryImage": "https://example.com/x.jpg",
                "title": "Mediocre Work",
                "artistDisplayName": "Some Artist",
                "objectDate": "1900",
                "medium": "Oil on canvas",
                "classification": "Paintings",
                "objectName": "Painting",
                "isHighlight": False,
                "additionalImages": [],
                "objectURL": "https://www.metmuseum.org/art/123",
            }),
        ])
        with patch.object(config, "MINIMUM_QUALITY_SCORE", 101):
            artwork = self.client.fetch_met_artwork(set())
        self.assertIsNone(artwork)  # kalite eşiği: içerik reddi
        snapshot = self._health("met")
        self.assertEqual(snapshot["total_failures"], 0)
        self.assertEqual(snapshot["total_successes"], 1)

    def test_rights_rejection_is_not_failure(self):
        met = self.client._sources["met"]
        met.session = session_sequence([
            FakeResponse({"objectIDs": [124]}),
            FakeResponse({
                "isPublicDomain": False,
                "primaryImage": "https://example.com/x.jpg",
                "title": "Restricted",
                "objectURL": "https://www.metmuseum.org/art/124",
            }),
        ])
        artwork = self.client.fetch_met_artwork(set())
        self.assertIsNone(artwork)
        snapshot = self._health("met")
        self.assertEqual(snapshot["total_failures"], 0)
        self.assertEqual(snapshot["total_successes"], 1)

    def test_duplicate_rejection_is_not_failure(self):
        met = self.client._sources["met"]
        met.session = session_returning(FakeResponse({"objectIDs": [1, 2, 3]}))
        artwork = self.client.fetch_met_artwork(posted_ids={"1", "2", "3"})
        self.assertIsNone(artwork)  # tüm adaylar duplicate
        snapshot = self._health("met")
        self.assertEqual(snapshot["total_failures"], 0)
        self.assertEqual(snapshot["total_successes"], 1)

    def test_target_medium_mismatch_is_not_failure(self):
        """Hedef tür uyuşmazlığı orkestratör düzeyinde POLICY_REJECTION'dır."""
        cma = self.client._sources["cma"]
        cma.session = session_returning(FakeResponse({"data": [{
            "id": 789, "title": "The Test Sculpture",
            "creators": [{"description": "Auguste Rodin (French, 1840-1917)"}],
            "creation_date": "1904", "technique": "Bronze", "type": "Sculpture",
            "department": "Sculpture", "culture": ["French"],
            "images": {"web": {"url": "https://example.com/thinker.jpg"}},
            "current_location": "Gallery 1", "share_license_status": "CC0",
            "url": "https://www.clevelandart.org/art/collection/789",
        }]}))
        with patch("random.shuffle", side_effect=lambda values: None), \
                patch("http_requests.time.sleep"):
            artwork = self.client.get_random_artwork({}, target_medium="Painting")
        self.assertIsNone(artwork)  # Sculpture eseri Painting hedefiyle uyuşmaz
        self.assertEqual(self.client.last_run_stats["rejected_other"], 1)
        self.assertEqual(self.client.last_run_stats["circuit_skips"], 0)
        snapshot = self._health("cma")
        self.assertEqual(snapshot["total_failures"], 0)  # kaynak sağlıklı


class TestOrchestratorCircuitIntegration(unittest.TestCase):
    """Kaynak davranışı: unhealthy kaynak devreyi açar, atlanır; sağlıklılar normal (19-23)."""

    def setUp(self):
        self.client = build_client()
        self.offline = failing_session(requests.ConnectionError("offline"))
        for source in self.client._sources.values():
            source.session = self.offline
        # Hızlı test: eşik 1 yerine gerçekçi 3 ama tek çağrıyla açılmaz ->
        # manager eşiğini kısaltmak yerine gerçek eşikle 3 fetch yapıyoruz.
        self.client.health.failure_threshold = 1

    def test_unhealthy_met_opens_circuit_and_is_skipped(self):
        # 1 arıza (eşik 1) -> devre açılır
        with patch("http_requests.time.sleep"):
            self.client.fetch_met_artwork(set())
        self.assertEqual(self.client.get_source_health()["met"]["state"], "open")
        met_calls_before = self.client._sources["met"].session.get.call_count

        cma = self.client._sources["cma"]
        cma.session = session_returning(FakeResponse({"data": [{
            "id": 789, "title": "The Test Sculpture",
            "creators": [{"description": "Auguste Rodin (French, 1840-1917)"}],
            "creation_date": "1904", "technique": "Bronze", "type": "Sculpture",
            "department": "Sculpture", "culture": ["French"],
            "images": {"web": {"url": "https://example.com/thinker.jpg"}},
            "current_location": "Gallery 1", "share_license_status": "CC0",
            "url": "https://www.clevelandart.org/art/collection/789",
        }]}))
        with patch("random.shuffle", side_effect=lambda values: None), \
                patch("http_requests.time.sleep"):
            artwork = self.client.get_random_artwork({})
        self.assertIsNotNone(artwork)  # açık met atlandı, cma devam etti
        self.assertEqual(artwork.museum, "cma")
        self.assertEqual(self.client.last_run_stats["circuit_skips"], 1)
        self.assertEqual(
            self.client._sources["met"].session.get.call_count,
            met_calls_before,  # OPEN kaynak koşu sırasında HTTP çağrısı yapmadı
        )

    def test_healthy_source_selection_unchanged(self):
        """Sağlıklı kaynaklarda seçim davranışı birebir korunur (golden parity)."""
        cma = self.client._sources["cma"]
        cma.session = session_returning(FakeResponse({"data": [{
            "id": 789, "title": "The Test Sculpture",
            "creators": [{"description": "Auguste Rodin (French, 1840-1917)"}],
            "creation_date": "1904", "technique": "Bronze", "type": "Sculpture",
            "department": "Sculpture", "culture": ["French"],
            "images": {"web": {"url": "https://example.com/thinker.jpg"}},
            "current_location": "Gallery 1", "share_license_status": "CC0",
            "url": "https://www.clevelandart.org/art/collection/789",
        }]}))
        # met ilk sırada ama sağlıklı-yanlış yapılandırılmış: boş arama → sonraki kaynağa geçer
        met = self.client._sources["met"]
        met.session = session_returning(FakeResponse({"objectIDs": None}))
        with patch("random.shuffle", side_effect=lambda values: None):
            artwork = self.client.get_random_artwork({})
        self.assertIsNotNone(artwork)
        self.assertEqual(artwork.museum, "cma")
        self.assertEqual(self.client.last_run_stats["circuit_skips"], 0)
        self.assertEqual(self.client.get_source_health()["met"]["state"], "closed")

    def test_circuit_skips_are_not_counted_as_content_rejections(self):
        """circuit_skips; rejected_quality/rights/duplicate ile karışmaz."""
        for _ in range(3):
            with patch("http_requests.time.sleep"):
                self.client.fetch_met_artwork(set())
        # met OPEN; smk da arızalı olsun (ayrı sayılsın)
        with patch("http_requests.time.sleep"):
            self.client.fetch_smk_artwork(set())
        self.client.health.failure_threshold = 3
        with patch("random.shuffle", side_effect=lambda values: None), \
                patch("http_requests.time.sleep"):
            self.client.get_random_artwork({})
        run_stats = self.client.last_run_stats
        self.assertGreaterEqual(run_stats["circuit_skips"], 1)
        self.assertEqual(run_stats["rejected_quality"], 0)
        self.assertEqual(run_stats["rejected_rights"], 0)


class TestAICDisabledBehavior(unittest.TestCase):

    def test_production_disabled_aic_never_accumulates_failures(self):
        client = build_client()
        # Gerçek ağ çağrısını kesin olarak engelle: tüm oturumlar mock.
        for source in client._sources.values():
            source.session = Mock()
        with patch("random.shuffle", side_effect=lambda values: None):
            client.get_random_artwork({})
        # AIC rotasyonda hiç yok: sağlık kaydı oluşmaz, devre açılmaz.
        self.assertNotIn("aic", client.get_source_health())
        client._sources["aic"].session.get.assert_not_called()
        client._sources["aic"].session.post.assert_not_called()

    def test_explicitly_enabled_aic_participates_normally(self):
        """Testlerde bayrak açılırsa normal circuit davranışı geçerlidir."""
        client = build_client()
        # Diğer kaynakların gerçek ağa çıkmasını engelle (sadece aic arızalı).
        for source in client._sources.values():
            source.session = Mock()
        aic = client._sources["aic"]
        aic.session = failing_session(requests.ConnectionError("offline"))
        with patch("artfolio.sources.aic.AIC_PUBLISHING_ENABLED", True), \
                patch("random.shuffle", side_effect=lambda values: None), \
                patch("http_requests.time.sleep"):
            client.get_random_artwork({})
        snapshot = client.get_source_health().get("aic")
        self.assertEqual(snapshot["total_failures"], 1)


class TestHarvardBehavior(unittest.TestCase):

    def test_rights_fail_closed_harvard_is_operationally_healthy(self):
        client = build_client()
        harvard = client._sources["harvard"]
        harvard.session = session_returning(FakeResponse({"records": [{
            "id": 1, "title": "H", "images": [{"baseimageurl": "https://example.com/h.jpg"}],
            "dated": "1900", "medium": "Oil on canvas", "classification": "Paintings",
            "imagepermissionlevel": 0,
        }]}))
        with patch.object(config, "HARVARD_API_KEY", "test-key"), \
                patch.object(config, "MINIMUM_QUALITY_SCORE", 0), \
                patch("http_requests.time.sleep"):
            artwork = client.fetch_harvard_artwork(set())
        self.assertIsNone(artwork)  # hak fail-closed
        snapshot = client.get_source_health()["harvard"]
        self.assertEqual(snapshot["total_failures"], 0)   # API sağlıklı
        self.assertEqual(snapshot["total_successes"], 1)  # HTTP+parse çalıştı
        self.assertEqual(snapshot["state"], "closed")

    def test_missing_harvard_api_key_is_not_transport_failure(self):
        client = build_client()
        self.assertFalse(config.HARVARD_API_KEY)
        with patch("http_requests.time.sleep"):
            artwork = client.fetch_harvard_artwork(set())
        self.assertIsNone(artwork)
        snapshot = client.get_source_health().get("harvard")
        self.assertIsNone(snapshot)  # DISABLED: hiç sağlık kaydı oluşmaz


class TestHealthIsolationAndSnapshot(unittest.TestCase):

    def test_fresh_client_starts_with_fresh_health_state(self):
        client1 = build_client()
        client1.health.failure_threshold = 1
        # Gerçek ağa çıkışı engelle: met arızalı sahte oturum kullanır.
        for source in client1._sources.values():
            source.session = Mock()
        met = client1._sources["met"]
        met.session = failing_session(requests.Timeout("t"))
        with patch("http_requests.time.sleep"):
            client1.fetch_met_artwork(set())
        self.assertEqual(client1.get_source_health()["met"]["state"], "open")

        client2 = build_client()
        self.assertEqual(client2.get_source_health(), {})
        self.assertTrue(client2.health.can_attempt("met"))  # state sızmadı

    def test_snapshot_is_serializable_and_sanitized(self):
        import json as json_module
        client = build_client()
        client.health.record_failure("met", "timeout http://secret.example/?apikey=XYZ line1\nline2")
        snapshot = client.get_source_health()
        serialized = json_module.dumps(snapshot)
        self.assertIn("met", snapshot)
        self.assertNotIn("apikey=XYZ", serialized)  # hassas sorgu parametresi taşınmaz
        self.assertNotIn("\n", snapshot["met"]["last_failure_reason"])


if __name__ == "__main__":
    unittest.main()
