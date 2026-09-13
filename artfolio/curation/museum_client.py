"""Curation orchestrator: kaynak rotasyonu, aggregate istatistikler, global seçim.

MuseumAPIClient sağlayıcı JSON ayrıştırma İÇERMEZ; her kaynağın fetch mantığı
kendi adaptöründedir (artfolio.sources.*). İstemci; paylaşılan HTTP oturumunu,
telemetri nesnelerini ve pool coverage sözlüğünü adaptörlerle paylaşır.
"""

import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

import config
import requests

from artfolio.domain.artwork import Artwork
from artfolio.curation.diversity import (
    ARTIST_COOLDOWN,
    MEDIUM_CONSECUTIVE_LIMIT,
    SOURCE_CONSECUTIVE_LIMIT,
    FeedDiversityPolicy,
)
from artfolio.scoring.telemetry import CandidatePoolTelemetry, ScoringTelemetry
from artfolio.sources import (
    AICSource,
    CMASource,
    HarvardSource,
    MetSource,
    SMKSource,
)
from artfolio.sources import aic as aic_module
from artfolio.sources.base import CandidateExclusions, SourceAttemptStatus, logger
from artfolio.curation.source_health import SourceHealthManager
from http_requests import JSON_REQUEST_HEADERS

MAX_DIVERSITY_CANDIDATES_PER_SOURCE = 2


class MuseumAPIClient:
    """Kaynak adaptörleri üzerinden eser seçen orkestratör.

    Sahip olduğu sorumluluklar: kaynak sıralaması/rotasyonu, AIC devre dışı
    kuralı, aggregate run istatistikleri, global seçim, telemetri koordinasyonu,
    hedef tür zorlaması ve kaynaklar-arası fallback.
    """

    def __init__(self, recent_published=None, diversity_policy=None):
        self.session = requests.Session()
        self.session.headers.update(JSON_REQUEST_HEADERS)
        self.last_fetch_stats = {}
        self.scoring_telemetry = ScoringTelemetry()
        self.pool_telemetry = CandidatePoolTelemetry()
        self.pool_coverage = {}
        self._aic_publishing_disabled_logged = False
        self.last_run_stats = {}
        self.diversity_policy = diversity_policy or FeedDiversityPolicy(
            recent_published or []
        )

        # Kaynak adaptörleri: paylaşılan oturum + telemetri ile kurulur.
        self._sources = {
            "met": MetSource(self.session, self.scoring_telemetry, self.pool_telemetry, self.pool_coverage),
            "aic": AICSource(self.session, self.scoring_telemetry, self.pool_telemetry, self.pool_coverage),
            "cma": CMASource(self.session, self.scoring_telemetry, self.pool_telemetry, self.pool_coverage),
            "smk": SMKSource(self.session, self.scoring_telemetry, self.pool_telemetry, self.pool_coverage),
            "harvard": HarvardSource(self.session, self.scoring_telemetry, self.pool_telemetry, self.pool_coverage),
        }
        # Kaynak sağlığı: işlem/koşu başına taze state (kalıcılık yok, run'lar arası taşınmaz).
        self.health = SourceHealthManager()

    def get_source_health(self) -> Dict[str, dict]:
        """Kaynak sağlık raporu (serileştirilebilir; izleme amaçlı)."""
        return self.health.snapshot()

    @property
    def session(self):
        return self._session

    @session.setter
    def session(self, new_session):
        """Paylaşılan oturumu tüm adaptörlere yayılır (eski mock yüzeyi korunur)."""
        self._session = new_session
        for source in getattr(self, "_sources", {}).values():
            source.session = new_session

    # ----------------------------------------------------------------------
    # Kaynak delegasyonu (uyumluluk seam'leri: testler bu metotları
    # değiştirebilir; her çağrı sonrası fetch istatistikleri istemciye yansır)
    # ----------------------------------------------------------------------
    def _fetch_via(self, source_id: str, posted_ids: Set[str], target_medium: str = None) -> Optional[Artwork]:
        source = self._sources[source_id]
        artwork = source.fetch_artwork(posted_ids, target_medium)
        self.last_fetch_stats = source.last_fetch_stats
        # Operasyonel sağlık: yalnızca adaptörün bildirdiği outcome sayılır.
        # İçerik sonuçları (boş/kalite/hak/duplicate) SUCCESS'tir; DISABLED kaydolmaz.
        outcome = source.last_fetch_health
        if outcome.status == SourceAttemptStatus.FAILURE:
            self.health.record_failure(source_id, outcome.failure_reason)
        elif outcome.status == SourceAttemptStatus.SUCCESS:
            self.health.record_success(source_id)
        return artwork

    def fetch_met_artwork(self, posted_ids: Set[str], target_medium: str = None) -> Optional[Artwork]:
        return self._fetch_via("met", posted_ids, target_medium)

    def fetch_aic_artwork(self, posted_ids: Set[str], target_medium: str = None) -> Optional[Artwork]:
        return self._fetch_via("aic", posted_ids, target_medium)

    def fetch_cma_artwork(self, posted_ids: Set[str], target_medium: str = None) -> Optional[Artwork]:
        return self._fetch_via("cma", posted_ids, target_medium)

    def fetch_smk_artwork(self, posted_ids: Set[str], target_medium: str = None) -> Optional[Artwork]:
        return self._fetch_via("smk", posted_ids, target_medium)

    def fetch_harvard_artwork(self, posted_ids: Set[str], target_medium: str = None) -> Optional[Artwork]:
        return self._fetch_via("harvard", posted_ids, target_medium)

    # --- Shadow pool telemetrisi delegasyonu (test uyumluluğu) -------------
    def _record_aic_pool(self, artworks, posted_ids: Set[str], iiif_url: str = None):
        self._sources["aic"].record_pool(artworks, posted_ids, iiif_url)

    def _record_cma_pool(self, artworks, posted_ids: Set[str]):
        self._sources["cma"].record_pool(artworks, posted_ids)

    def _record_smk_pool(self, artworks, posted_ids: Set[str]):
        self._sources["smk"].record_pool(artworks, posted_ids)

    def _record_harvard_pool(self, artworks, posted_ids: Set[str]):
        self._sources["harvard"].record_pool(artworks, posted_ids)

    # ----------------------------------------------------------------------
    # Telemetri koordinasyonu
    # ----------------------------------------------------------------------
    def log_scoring_telemetry(self):
        self.scoring_telemetry.log(logger)
        self.pool_telemetry.log_pool(logger, self.pool_coverage)

    def build_scoring_telemetry_export(self, publish_success=None, run_timestamp=None):
        """Return aggregate telemetry only; this performs no scoring or selection."""
        if run_timestamp is None:
            run_timestamp = datetime.now(timezone.utc).isoformat()
        return {
            "schema_version": 1,
            "run_timestamp": run_timestamp,
            "selection_path": self.scoring_telemetry.export_selection_path(),
            "pool": self.pool_telemetry.export_pool(self.pool_coverage),
            "publish": {"success": publish_success},
        }

    def write_scoring_telemetry(self, path, publish_success=None, run_timestamp=None):
        """Telemetriyi verilen dosya yoluna yazar (çağıran sahibi olan dahili yol)."""
        payload = self.build_scoring_telemetry_export(publish_success, run_timestamp)
        target = Path(path)
        serialized = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
        target.write_text(serialized, encoding="utf-8")

    # ----------------------------------------------------------------------
    # Rastgele Müze Seçimi ve Fallback Orkestrasyonu
    # ----------------------------------------------------------------------
    def get_random_artwork(self, posted_ids_by_museum: Dict[str, List[str]], target_medium: str = None) -> Optional[Artwork]:
        """
        Müzeler arasında rastgele seçim yapar. Yalnızca kalite eşiğini (config.MINIMUM_QUALITY_SCORE)
        sağlayan VE yayın hakları doğrulanmış (is_publishable) eserleri kabul eder.
        Eğer target_medium belirtilmişse o kategoriye ağırlık verir.
        """
        museum_fetchers = [("met", self.fetch_met_artwork)]
        # Modül-özelliği üzerinden okunur: testler artfolio.sources.aic.AIC_PUBLISHING_ENABLED
        # path'ini patch'leyerek bayrağı güvenle değiştirebilsin.
        if aic_module.AIC_PUBLISHING_ENABLED:
            museum_fetchers.append(("aic", self.fetch_aic_artwork))
        else:
            if not self._aic_publishing_disabled_logged:
                logger.info("source=aic publishing_disabled reason=iiif_cloudflare_403")
                self._aic_publishing_disabled_logged = True
        museum_fetchers.extend([
            ("cma", self.fetch_cma_artwork),
            ("smk", self.fetch_smk_artwork),
            ("harvard", self.fetch_harvard_artwork),
        ])

        random.shuffle(museum_fetchers)
        run_stats = {
            "source": "none",
            "candidates": 0,
            "duplicates": 0,
            "rejected_image": 0,
            "rejected_quality": 0,
            "rejected_other": 0,
            "rejected_rights": 0,
            "rejected_diversity": 0,
            ARTIST_COOLDOWN: 0,
            SOURCE_CONSECUTIVE_LIMIT: 0,
            MEDIUM_CONSECUTIVE_LIMIT: 0,
            "circuit_skips": 0,
            "eligible": 0,
        }

        for museum_key, fetcher_func in museum_fetchers:
            # Devre kesici: OPEN kaynak cooldown bitene dek atlanır (içerik reddi değil).
            if not self.health.can_attempt(museum_key):
                run_stats["circuit_skips"] += 1
                continue
            posted_set = CandidateExclusions(
                posted_ids_by_museum.get(museum_key, [])
            )
            for candidate_number in range(MAX_DIVERSITY_CANDIDATES_PER_SOURCE):
                self.scoring_telemetry.record_attempt(museum_key)
                artwork = fetcher_func(posted_set, target_medium)
                fetch_stats = self.last_fetch_stats
                for key in (
                    "candidates", "duplicates", "rejected_image", "rejected_quality",
                    "rejected_other", "rejected_rights", "eligible",
                ):
                    run_stats[key] += fetch_stats.get(key, 0)
                if not artwork:
                    break

                # Yayın hakları kapısı: hak durumu doğrulanamayan eser asla yayına aday olamaz.
                # Bu bir kalite reddi değil, hak reddidir ve ayrı sayılır.
                if not artwork.is_publishable:
                    run_stats["rejected_rights"] += 1
                    logger.warning(
                        "rights_rejection source=%s object_id=%s reason=rights_not_publishable",
                        museum_key, artwork.id,
                    )
                    break
                if artwork.score < config.MINIMUM_QUALITY_SCORE:
                    break

                self.scoring_telemetry.record_first_eligible(museum_key, artwork.score)
                # Eser türü uyuşmazlığına karşı ek bir kontrol
                if target_medium and artwork.medium_type != target_medium:
                    run_stats["rejected_other"] += 1
                    logger.warning(f"Bulunan eser ({artwork.medium_type}) hedeflenen tür ({target_medium}) ile uyuşmadı, bir sonraki müzeye geçiliyor.")
                    break

                diversity_reason = self.diversity_policy.rejection_reason(artwork)
                if diversity_reason:
                    run_stats["rejected_diversity"] += 1
                    run_stats[diversity_reason] += 1
                    if diversity_reason == MEDIUM_CONSECUTIVE_LIMIT:
                        logger.info(
                            "diversity_rejection source=%s object_id=%s reason=%s medium=%s",
                            artwork.museum,
                            artwork.id,
                            diversity_reason,
                            artwork.medium_type,
                        )
                    else:
                        logger.info(
                            "diversity_rejection source=%s object_id=%s reason=%s",
                            artwork.museum,
                            artwork.id,
                            diversity_reason,
                        )

                    can_try_alternative = (
                        candidate_number + 1 < MAX_DIVERSITY_CANDIDATES_PER_SOURCE
                        and (
                            diversity_reason == ARTIST_COOLDOWN
                            or (
                                diversity_reason == MEDIUM_CONSECUTIVE_LIMIT
                                and not target_medium
                            )
                        )
                    )
                    if can_try_alternative:
                        posted_set.add_transient(artwork.id)
                        continue
                    break

                run_stats["source"] = museum_key
                self.last_run_stats = run_stats
                self.scoring_telemetry.record_selected(museum_key, artwork.score)
                self.health.log_summary()
                logger.info(
                    "selection_summary source=%s candidates=%d duplicates=%d "
                    "rejected_quality=%d rejected_rights=%d rejected_diversity=%d "
                    "artist_cooldown=%d source_consecutive_limit=%d "
                    "medium_consecutive_limit=%d eligible=%d selected=%s",
                    run_stats["source"], run_stats["candidates"], run_stats["duplicates"],
                    run_stats["rejected_quality"], run_stats["rejected_rights"],
                    run_stats["rejected_diversity"], run_stats[ARTIST_COOLDOWN],
                    run_stats[SOURCE_CONSECUTIVE_LIMIT],
                    run_stats[MEDIUM_CONSECUTIVE_LIMIT], run_stats["eligible"], artwork.id,
                )
                return artwork
        self.last_run_stats = run_stats
        self.health.log_summary()
        logger.info(
            "selection_summary source=none candidates=%d duplicates=%d "
            "rejected_quality=%d rejected_rights=%d rejected_diversity=%d "
            "artist_cooldown=%d source_consecutive_limit=%d "
            "medium_consecutive_limit=%d eligible=%d selected=none",
            run_stats["candidates"], run_stats["duplicates"],
            run_stats["rejected_quality"], run_stats["rejected_rights"],
            run_stats["rejected_diversity"], run_stats[ARTIST_COOLDOWN],
            run_stats[SOURCE_CONSECUTIVE_LIMIT],
            run_stats[MEDIUM_CONSECUTIVE_LIMIT], run_stats["eligible"],
        )
        logger.error(
            "Hiçbir müze API'sinden %d/100 kriterini sağlayan bir eser bulunamadı!",
            config.MINIMUM_QUALITY_SCORE,
        )
        return None
