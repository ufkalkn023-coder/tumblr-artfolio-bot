"""
test_publication_state.py - Yayın durumu / duplicate-post koruması regresyon testleri.

Kapsam:
  - PublicationStateStore: yükleme, atomik yazma, bozuk state fail-closed, TTL rezervasyon
  - PublishResult sözleşmesi (Tumblr publisher)
  - main.run_curation_cycle orkestrasyonu (rezervasyon → yayın → journal/legacy güncelleme)
  - Workflow state dosyalarının doğruluğu
Gerçek ağ çağrısı yapılmaz; tüm dış etkiler mock/geçici dosyadır.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import config
from _mock_tumblr_credentials import apply_mock_tumblr_credentials
import main
from museum_api import Artwork
from publication_state import (
    PublicationRecord,
    PublicationStateStore,
    StateCorruptionError,
    _parse_utc,
)
from tumblr_poster import PublishResult, TumblrPoster


def make_artwork(artwork_id="123", museum="met", score=95):
    return Artwork(
        museum=museum, id=artwork_id, title="Test Artwork", artist="Artist",
        artist_bio="Artist", date="1900", image_url="https://example.com/image.jpg",
        original_source_url="https://example.com/artwork", museum_name="Test Museum",
        location_info="Gallery", dimensions="10 cm", medium_type="Painting",
        raw_medium="Oil on canvas", score=score, is_public_domain=True,
    )


def published_record(museum="met", artwork_id="1"):
    return {
        "museum": museum, "artwork_id": artwork_id, "status": "published",
        "reserved_at": datetime.now(timezone.utc).isoformat(),
        "published_at": datetime.now(timezone.utc).isoformat(),
        "tumblr_post_id": "999", "score": 95, "title": "Old", "error": "",
    }


def reserved_record(museum="met", artwork_id="1", age_minutes=0):
    reserved_at = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    return {
        "museum": museum, "artwork_id": artwork_id, "status": "reserved",
        "reserved_at": reserved_at.isoformat(),
        "published_at": "", "tumblr_post_id": "", "score": 95, "title": "T", "error": "",
    }


class TestPublicationStateStore(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.state_path = self.tmp_dir / "publication_state.json"

    def test_missing_state_file_initializes_safely(self):
        """Eksik state dosyası güvenli boş başlangıç yapar ve dosya oluşturmaz (lazy)."""
        store = PublicationStateStore(self.state_path).load()
        self.assertEqual(store.active_blocks(), {})
        self.assertIsNone(store.get_record("met", "123"))
        self.assertFalse(self.state_path.exists())

    def test_valid_state_loads(self):
        self.state_path.write_text(json.dumps({
            "schema_version": 1,
            "records": {"met:1": published_record()},
        }), encoding="utf-8")
        store = PublicationStateStore(self.state_path).load()
        record = store.get_record("met", "1")
        self.assertIsInstance(record, PublicationRecord)
        self.assertEqual(record.status, "published")
        self.assertEqual(record.tumblr_post_id, "999")

    def test_legacy_record_without_diversity_metadata_still_loads(self):
        self.state_path.write_text(json.dumps({
            "schema_version": 1,
            "records": {"met:1": published_record()},
        }), encoding="utf-8")

        record = PublicationStateStore(self.state_path).load().get_record("met", "1")

        self.assertEqual(record.artist_name, "")
        self.assertEqual(record.artist_key, "")
        self.assertEqual(record.medium_type, "")

    def test_malformed_state_fails_closed(self):
        cases = [
            "{not valid json",
            json.dumps({"schema_version": 2, "records": {}}),
            json.dumps({"schema_version": 1, "records": {"met:1": {"status": "bogus"}}}),
            json.dumps({"schema_version": 1, "records": ["not", "a", "dict"]}),
            json.dumps({"schema_version": 1, "records": {
                "met:1": dict(published_record(), reserved_at="2026-01-01T00:00:00"),  # naive
            }}),
        ]
        for payload in cases:
            with self.subTest(payload=payload[:40]):
                self.state_path.write_text(payload, encoding="utf-8")
                with self.assertRaises(StateCorruptionError):
                    PublicationStateStore(self.state_path).load()

    def test_record_key_must_match_record_identity(self):
        self.state_path.write_text(json.dumps({
            "schema_version": 1,
            "records": {"met:different": published_record(artwork_id="1")},
        }), encoding="utf-8")

        with self.assertRaises(StateCorruptionError):
            PublicationStateStore(self.state_path).load()

    def test_atomic_save_works(self):
        store = PublicationStateStore(self.state_path).load()
        store.reserve(make_artwork())
        store.save_atomic()
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], 1)
        self.assertIn("met:123", data["records"])

    def test_temporary_file_cleanup_on_save(self):
        store = PublicationStateStore(self.state_path).load()
        store.reserve(make_artwork())
        store.save_atomic()
        leftovers = list(self.tmp_dir.glob(".publication_state.json.*.tmp"))
        self.assertEqual(leftovers, [])

    def test_temporary_file_cleanup_on_save_failure(self):
        store = PublicationStateStore(self.state_path).load()
        store.reserve(make_artwork())
        with patch("publication_state.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                store.save_atomic()
        leftovers = list(self.tmp_dir.glob(".publication_state.json.*.tmp"))
        self.assertEqual(leftovers, [])

    def test_identity_uses_museum_and_artwork_id(self):
        store = PublicationStateStore(self.state_path).load()
        store._records["met:1"] = published_record(artwork_id="1")
        store._records["met:2"] = published_record(artwork_id="2")
        self.assertTrue(store.is_blocked("met", "1"))
        self.assertTrue(store.is_blocked("met", "2"))
        self.assertFalse(store.is_blocked("aic", "1"))  # aynı ID başka müzede serbest
        self.assertFalse(store.is_blocked("met", "3"))
        self.assertEqual(PublicationStateStore.identity_key("met", "7"), "met:7")

    def test_reserve_creates_record(self):
        store = PublicationStateStore(self.state_path).load()
        record = store.reserve(make_artwork())
        self.assertIsNotNone(record)
        self.assertEqual(record.status, "reserved")
        self.assertEqual(store.get_record("met", "123").status, "reserved")
        # ISO + timezone bilgisi taşımalı
        _parse_utc(record.reserved_at)

    def test_diversity_metadata_is_stored_only_when_published(self):
        artwork = make_artwork()
        artwork.artist = "  Claude   MONET  "
        store = PublicationStateStore(self.state_path).load()

        reserved = store.reserve(artwork)
        published = store.mark_published("met", "123", "post-77", artwork=artwork)

        self.assertEqual(reserved.artist_name, "")
        self.assertEqual(reserved.artist_key, "")
        self.assertEqual(reserved.medium_type, "")
        self.assertEqual(published.artist_name, "  Claude   MONET  ")
        self.assertEqual(published.artist_key, "claude monet")
        self.assertEqual(published.medium_type, "Painting")

    def test_duplicate_active_reservation_rejected(self):
        store = PublicationStateStore(self.state_path).load()
        self.assertIsNotNone(store.reserve(make_artwork()))
        self.assertIsNone(store.reserve(make_artwork()))
        # Blok log'u üretildi mi?
        with self.assertLogs("artfolio_bot.publication_state", level="WARNING") as captured:
            store.reserve(make_artwork())
        self.assertTrue(any("publication_reservation_blocked source=met object_id=123" in line for line in captured.output))

    def test_published_record_blocked(self):
        store = PublicationStateStore(self.state_path).load()
        store._records["met:123"] = published_record(artwork_id="123")
        self.assertTrue(store.is_blocked("met", "123"))
        self.assertIsNone(store.reserve(make_artwork()))

    def test_failed_record_retryable(self):
        store = PublicationStateStore(self.state_path).load()
        store.reserve(make_artwork())
        store.mark_failed("met", "123", error="tumblr api error")
        self.assertFalse(store.is_blocked("met", "123"))
        # Retry yeniden rezervasyon yapabilir
        self.assertIsNotNone(store.reserve(make_artwork()))

    def test_expired_reservation_retryable(self):
        store = PublicationStateStore(self.state_path).load()
        store._records["met:123"] = reserved_record(artwork_id="123", age_minutes=3 * 60)
        with self.assertLogs("artfolio_bot.publication_state", level="WARNING") as captured:
            blocked = store.is_blocked("met", "123")
        self.assertFalse(blocked)
        self.assertTrue(any("publication_reservation_expired source=met object_id=123" in line for line in captured.output))
        self.assertNotIn("met", store.active_blocks())

    def test_unexpired_reservation_blocked(self):
        store = PublicationStateStore(self.state_path).load()
        store._records["met:123"] = reserved_record(artwork_id="123", age_minutes=10)
        self.assertTrue(store.is_blocked("met", "123"))
        self.assertEqual(store.active_blocks(), {"met": ["123"]})

    def test_ttl_respects_configured_value(self):
        store = PublicationStateStore(self.state_path).load()
        store._records["met:123"] = reserved_record(artwork_id="123", age_minutes=30)
        self.assertTrue(store.is_blocked("met", "123"))
        with patch.object(config, "PUBLICATION_RESERVATION_TTL_MINUTES", 15):
            self.assertFalse(store.is_blocked("met", "123"))

    def test_utc_timestamp_parsing(self):
        parsed = _parse_utc("2026-09-13T10:00:00+02:00")
        self.assertEqual(parsed.utcoffset(), timedelta(0))
        self.assertEqual(parsed.hour, 8)  # UTC'ye çevrilir
        with self.assertRaises(ValueError):
            _parse_utc("2026-09-13T10:00:00")  # naive reddedilir

    def test_mark_published_sets_post_id_and_timestamp(self):
        store = PublicationStateStore(self.state_path).load()
        store.reserve(make_artwork())
        record = store.mark_published("met", "123", "post-77")
        self.assertEqual(record.status, "published")
        self.assertEqual(record.tumblr_post_id, "post-77")
        self.assertTrue(record.published_at)
        self.assertTrue(store.is_blocked("met", "123"))

    def test_mark_failed_sanitizes_and_stores_error(self):
        store = PublicationStateStore(self.state_path).load()
        store.reserve(make_artwork())
        record = store.mark_failed("met", "123", error="line1\nline2  with   spaces  " + "x" * 500)
        self.assertEqual(record.status, "failed")
        self.assertEqual(record.artist_key, "")
        self.assertEqual(record.medium_type, "")
        self.assertNotIn("\n", record.error)
        self.assertLessEqual(len(record.error), 300)

    def test_mark_uncertain_keeps_reservation_blocked_until_ttl(self):
        """Belirsiz sonuç (transport hatası): durum 'reserved' kalır, TTL içinde bloklanır."""
        store = PublicationStateStore(self.state_path).load()
        store.reserve(make_artwork())
        record = store.mark_uncertain("met", "123", error="exception:ConnectionError")
        self.assertEqual(record.status, "reserved")  # failed'e çevrilmez
        self.assertEqual(record.artist_key, "")
        self.assertEqual(record.medium_type, "")
        self.assertEqual(record.error, "exception:ConnectionError")
        self.assertTrue(store.is_blocked("met", "123"))  # TTL içinde tekrar seçilemez
        self.assertTrue(store.is_blocked("met", "123", now=datetime.now(timezone.utc) + timedelta(minutes=119)))
        self.assertFalse(store.is_blocked("met", "123", now=datetime.now(timezone.utc) + timedelta(minutes=121)))

    def test_recent_published_is_newest_first_and_excludes_other_statuses(self):
        store = PublicationStateStore(self.state_path).load()
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        store._records = {
            "met:old": dict(published_record(artwork_id="old"), published_at=base.isoformat()),
            "met:failed": dict(published_record(artwork_id="failed"), status="failed",
                               published_at=(base + timedelta(days=4)).isoformat()),
            "met:new": dict(published_record(artwork_id="new"),
                            published_at=(base + timedelta(days=2)).isoformat()),
            "met:reserved": dict(reserved_record(artwork_id="reserved"),
                                 published_at=(base + timedelta(days=3)).isoformat()),
            "met:middle": dict(published_record(artwork_id="middle"),
                               published_at=(base + timedelta(days=1)).isoformat()),
        }

        records = store.get_recent_published()

        self.assertEqual([record.artwork_id for record in records], ["new", "middle", "old"])

    def test_recent_published_skips_malformed_legacy_timestamps_safely(self):
        store = PublicationStateStore(self.state_path).load()
        valid = dict(published_record(artwork_id="valid"),
                     published_at="2026-01-02T00:00:00+00:00")
        malformed = dict(published_record(artwork_id="malformed"), published_at="not-a-time")
        missing = published_record(artwork_id="missing")
        missing.pop("published_at")
        store._records = {
            "met:malformed": malformed,
            "met:missing": missing,
            "met:valid": valid,
        }

        with self.assertLogs("artfolio_bot.publication_state", level="WARNING"):
            records = store.get_recent_published()

        self.assertEqual([record.artwork_id for record in records], ["valid"])
        self.assertEqual(store.get_recent_published(limit=0), [])

    def test_recent_published_orders_offsets_by_instant_not_text(self):
        store = PublicationStateStore(self.state_path).load()
        store._records = {
            "met:older": dict(published_record(artwork_id="older"),
                              published_at="2026-01-01T12:00:00+02:00"),
            "met:newer": dict(published_record(artwork_id="newer"),
                              published_at="2026-01-01T10:30:00+00:00"),
        }

        self.assertEqual(
            [record.artwork_id for record in store.get_recent_published()],
            ["newer", "older"],
        )

    def test_failed_and_uncertain_records_never_enter_diversity_history(self):
        store = PublicationStateStore(self.state_path).load()
        store.reserve(make_artwork("failed"))
        store.mark_failed("met", "failed", "forbidden")
        store.reserve(make_artwork("uncertain"))
        store.mark_uncertain("met", "uncertain", "exception:Timeout")

        self.assertEqual(store.get_recent_published(), [])


class TestPublishResultContract(unittest.TestCase):

    def setUp(self):
        apply_mock_tumblr_credentials(config)
        self.poster = TumblrPoster()
        self.poster.client = Mock()

    def test_tumblr_success_returns_structured_result(self):
        self.poster.client.create_photo.return_value = {"id": 424242}
        result = self.poster.post_artwork(make_artwork())
        self.assertIsInstance(result, PublishResult)
        self.assertTrue(result.success)
        self.assertEqual(result.post_id, "424242")  # str'e normalize edilir
        self.assertTrue(bool(result))  # geriye dönük bool uyumluluğu

    def test_tumblr_api_failure_returns_structured_failure(self):
        self.poster.client.create_photo.return_value = {
            "meta": {"status": 403, "msg": "You are not authorized"}
        }
        result = self.poster.post_artwork(make_artwork())
        self.assertFalse(result.success)
        self.assertEqual(result.status_code, 403)
        self.assertEqual(result.error, "You are not authorized")

    def test_rights_gate_blocks_before_tumblr(self):
        artwork = make_artwork()
        artwork.is_public_domain = False
        result = self.poster.post_artwork(artwork)
        self.assertIsInstance(result, PublishResult)
        self.assertFalse(result.success)
        self.assertEqual(result.error, "rights_not_publishable")
        self.poster.client.create_photo.assert_not_called()

    def test_no_credentials_leak_into_error_or_logs(self):
        self.poster.client.create_photo.return_value = {
            "meta": {"status": 500, "msg": "internal error"}
        }
        with self.assertLogs("artfolio_bot.tumblr_poster", level="ERROR") as captured:
            result = self.poster.post_artwork(make_artwork())
        secrets = (
            config.TUMBLR_CONSUMER_KEY, config.TUMBLR_CONSUMER_SECRET,
            config.TUMBLR_OAUTH_TOKEN, config.TUMBLR_OAUTH_SECRET,
        )
        serialized_logs = "\n".join(captured.output)
        self.assertFalse(result.success)
        for secret in secrets:
            self.assertNotIn(secret, result.error)
            self.assertNotIn(secret, serialized_logs)

    def test_unexpected_response_handled_safely(self):
        self.poster.client.create_photo.return_value = ["weird", "shape"]
        result = self.poster.post_artwork(make_artwork())
        self.assertFalse(result.success)
        self.assertEqual(result.error, "unexpected_response")

    def test_exception_is_recorded_as_uncertain_failure(self):
        self.poster.client.create_photo.side_effect = ConnectionError("connection reset")
        result = self.poster.post_artwork(make_artwork())
        self.assertFalse(result.success)
        self.assertEqual(result.error, "exception:ConnectionError")


class TestMainPublishFlow(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self.state_path = self.tmp_dir / "publication_state.json"
        self.artwork = make_artwork("123")
        self.client = Mock()
        self.client.get_random_artwork.return_value = self.artwork
        self.client.last_run_stats = {}
        self.poster = Mock()
        self.poster.post_artwork.return_value = PublishResult(success=True, post_id="post-9")
        self.save_ids = Mock()
        self.telemetry = Mock()

    def _run(
        self,
        posted_ids,
        poster=None,
        store_override=None,
        *,
        effect="none",
        downloaded_path="/tmp/fake_main.jpg",
    ):
        poster = poster or self.poster
        with patch("main.time.monotonic", side_effect=(100.0, 101.0, 102.0)), \
                patch("main.time.sleep"), \
                patch("random.choices", side_effect=(["Painting"], [effect])), \
                patch("main.load_posted_ids", return_value=posted_ids), \
                patch("main.save_posted_ids", self.save_ids), \
                patch("main.MuseumAPIClient", return_value=self.client), \
                patch("main.TumblrPoster", return_value=poster), \
                patch("main.export_scoring_telemetry", self.telemetry), \
                patch.object(main.image_processor, "download_image", return_value=downloaded_path), \
                patch.object(config, "PUBLICATION_STATE_FILE", self.state_path):
            if store_override:
                with patch("main.PublicationStateStore", return_value=store_override):
                    main.run_curation_cycle()
            else:
                main.run_curation_cycle()

    def test_selected_artwork_log_does_not_expose_sensitive_url_parts(self):
        self.artwork.image_url = (
            "https://user:password@example.com/image.jpg?token=secret#detail"
        )

        with self.assertLogs("artfolio_bot", level="INFO") as captured:
            self._run({
                "met": [], "aic": [], "cma": [], "rijksmuseum": [],
                "smk": [], "harvard": [],
            })

        logs = "\n".join(captured.output)
        for sensitive_value in ("user", "password", "token", "secret", "#detail"):
            self.assertNotIn(sensitive_value, logs)

    def test_passepartout_publish_cleans_original_and_derivative_files(self):
        original = self.tmp_dir / "original.jpg"
        derivative = self.tmp_dir / "framed.jpg"
        original.write_bytes(b"original")
        derivative.write_bytes(b"derivative")

        with patch.object(
            main.image_processor,
            "add_passepartout",
            return_value=str(derivative),
        ):
            self._run(
                {
                    "met": [], "aic": [], "cma": [], "rijksmuseum": [],
                    "smk": [], "harvard": [],
                },
                effect="passepartout",
                downloaded_path=str(original),
            )

        self.assertFalse(original.exists())
        self.assertFalse(derivative.exists())

    def test_failed_publish_cleans_downloaded_file(self):
        original = self.tmp_dir / "failed-publish.jpg"
        original.write_bytes(b"original")
        self.poster.post_artwork.return_value = PublishResult(
            success=False,
            status_code=403,
            error="forbidden",
        )

        with self.assertRaises(SystemExit):
            self._run(
                {
                    "met": [], "aic": [], "cma": [], "rijksmuseum": [],
                    "smk": [], "harvard": [],
                },
                downloaded_path=str(original),
            )

        self.assertFalse(original.exists())

    def test_reservation_persisted_before_tumblr_is_called(self):
        observed = {}

        def fake_post(artwork, image_paths=None):
            journal = json.loads(self.state_path.read_text(encoding="utf-8"))
            observed["status_at_publish_time"] = journal["records"]["met:123"]["status"]
            return PublishResult(success=True, post_id="post-9")

        self.poster.post_artwork.side_effect = fake_post
        self._run({"met": [], "aic": [], "cma": [], "rijksmuseum": [], "smk": [], "harvard": []})

        self.assertEqual(observed["status_at_publish_time"], "reserved")
        self.poster.post_artwork.assert_called_once()

    def test_successful_publish_marks_journal_published(self):
        self._run({"met": [], "aic": [], "cma": [], "rijksmuseum": [], "smk": [], "harvard": []})
        record = self._load_journal()["records"]["met:123"]
        self.assertEqual(record["status"], "published")
        self.assertEqual(record["tumblr_post_id"], "post-9")

    def test_successful_publish_writes_diversity_metadata(self):
        self.artwork.artist = "Claude Monet"
        self.artwork.medium_type = "Painting"

        self._run({"met": [], "aic": [], "cma": [], "rijksmuseum": [], "smk": [], "harvard": []})

        record = self._load_journal()["records"]["met:123"]
        self.assertEqual(record["artist_name"], "Claude Monet")
        self.assertEqual(record["artist_key"], "claude monet")
        self.assertEqual(record["medium_type"], "Painting")

    def test_successful_publish_still_updates_legacy_posted_ids(self):
        posted = {"met": [], "aic": [], "cma": [], "rijksmuseum": [], "smk": [], "harvard": []}
        self._run(posted)
        self.save_ids.assert_called_once()
        saved_data = self.save_ids.call_args.args[0]
        self.assertIn("123", saved_data["met"])

    def test_definite_failed_publish_marks_journal_failed(self):
        self.poster.post_artwork.return_value = PublishResult(
            success=False, status_code=403, error="forbidden"
        )
        with self.assertRaises(SystemExit) as ctx:
            self._run({"met": [], "aic": [], "cma": [], "rijksmuseum": [], "smk": [], "harvard": []})
        self.assertEqual(ctx.exception.code, 1)
        record = self._load_journal()["records"]["met:123"]
        self.assertEqual(record["status"], "failed")
        self.assertEqual(record["error"], "forbidden")

    def test_failed_publish_does_not_update_legacy_posted_ids(self):
        self.poster.post_artwork.return_value = PublishResult(success=False, error="forbidden")
        with self.assertRaises(SystemExit):
            self._run({"met": [], "aic": [], "cma": [], "rijksmuseum": [], "smk": [], "harvard": []})
        self.save_ids.assert_not_called()

    def test_uncertain_publish_failure_keeps_reservation_until_ttl(self):
        """Transport hatası (exception:*): istek Tumblr'a ulaşmış olabilir; kayıt
        'reserved' kalır, TTL dolana kadar aynı eser tekrar seçilemez (M1)."""
        self.poster.post_artwork.return_value = PublishResult(
            success=False, error="exception:ConnectionError"
        )
        with self.assertRaises(SystemExit) as ctx:
            self._run({"met": [], "aic": [], "cma": [], "rijksmuseum": [], "smk": [], "harvard": []})
        self.assertEqual(ctx.exception.code, 1)
        record = self._load_journal()["records"]["met:123"]
        self.assertEqual(record["status"], "reserved")  # failed'e çevrilmez
        self.assertEqual(record["error"], "exception:ConnectionError")

    def test_unexpected_response_keeps_reservation_until_ttl(self):
        self.poster.post_artwork.return_value = PublishResult(
            success=False, error="unexpected_response"
        )
        with self.assertRaises(SystemExit):
            self._run({"met": [], "aic": [], "cma": [], "rijksmuseum": [], "smk": [], "harvard": []})
        record = self._load_journal()["records"]["met:123"]
        self.assertEqual(record["status"], "reserved")
        self.assertEqual(record["error"], "unexpected_response")

    def test_journal_save_failure_after_publish_raises_with_reserved_on_disk(self):
        """Crash window B: Tumblr başarılı, journal yazılamadı → hata yükseltilir,
        diskteki kayıt 'reserved' kalır (TTL koruması devrede)."""
        real_save = PublicationStateStore.save_atomic
        save_calls = {"count": 0}

        def flaky_save(store_self):
            save_calls["count"] += 1
            if save_calls["count"] == 2:  # 1. çağrı (rezervasyon) gerçekten yazar; 2. başarısız olur
                raise OSError("disk full")
            return real_save(store_self)

        with patch.object(PublicationStateStore, "save_atomic", flaky_save):
            with self.assertRaises(OSError):
                self._run({"met": [], "aic": [], "cma": [], "rijksmuseum": [], "smk": [], "harvard": []})
        record = self._load_journal()["records"]["met:123"]
        self.assertEqual(record["status"], "reserved")

    def test_posted_ids_save_failure_after_journal_success_raises_with_published_journal(self):
        """Crash window C: journal 'published' olarak güvenli; legacy yazımı başarısız
        olsa bile çalışma hatayla biter ve kayıt kaybolmaz."""
        self.save_ids.side_effect = OSError("disk full")
        with self.assertRaises(OSError):
            self._run({"met": [], "aic": [], "cma": [], "rijksmuseum": [], "smk": [], "harvard": []})
        record = self._load_journal()["records"]["met:123"]
        self.assertEqual(record["status"], "published")
        self.assertEqual(record["tumblr_post_id"], "post-9")

    def test_reservation_persist_failure_means_tumblr_never_called(self):
        store = Mock()
        store.load.return_value = store
        store.active_blocks.return_value = {}
        store.reserve.return_value = Mock()  # rezervasyon oluşur...
        store.save_atomic.side_effect = OSError("disk full")  # ...ama kalıcılaşamaz
        self.poster.post_artwork.return_value = PublishResult(success=True, post_id="post-9")

        with self.assertRaises(SystemExit) as ctx:
            self._run({"met": [], "aic": [], "cma": [], "rijksmuseum": [], "smk": [], "harvard": []},
                      store_override=store)
        self.assertEqual(ctx.exception.code, 1)
        self.poster.post_artwork.assert_not_called()

    def test_state_corruption_prevents_publication(self):
        self.state_path.write_text("{corrupt", encoding="utf-8")
        with patch("main.time.monotonic", side_effect=(100.0, 101.0, 102.0)), \
                patch("main.load_posted_ids", return_value={
                    "met": [], "aic": [], "cma": [], "rijksmuseum": [], "smk": [], "harvard": [],
                }), \
                patch("main.MuseumAPIClient", return_value=self.client), \
                patch.object(config, "PUBLICATION_STATE_FILE", self.state_path), \
                self.assertRaises(SystemExit) as ctx:
            main.run_curation_cycle()
        self.assertEqual(ctx.exception.code, 1)
        self.client.get_random_artwork.assert_not_called()

    def test_legacy_posted_ids_corruption_prevents_publication(self):
        with patch("main.load_posted_ids", side_effect=StateCorruptionError("malformed")), \
                patch("main.MuseumAPIClient", return_value=self.client), \
                self.assertRaises(SystemExit) as ctx:
            main.run_curation_cycle()
        self.assertEqual(ctx.exception.code, 1)
        self.client.get_random_artwork.assert_not_called()

    def test_active_reservation_from_crashed_run_prevents_duplicate_publish(self):
        # Simülasyon: önceki süreç rezervasyon yazdıktan sonra öldü (crash window E).
        self.state_path.write_text(json.dumps({
            "schema_version": 1,
            "records": {"met:123": reserved_record(artwork_id="123", age_minutes=5)},
        }), encoding="utf-8")
        self.poster.post_artwork.return_value = PublishResult(success=True, post_id="post-9")

        with self.assertRaises(SystemExit) as ctx:
            self._run({"met": [], "aic": [], "cma": [], "rijksmuseum": [], "smk": [], "harvard": []})
        self.assertEqual(ctx.exception.code, 1)
        # Rezervasyon seçim listesine girmiş olmalı (duplicate filtresi)...
        selection_arg = self.client.get_random_artwork.call_args.args[0]
        self.assertIn("123", selection_arg["met"])
        # ...ve publish çağrılmamalı (fail-closed).
        self.poster.post_artwork.assert_not_called()

    def test_legacy_posted_id_and_journal_published_both_block_selection(self):
        self.state_path.write_text(json.dumps({
            "schema_version": 1,
            "records": {"met:99": published_record(artwork_id="99")},
        }), encoding="utf-8")
        self.client.get_random_artwork.return_value = None  # seçim yapılamaz
        with patch("main.time.monotonic", side_effect=(100.0, 101.0, 102.0)), \
                patch("main.time.sleep"), \
                patch("random.choices", return_value=["Painting"]), \
                patch("main.load_posted_ids", return_value={
                    "met": ["123"], "aic": [], "cma": [], "rijksmuseum": [], "smk": [], "harvard": [],
                }), \
                patch("main.MuseumAPIClient", return_value=self.client), \
                patch("main.export_scoring_telemetry"), \
                patch.object(config, "PUBLICATION_STATE_FILE", self.state_path), \
                self.assertRaises(SystemExit):
            main.run_curation_cycle()
        # Legacy ID ve journal published ID her ikisi de seçim dışlama listesinde.
        selection_arg = self.client.get_random_artwork.call_args.args[0]
        self.assertIn("123", selection_arg["met"])  # legacy only
        self.assertIn("99", selection_arg["met"])   # journal only

    def test_expired_reservation_allows_retry(self):
        self.state_path.write_text(json.dumps({
            "schema_version": 1,
            "records": {"met:123": reserved_record(artwork_id="123", age_minutes=3 * 60)},
        }), encoding="utf-8")
        self._run({"met": [], "aic": [], "cma": [], "rijksmuseum": [], "smk": [], "harvard": []})
        selection_arg = self.client.get_random_artwork.call_args.args[0]
        self.assertNotIn("123", selection_arg["met"])  # süresi dolduğu için seçilebilir
        record = self._load_journal()["records"]["met:123"]
        self.assertEqual(record["status"], "published")  # başarılı yayınla güncellendi

    def _load_journal(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))


class TestLegacyPostedIds(unittest.TestCase):

    def test_non_list_museum_history_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "posted_ids.json"
            path.write_text(json.dumps({"met": "123"}), encoding="utf-8")

            with patch.object(config, "POSTED_IDS_FILE", path), \
                    self.assertRaises(StateCorruptionError):
                main.load_posted_ids()


class TestWorkflowStatePersistence(unittest.TestCase):

    def test_workflow_commits_both_state_files(self):
        workflow = Path(".github/workflows/tumblr.yml").read_text(encoding="utf-8")
        self.assertIn("publication_state.json", workflow)
        self.assertIn("posted_ids.json", workflow)
        self.assertIn("git add $state_files", workflow)
        self.assertIn("git status --porcelain -- $state_files", workflow)
        # Yanlışlıkla her şeyi stage'lememeli
        self.assertNotIn("git add .", workflow)
        self.assertNotIn("git add -A", workflow)
        # Telemetri / .mimosa stage'lenmemeli
        self.assertNotIn("git add output", workflow)
        self.assertNotIn("git add .mimosa", workflow)

    def test_initial_state_file_is_valid_and_committed_ready(self):
        payload = json.loads(Path("publication_state.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["records"], {})


if __name__ == "__main__":
    unittest.main()
