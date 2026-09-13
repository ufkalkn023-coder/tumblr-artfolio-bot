"""Kaynak adaptör tabanı: ortak stats/telemetri/health altyapısı.

Bir adaptör SAHİP olur: sağlayıcı uçları, sorgu kurulumu, HTTP çağrıları,
metadata ayrıştırma, hak eşlemesi, Artwork normalizasyonu, aday filtreleme
ve sağlayıcıya özgü istatistikler.

Bir adaptör SAHİP OLMAZ: Tumblr yayıncılığı, global kaynak rotasyonu,
içerik ağırlıklandırması, yayın günlüğü, genel puanlama politikası veya
global seçim orkestrasyonu.
"""

import logging
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Set

from artfolio.domain.artwork import Artwork
from artfolio.scoring.scorer import ArtworkScorer

# Üretim log kimliği korunur: mevcut yapılandırılmış log çıktısı ve
# assertLogs tabanlı regresyon testleri bu isme bağlıdır.
logger = logging.getLogger("artfolio_bot.museum_api")

HTTP_TIMEOUT = 20

_ERROR_REASON_LIMIT = 120
_URL_PATTERN = re.compile(r"https?://\S+")


class SourceAttemptStatus(str, Enum):
    """Bir fetch denemesinin operasyonel sağlık sonucu.

    SUCCESS  -> HTTP/API çalıştı, yanıt ayrıştırıldı (içerik sonucu ne olursa olsun)
    FAILURE  -> operasyonel arıza (transport, bozuk yanıt, beklenmeyen hata)
    DISABLED -> yapılandırma nedeniyle çalıştırılmadı (örn. eksik API anahtarı)
    """

    SUCCESS = "success"
    FAILURE = "failure"
    DISABLED = "disabled"


@dataclass
class SourceFetchResult:
    """Son fetch denemesinin operasyonel sonucu.

    NOT: içerik sonuçları (kalite/hak/duplicate reddi, boş sonuç, hedef tür
    uyuşmazlığı) operasyonel FAILURE değildir; bunlar SUCCESS altında kalır.
    """

    status: SourceAttemptStatus = SourceAttemptStatus.SUCCESS
    failure_reason: str = ""


class CandidateExclusions(set):
    """Selection exclusions with persisted duplicate history kept distinct."""

    def __init__(self, persisted_ids=()):
        persisted = {str(value) for value in persisted_ids}
        super().__init__(persisted)
        self.persisted_ids = frozenset(persisted)

    def add_transient(self, artwork_id):
        """Exclude a candidate within this selection run without calling it a duplicate."""
        self.add(str(artwork_id))


def is_persisted_duplicate(exclusions, artwork_id) -> bool:
    """Return whether an ID belongs to durable duplicate/reservation history."""
    persisted_ids = getattr(exclusions, "persisted_ids", exclusions)
    return str(artwork_id) in persisted_ids


def _sanitize_reason(reason, limit: int = _ERROR_REASON_LIMIT) -> str:
    """Neden etiketini güvenli kılar: URL'ler (<url>) kırpılır, tek satır, sınırlı uzunluk."""
    text = _URL_PATTERN.sub("<url>", str(reason or ""))
    return " ".join(text.split())[:limit]


class MuseumSource:
    """Tüm müze kaynak adaptörlerinin ortak tabanı."""

    source_id: str = ""
    museum_name: str = ""

    def __init__(self, session, scoring_telemetry, pool_telemetry, pool_coverage):
        self.session = session
        self.scoring_telemetry = scoring_telemetry
        self.pool_telemetry = pool_telemetry
        self.pool_coverage = pool_coverage
        self.last_fetch_stats = {}
        self.last_fetch_health = SourceFetchResult()

    def fetch_artwork(self, posted_ids: Set[str], target_medium: str = None) -> Optional[Artwork]:
        raise NotImplementedError

    # --- Operasyonel sağlık kaydı ----------------------------------------
    # İyimser varsayılan: _start_fetch_stats her fetch'in başında çağrılır ve
    # sonucu SUCCESS'e sıfırlar; yalnızca operasyonel arıza noktaları bunu
    # FAILURE ile ezer. return Artwork yolunda açık SUCCESS teyidi yapılır.
    def _record_health_success(self):
        self.last_fetch_health = SourceFetchResult(status=SourceAttemptStatus.SUCCESS)

    def _record_health_failure(self, reason):
        self.last_fetch_health = SourceFetchResult(
            status=SourceAttemptStatus.FAILURE,
            failure_reason=_sanitize_reason(reason),
        )

    def _record_health_disabled(self):
        self.last_fetch_health = SourceFetchResult(status=SourceAttemptStatus.DISABLED)

    # --- Fetch istatistikleri -------------------------------------------
    def _start_fetch_stats(self):
        self.last_fetch_health = SourceFetchResult()
        self.last_fetch_stats = {
            "source": self.source_id,
            "candidates": 0,
            "duplicates": 0,
            "rejected_image": 0,
            "rejected_quality": 0,
            "rejected_other": 0,
            "rejected_rights": 0,
            "eligible": 0,
        }
        return self.last_fetch_stats

    def _log_fetch_stats(self, stats=None):
        stats = stats or self.last_fetch_stats
        logger.info(
            "source=%s candidates=%d duplicates=%d rejected_image=%d "
            "rejected_quality=%d rejected_other=%d eligible=%d",
            stats.get("source", "unknown"),
            stats.get("candidates", 0),
            stats.get("duplicates", 0),
            stats.get("rejected_image", 0),
            stats.get("rejected_quality", 0),
            stats.get("rejected_other", 0),
            stats.get("eligible", 0),
        )

    # --- Pool telemetrisi -------------------------------------------------
    def _set_pool_coverage(self, source: str, coverage: str, materialized: int):
        existing = self.pool_coverage.get(source)
        if existing is None:
            self.pool_coverage[source] = {
                "coverage": coverage,
                "materialized": materialized,
            }
            return

        existing["materialized"] += materialized
        if existing["coverage"] != coverage:
            existing["coverage"] = "partial"

    def _safe_pool_operation(self, operation, *args):
        """Telemetry failures must never change the production fetch path."""
        try:
            operation(*args)
        except Exception as exc:
            logger.debug("pool_telemetry operation_skipped type=%s", type(exc).__name__)

    def _record_pool_candidate(
        self,
        source: str,
        title: str,
        artist: str,
        date_str: str,
        raw_medium: str,
        classification: str,
        object_name: str,
        image_url: str,
        is_highlight: bool = False,
        has_additional_images: bool = False,
        on_view: bool = False,
    ):
        """Score one already materialized candidate for shadow telemetry only."""
        try:
            self.pool_telemetry.record_evaluated(source)
            if not image_url:
                return
            score, _ = ArtworkScorer.calculate_score(
                title=title or "Untitled",
                artist=artist or "Unknown Artist",
                date_str=date_str or "Unknown Date",
                raw_medium=raw_medium or "",
                classification=classification or "",
                object_name=object_name or "",
                image_url=image_url,
                is_highlight=is_highlight,
                has_additional_images=has_additional_images,
                on_view=on_view,
            )
            self.pool_telemetry.record_scored(
                source, raw_medium or "", classification or "", object_name or "", artist or "Unknown Artist", score,
                is_highlight, on_view, has_additional_images,
            )
        except Exception as exc:
            logger.debug("pool_telemetry source=%s candidate_score_skipped type=%s", source, type(exc).__name__)
