"""Harvard Art Museums kaynak adaptörü.

HAK DURUMU (bilinçli karar, fail-closed): Harvard API'si eser bazlı CC0/kamu malı
bildirimi içermez (yalnızca 'imagepermissionlevel' görüntüleme izni verir). Bu
belirsizlik nedeniyle Harvard adayları fetch aşamasında hak reddi olarak elenir
ve hiçbir Harvard eseri yayınlanmaz. Arayüz korunur; metadata gelirse kolayca
açılabilir.

SAĞLIK DURUMU: hak fail-closed bir İÇERİK sonucudur, operasyonel arıza değildir;
sağlık serisini bozmaz. Eksik API anahtarı DISABLED olarak kaydedilir.
"""

import json
import random
from typing import Optional, Set

import config
import requests

from artfolio.domain.artwork import Artwork
from artfolio.scoring.scorer import ArtworkScorer

from .base import HTTP_TIMEOUT, MuseumSource, is_persisted_duplicate, logger
from http_requests import request_with_bounded_retry


class HarvardSource(MuseumSource):
    source_id = "harvard"
    museum_name = "Harvard Art Museums"

    SEARCH_URL = "https://api.harvardartmuseums.org/object"

    TARGET_CLASSIFICATIONS = {
        "Painting": "Paintings",
        "Sculpture": "Sculpture",
        "Drawing": "Drawings",
    }

    def fetch_artwork(self, posted_ids: Set[str], target_medium: str = None) -> Optional[Artwork]:
        """Harvard Art Museums API'sinden eser seçer (API Key gerektirir)."""
        stats = self._start_fetch_stats()
        if not config.HARVARD_API_KEY:
            # Yapılandırma eksikliği: operasyonel arıza DEĞİL, tekrar tekrar sayılmaz.
            self._record_health_disabled()
            logger.warning("Harvard API anahtarı (HARVARD_API_KEY) tanımlanmamış, atlanıyor.")
            return None

        logger.info(f"Harvard Art Museums API taranıyor (Hedef: {target_medium or 'Karışık'})...")

        random_page = random.randint(1, 50)

        params = {
            "apikey": config.HARVARD_API_KEY,
            "hasimage": 1,
            "permissionlevel": 0, # Public Domain
            "sort": "random",
            "page": random_page,
            "size": 20
        }

        if target_medium in self.TARGET_CLASSIFICATIONS:
            params["classification"] = self.TARGET_CLASSIFICATIONS[target_medium]

        try:
            resp = request_with_bounded_retry(
                lambda: self.session.get(self.SEARCH_URL, params=params, timeout=HTTP_TIMEOUT),
                endpoint="harvard_search",
                logger=logger,
            )
            if resp.status_code != 200:
                self._record_health_failure(f"http_{resp.status_code}")
                logger.error("source=harvard api_failure type=http_status status=%s", resp.status_code)
                logger.warning(f"Harvard arama hatası: HTTP {resp.status_code}")
                return None

            data = resp.json()
            artworks = data.get("records", [])
            if not artworks:
                self._log_fetch_stats(stats)
                return None

            stats["candidates"] = len(artworks)
            stats["duplicates"] = sum(
                1 for item in artworks
                if is_persisted_duplicate(posted_ids, item.get("id", ""))
            )
            self.scoring_telemetry.record_duplicate("harvard", stats["duplicates"])
            self._safe_pool_operation(self.record_pool, artworks, posted_ids)
            logger.info("source=harvard candidate_response_count=%d", stats["candidates"])

            for item in artworks:
                artwork_id = str(item.get("id", ""))
                if not artwork_id or artwork_id in posted_ids:
                    continue
                self.scoring_telemetry.record_evaluated("harvard")

                images = item.get("images", [])
                image_url = ""
                if images:
                    image_url = images[0].get("baseimageurl", "")
                if not image_url:
                    stats["rejected_image"] += 1
                    continue

                title = (item.get("title") or "Untitled").strip()
                artist = "Unknown Artist"
                people = item.get("people", [])
                if people:
                    artist = people[0].get("name", "Unknown Artist")

                date_str = str(item.get("dated", "Unknown Date"))
                raw_medium = item.get("medium", "") or ""
                classification = item.get("classification", "") or ""

                score, log_summary = ArtworkScorer.calculate_score(
                    title=title, artist=artist, date_str=date_str,
                    raw_medium=raw_medium, classification=classification,
                    object_name="", image_url=image_url,
                    is_highlight=False, has_additional_images=bool(len(images) > 1), on_view=False
                )

                logger.debug(f"Harvard ID {artwork_id} Değerlendirmesi: {log_summary}")
                self.scoring_telemetry.record_scored(
                    "harvard", raw_medium, classification, "", artist, score,
                    False, False, bool(len(images) > 1),
                )

                if score >= config.MINIMUM_QUALITY_SCORE:
                    # Harvard API'sinde eser bazlı CC0/kamu malı bildirimi yoktur
                    # (yalnızca 'imagepermissionlevel' görüntüleme izni verir).
                    # Bu belirsizlik nedeniyle Harvard adayları fail-closed elenir.
                    stats["rejected_rights"] += 1
                    logger.warning(
                        "rights_rejection source=harvard object_id=%s reason=rights_not_publishable",
                        artwork_id,
                    )
                    continue
                stats["rejected_quality"] += 1
        except (requests.Timeout, requests.ConnectionError) as e:
            # Paylaşılan helper retry'ları tüketti: tek mantıksal kaynak arızası.
            logger.error("source=harvard api_failure type=%s", type(e).__name__)
            self._record_health_failure(
                "timeout" if isinstance(e, requests.Timeout) else "connection_error"
            )
        except json.JSONDecodeError as e:
            logger.error("source=harvard api_failure type=%s", type(e).__name__)
            self._record_health_failure("malformed_json")
        except Exception as e:
            logger.error("source=harvard api_failure type=%s", type(e).__name__)
            self._record_health_failure("internal_error")
        self._log_fetch_stats(stats)
        return None

    def record_pool(self, artworks, posted_ids: Set[str]):
        """Shadow-telemetry for the full materialized Harvard candidate pool."""
        source = "harvard"
        self._set_pool_coverage(source, "full", len(artworks))
        self.pool_telemetry.record_duplicate(
            source, sum(
                1 for item in artworks
                if is_persisted_duplicate(posted_ids, item.get("id", ""))
            )
        )
        for item in artworks:
            artwork_id = str(item.get("id", ""))
            if not artwork_id or artwork_id in posted_ids:
                continue
            images = item.get("images", []) or []
            image_url = images[0].get("baseimageurl", "") if images else ""
            people = item.get("people", []) or []
            artist = people[0].get("name", "Unknown Artist") if people else "Unknown Artist"
            self._record_pool_candidate(
                source,
                (item.get("title") or "Untitled").strip() or "Untitled",
                artist,
                str(item.get("dated", "Unknown Date")),
                item.get("medium", "") or "",
                item.get("classification", "") or "",
                "",
                image_url,
                False,
                bool(len(images) > 1),
                False,
            )
