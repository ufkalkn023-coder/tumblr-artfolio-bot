"""
test_http_requests.py - Paylaşılan HTTP retry politikası regresyon testleri.

Kapsam: request_with_bounded_retry (durum kodları + Timeout/ConnectionError,
sınırlı exponential backoff + jitter, Retry-After, response.close) ve
müze/görsel/Tumblr entegrasyon regresyonları. Tüm testler hızlıdır:
time.sleep mock'lanır, gerçek ağ çağrısı yapılmaz.
"""

import unittest
from unittest.mock import Mock, patch

import requests

from http_requests import (
    AIC_METADATA_RETRYABLE_STATUS_CODES,
    MAX_RETRY_AFTER_SECONDS,
    MAX_TRANSIENT_HTTP_ATTEMPTS,
    _transient_delay,
    parse_retry_after,
    request_with_bounded_retry,
    safe_url_for_logging,
)
import image_processor
from tumblr_poster import PublishResult, TumblrPoster

import config
from _mock_tumblr_credentials import apply_mock_tumblr_credentials


class FakeHttpResponse:
    def __init__(self, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.close = Mock()

    def json(self):
        return {}


def _run(request_mock, **kwargs):
    logger = Mock()
    result = request_with_bounded_retry(
        request_mock,
        endpoint="unit_test",
        logger=logger,
        **kwargs,
    )
    return result, logger


class TestRetryHelperStatuses(unittest.TestCase):

    def test_first_request_success_does_not_sleep(self):
        request_mock = Mock(return_value=FakeHttpResponse(200))
        result, logger = _run(request_mock)
        self.assertEqual(result.status_code, 200)
        request_mock.assert_called_once()
        result.close.assert_not_called()
        logger.warning.assert_not_called()

    def test_timeout_is_retried_then_succeeds(self):
        ok = FakeHttpResponse(200)
        request_mock = Mock(side_effect=[requests.Timeout("t"), ok])

        with patch("http_requests.time.sleep") as sleep:
            result, _ = _run(request_mock)

        self.assertIs(result, ok)
        self.assertEqual(request_mock.call_count, 2)
        sleep.assert_called_once()

    def test_connection_error_is_retried_then_succeeds(self):
        ok = FakeHttpResponse(200)
        request_mock = Mock(side_effect=[requests.ConnectionError("reset"), ok])

        with patch("http_requests.time.sleep"):
            result, _ = _run(request_mock)

        self.assertIs(result, ok)
        self.assertEqual(request_mock.call_count, 2)

    def test_timeout_exhausts_attempts_and_reraises(self):
        request_mock = Mock(side_effect=requests.Timeout("t"))

        with patch("http_requests.time.sleep") as sleep:
            with self.assertRaises(requests.Timeout):
                _run(request_mock)

        self.assertEqual(request_mock.call_count, MAX_TRANSIENT_HTTP_ATTEMPTS)
        self.assertEqual(sleep.call_count, MAX_TRANSIENT_HTTP_ATTEMPTS - 1)

    def test_connection_error_exhausts_attempts_and_reraises(self):
        request_mock = Mock(side_effect=requests.ConnectionError("down"))

        with patch("http_requests.time.sleep"):
            with self.assertRaises(requests.ConnectionError):
                _run(request_mock)

        self.assertEqual(request_mock.call_count, MAX_TRANSIENT_HTTP_ATTEMPTS)

    def test_transient_5xx_statuses_are_retried(self):
        for status in (500, 502, 503, 504):
            with self.subTest(status=status):
                ok = FakeHttpResponse(200)
                request_mock = Mock(side_effect=[FakeHttpResponse(status), ok])

                with patch("http_requests.time.sleep") as sleep:
                    result, _ = _run(request_mock)

                self.assertEqual(result.status_code, 200)
                self.assertEqual(request_mock.call_count, 2)
                sleep.assert_called_once()

    def test_429_is_retried(self):
        ok = FakeHttpResponse(200)
        request_mock = Mock(side_effect=[FakeHttpResponse(429), ok])

        with patch("http_requests.time.sleep") as sleep:
            result, _ = _run(request_mock)

        self.assertEqual(result.status_code, 200)
        sleep.assert_called_once()

    def test_permanent_404_is_not_retried(self):
        resp = FakeHttpResponse(404)
        request_mock = Mock(return_value=resp)

        with patch("http_requests.time.sleep") as sleep:
            result, _ = _run(request_mock)

        self.assertIs(result, resp)
        request_mock.assert_called_once()
        sleep.assert_not_called()

    def test_permanent_401_is_not_retried(self):
        resp = FakeHttpResponse(401)
        request_mock = Mock(return_value=resp)

        with patch("http_requests.time.sleep") as sleep:
            result, _ = _run(request_mock)

        self.assertIs(result, resp)
        request_mock.assert_called_once()
        sleep.assert_not_called()

    def test_403_is_not_retried_by_default(self):
        resp = FakeHttpResponse(403)
        request_mock = Mock(return_value=resp)

        with patch("http_requests.time.sleep") as sleep:
            result, _ = _run(request_mock)

        self.assertIs(result, resp)
        request_mock.assert_called_once()
        sleep.assert_not_called()

    def test_403_is_retried_only_with_explicit_aic_configuration(self):
        ok = FakeHttpResponse(200)
        request_mock = Mock(side_effect=[FakeHttpResponse(403), ok])

        with patch("http_requests.time.sleep") as sleep:
            result, _ = _run(
                request_mock,
                retryable_status_codes=AIC_METADATA_RETRYABLE_STATUS_CODES,
            )

        self.assertEqual(result.status_code, 200)
        self.assertEqual(request_mock.call_count, 2)
        sleep.assert_called_once()

    def test_arbitrary_exceptions_are_not_retried(self):
        request_mock = Mock(side_effect=ValueError("bug"))

        with patch("http_requests.time.sleep") as sleep:
            with self.assertRaises(ValueError):
                _run(request_mock)

        request_mock.assert_called_once()
        sleep.assert_not_called()

    def test_discarded_responses_are_closed_final_is_not(self):
        blocked = FakeHttpResponse(503)
        middle = FakeHttpResponse(503)
        final = FakeHttpResponse(503)
        request_mock = Mock(side_effect=[blocked, middle, final])

        with patch("http_requests.time.sleep"):
            result, _ = _run(request_mock)

        blocked.close.assert_called_once()
        middle.close.assert_called_once()
        self.assertIs(result, final)
        final.close.assert_not_called()


class TestBackoffAndRetryAfter(unittest.TestCase):

    NO_JITTER = staticmethod(lambda a, b: 0)

    def test_delay_increases_exponentially(self):
        request_mock = Mock(side_effect=[
            FakeHttpResponse(503), FakeHttpResponse(503), FakeHttpResponse(503),
        ])

        with patch("http_requests.time.sleep") as sleep, \
                patch("http_requests.random.uniform", self.NO_JITTER):
            _run(request_mock)

        delays = [c.args[0] for c in sleep.call_args_list]
        self.assertAlmostEqual(delays[0], 0.5)
        self.assertAlmostEqual(delays[1], 1.0)
        self.assertGreater(delays[1], delays[0])

    def test_delay_cap_applies(self):
        # base = 0.5 * 2**5 = 16s → MAX_RETRY_DELAY_SECONDS ile sınırlanır.
        for attempt in (1, 5, 6, 10):
            with self.subTest(attempt=attempt):
                delay = _transient_delay(attempt, jitter_fn=lambda a, b: b)
                self.assertLessEqual(delay, 10.0)
        self.assertAlmostEqual(_transient_delay(6, jitter_fn=lambda a, b: b), 10.0)

    def test_jitter_is_bounded(self):
        for attempt in (1, 2, 3):
            with self.subTest(attempt=attempt):
                base = 0.5 * (2 ** (attempt - 1))
                for _ in range(50):
                    delay = _transient_delay(attempt)
                    self.assertGreaterEqual(delay, base)
                    self.assertLessEqual(delay, base * (1 + 0.25) + 1e-9)

    def test_numeric_retry_after_is_respected(self):
        resp = FakeHttpResponse(429, headers={"Retry-After": "2"})
        request_mock = Mock(side_effect=[resp, FakeHttpResponse(200)])

        with patch("http_requests.time.sleep") as sleep:
            _run(request_mock)

        sleep.assert_called_once_with(2.0)

    def test_excessive_retry_after_is_capped(self):
        self.assertEqual(parse_retry_after("120"), MAX_RETRY_AFTER_SECONDS)
        resp = FakeHttpResponse(429, headers={"Retry-After": "120"})
        request_mock = Mock(side_effect=[resp, FakeHttpResponse(200)])

        with patch("http_requests.time.sleep") as sleep:
            _run(request_mock)

        sleep.assert_called_once_with(MAX_RETRY_AFTER_SECONDS)

    def test_malformed_retry_after_falls_back_to_backoff(self):
        resp = FakeHttpResponse(503, headers={"Retry-After": "soon"})
        request_mock = Mock(side_effect=[resp, FakeHttpResponse(200)])

        with patch("http_requests.time.sleep") as sleep, \
                patch("http_requests.random.uniform", self.NO_JITTER):
            _run(request_mock)

        sleep.assert_called_once_with(0.5)

    def test_negative_retry_after_is_ignored(self):
        self.assertIsNone(parse_retry_after("-5"))
        resp = FakeHttpResponse(503, headers={"Retry-After": "-5"})
        request_mock = Mock(side_effect=[resp, FakeHttpResponse(200)])

        with patch("http_requests.time.sleep") as sleep, \
                patch("http_requests.random.uniform", self.NO_JITTER):
            _run(request_mock)

        sleep.assert_called_once_with(0.5)

    def test_retry_after_ignored_for_other_statuses(self):
        # 500 için Retry-After anlamlı değildir; normal backoff kullanılır.
        resp = FakeHttpResponse(500, headers={"Retry-After": "2"})
        request_mock = Mock(side_effect=[resp, FakeHttpResponse(200)])

        with patch("http_requests.time.sleep") as sleep, \
                patch("http_requests.random.uniform", self.NO_JITTER):
            _run(request_mock)

        sleep.assert_called_once_with(0.5)


class TestSafeUrlLogging(unittest.TestCase):

    def test_url_logging_strips_credentials_query_and_fragment(self):
        url = "https://user:password@example.com:8443/art/image.jpg?token=secret#detail"

        safe_url = safe_url_for_logging(url)

        self.assertEqual(safe_url, "https://example.com:8443/art/image.jpg")
        self.assertNotIn("user", safe_url)
        self.assertNotIn("password", safe_url)
        self.assertNotIn("secret", safe_url)

    def test_image_download_failure_does_not_log_sensitive_url_parts(self):
        url = "https://user:password@example.com/missing.jpg?token=secret#detail"
        not_found = Mock(status_code=404, headers={})
        not_found.close.side_effect = RuntimeError(
            "close failed for https://example.com/missing.jpg?token=secret"
        )

        with patch.object(image_processor.requests, "get", return_value=not_found), \
                self.assertLogs("artfolio_bot.image_processor", level="INFO") as captured:
            path = image_processor.download_image(url)

        logs = "\n".join(captured.output)
        self.assertIsNone(path)
        self.assertIn("https://example.com/missing.jpg", logs)
        for sensitive_value in ("user", "password", "token", "secret", "#detail"):
            self.assertNotIn(sensitive_value, logs)


class TestNetworkIntegrationRegression(unittest.TestCase):
    """Müze/görsel kaynaklarının paylaşılan politika ile uyumu."""

    def setUp(self):
        apply_mock_tumblr_credentials(config)

    def test_museum_search_endpoints_use_shared_policy(self):
        """met/cma/smk/harvard aramaları paylaşılan yardımcıya bağlanmış olmalı."""
        from museum_api import MuseumAPIClient

        client = MuseumAPIClient()
        timeout_blocked = Mock(status_code=503)
        success = Mock(status_code=200)
        success.json.return_value = {"objectIDs": []}
        client.session.get = Mock(side_effect=[timeout_blocked, success])

        with patch("http_requests.time.sleep") as sleep:
            artwork = client.fetch_cma_artwork(posted_ids=set())

        self.assertIsNone(artwork)  # boş sonuç listesi → eser yok (ama retry çalıştı)
        self.assertEqual(client.session.get.call_count, 2)
        sleep.assert_called_once()

    def test_museum_timeout_exhaustion_falls_through_cleanly(self):
        """Kaynak timeout'u tükenirse fetcher None döner; seçim diğer müzeye geçebilir."""
        from museum_api import MuseumAPIClient

        client = MuseumAPIClient()
        client.session.get = Mock(side_effect=requests.ConnectionError("down"))

        with patch("http_requests.time.sleep"):
            artwork = client.fetch_smk_artwork(posted_ids=set())

        self.assertIsNone(artwork)
        self.assertEqual(client.session.get.call_count, MAX_TRANSIENT_HTTP_ATTEMPTS)

    def test_image_download_timeout_is_retried(self):
        ok = Mock(status_code=200, headers={"Content-Type": "image/jpeg"})
        ok.iter_content = Mock(return_value=iter([b"jpegdata"]))
        with patch.object(image_processor.requests, "get", side_effect=[requests.Timeout("t"), ok]) as get, \
                patch("http_requests.time.sleep") as sleep:
            path = image_processor.download_image("https://example.com/image")

        # Asıl iddia: transport hatasında 2. deneme yapılır ve tek sleep vardır.
        self.assertEqual(get.call_count, 2)
        sleep.assert_called_once()
        if path:
            import os
            os.unlink(path)

    def test_image_download_permanent_404_does_not_retry(self):
        not_found = Mock(status_code=404, headers={})
        with patch.object(image_processor.requests, "get", return_value=not_found) as get, \
                patch("http_requests.time.sleep") as sleep:
            path = image_processor.download_image("https://example.com/missing.jpg")

        self.assertIsNone(path)
        self.assertEqual(get.call_count, 1)
        sleep.assert_not_called()

    def test_tumblr_create_photo_is_never_auto_retried(self):
        """Tumblr oluşturma isteği idempotent değildir: taşıma hatasında tek deneme."""
        poster = TumblrPoster()
        poster.client = Mock()
        poster.client.create_photo.side_effect = requests.ConnectionError("transport down")
        artwork = Mock(spec=[
            "is_publishable", "museum", "id", "title", "artist", "artist_bio", "date",
            "image_url", "original_source_url", "museum_name", "location_info",
            "dimensions", "medium_type", "raw_medium", "score", "style_or_era",
        ])
        artwork.is_publishable = True
        artwork.museum = "met"
        artwork.id = "t-1"
        artwork.title = "Test"
        artwork.artist = "Artist"
        artwork.artist_bio = "Artist"
        artwork.date = "1900"
        artwork.image_url = "https://example.com/image.jpg"
        artwork.original_source_url = "https://example.com/artwork"
        artwork.museum_name = "Test Museum"
        artwork.location_info = "Gallery"
        artwork.dimensions = "10 cm"
        artwork.medium_type = "Painting"
        artwork.raw_medium = "Oil on canvas"
        artwork.score = 90
        artwork.style_or_era = "Renaissance"

        with patch("http_requests.time.sleep") as sleep:
            result = poster.post_artwork(artwork)

        self.assertIsInstance(result, PublishResult)
        self.assertFalse(result.success)
        self.assertEqual(result.error, "exception:ConnectionError")
        self.assertEqual(poster.client.create_photo.call_count, 1)  # retry YOK
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
