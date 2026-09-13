"""Art Institute of Chicago (AIC) kaynak adaptörü.

Üretim durumu: AIC IIIF barındırması otomatik istemcilere Cloudflare challenge
(403) döndürdüğü için yayın bilinçli olarak devre dışıdır (AIC_PUBLISHING_ENABLED).
"""

import json
import random
from typing import Optional, Set

import config
import requests

from artfolio.domain.artwork import Artwork
from artfolio.scoring.scorer import ArtworkScorer

from .base import HTTP_TIMEOUT, MuseumSource, is_persisted_duplicate, logger
from http_requests import (
    AIC_METADATA_RETRYABLE_STATUS_CODES,
    JSON_REQUEST_HEADERS,
    request_with_bounded_retry,
)

AIC_DEFAULT_IIIF_URL = "https://www.artic.edu/iiif/2"
AIC_STANDARD_IMAGE_SIZE = 843
AIC_LARGE_PUBLIC_DOMAIN_IMAGE_SIZE = 1686
# AIC's IIIF host currently serves Cloudflare challenges to automated clients.
# Keep the integration intact so it can be restored by flipping this flag.
AIC_PUBLISHING_ENABLED = False

SEARCH_URL = "https://api.artic.edu/api/v1/artworks/search"

# Fields whitelist — is_public_domain bilinçli olarak istenir (hak eşlemesi için).
SEARCH_FIELDS = [
    "id",
    "title",
    "artist_display",
    "date_display",
    "image_id",
    "artwork_type_title",
    "medium_display",
    "classification_title",
    "is_public_domain",
    "is_boosted",
    "is_on_view",
    "style_title"
]

DEFAULT_TYPE_MATCHES = [
    {"match": {"artwork_type_title": "Painting"}},
    {"match": {"artwork_type_title": "Sculpture"}},
    {"match": {"artwork_type_title": "Drawing and Watercolor"}}
]

TARGET_TYPE_MATCHES = {
    "Painting": [{"match": {"artwork_type_title": "Painting"}}],
    "Sculpture": [{"match": {"artwork_type_title": "Sculpture"}}],
    "Drawing": [{"match": {"artwork_type_title": "Drawing and Watercolor"}}],
    "Object": [{"match": {"artwork_type_title": "Decorative Arts"}}, {"match": {"artwork_type_title": "Vessels"}}],
}


def build_aic_iiif_image_url(image_id: str, iiif_url: str = None, is_public_domain: bool = False) -> str:
    """Build an AIC IIIF URL using the API-provided endpoint when available."""
    if not image_id:
        return ""
    base_url = (iiif_url or AIC_DEFAULT_IIIF_URL).rstrip("/")
    size = AIC_LARGE_PUBLIC_DOMAIN_IMAGE_SIZE if is_public_domain is True else AIC_STANDARD_IMAGE_SIZE
    return f"{base_url}/{image_id}/full/{size},/0/default.jpg"


class AICSource(MuseumSource):
    source_id = "aic"
    museum_name = "Art Institute of Chicago"

    def fetch_artwork(self, posted_ids: Set[str], target_medium: str = None) -> Optional[Artwork]:
        """Art Institute of Chicago API'den kalite eşiğini (config.MINIMUM_QUALITY_SCORE) sağlayan eser seçer."""
        stats = self._start_fetch_stats()
        logger.info(f"Art Institute of Chicago (AIC) API taranıyor (Hedef: {target_medium or 'Karışık'})...")

        should_matches = list(DEFAULT_TYPE_MATCHES)
        if target_medium in TARGET_TYPE_MATCHES:
            should_matches = list(TARGET_TYPE_MATCHES[target_medium])

        random_page = random.randint(1, 30)
        payload = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"is_public_domain": True}}
                    ],
                    "should": should_matches,
                    "minimum_should_match": 1
                }
            },
            "fields": list(SEARCH_FIELDS),
            "limit": 40,
            "page": random_page
        }

        try:
            resp = request_with_bounded_retry(
                lambda: self.session.post(
                    SEARCH_URL,
                    json=payload,
                    headers=JSON_REQUEST_HEADERS,
                    timeout=HTTP_TIMEOUT,
                ),
                endpoint="aic_metadata",
                logger=logger,
                # AIC 403'ü yalnızca burada geçici sayar (Cloudflare challenge);
                # 403 varsayılan kümede kasıtlı olarak yoktur.
                retryable_status_codes=AIC_METADATA_RETRYABLE_STATUS_CODES,
            )
            if resp.status_code != 200:
                self._record_health_failure(f"http_{resp.status_code}")
                logger.error("source=aic api_failure type=http_status status=%s", resp.status_code)
                logger.warning(f"AIC arama hatası: HTTP {resp.status_code}")
                return None

            data = resp.json()
            artworks = data.get("data", [])
            iiif_url = (data.get("config") or {}).get("iiif_url")
            if not artworks:
                self._log_fetch_stats(stats)
                return None

            stats["candidates"] = len(artworks)
            stats["duplicates"] = sum(
                1 for item in artworks
                if is_persisted_duplicate(posted_ids, item.get("id"))
            )
            self.scoring_telemetry.record_duplicate("aic", stats["duplicates"])
            self._safe_pool_operation(self.record_pool, artworks, posted_ids, iiif_url)
            logger.info("source=aic candidate_response_count=%d", stats["candidates"])

            random.shuffle(artworks)

            for item in artworks:
                artwork_id = str(item.get("id"))
                if artwork_id in posted_ids:
                    continue
                self.scoring_telemetry.record_evaluated("aic")

                image_id = item.get("image_id")
                if not image_id:
                    stats["rejected_image"] += 1
                    continue

                image_url = build_aic_iiif_image_url(
                    image_id,
                    iiif_url,
                    item.get("is_public_domain", False),
                )
                title = (item.get("title") or "Untitled").strip() or "Untitled"
                artist_raw = (item.get("artist_display") or "").strip() or "Unknown Artist"
                artist = artist_raw.split("\n")[0].strip()
                artist_bio = artist_raw.replace("\n", " ")

                date_str = (item.get("date_display") or "").strip() or "Unknown Date"
                raw_medium = item.get("medium_display", "") or ""
                classification = item.get("classification_title", "") or ""
                object_name = item.get("artwork_type_title", "") or ""
                is_boosted = item.get("is_boosted", False)
                is_on_view = item.get("is_on_view", False)
                style_title = item.get("style_title")

                dimensions = (item.get("dimensions") or "").strip() or "Unknown dimensions"
                gallery_title = (item.get("gallery_title") or "").strip()
                location_info = gallery_title if gallery_title else "Art Institute of Chicago"
                original_source_url = f"https://www.artic.edu/artworks/{artwork_id}"

                # Puanlama
                score, log_summary = ArtworkScorer.calculate_score(
                    title=title,
                    artist=artist,
                    date_str=date_str,
                    raw_medium=raw_medium,
                    classification=classification,
                    object_name=object_name,
                    image_url=image_url,
                    is_highlight=is_boosted,
                    has_additional_images=True,
                    on_view=is_on_view
                )

                logger.debug(f"AIC ID {artwork_id} Değerlendirmesi: {log_summary}")
                self.scoring_telemetry.record_scored(
                    "aic", raw_medium, classification, object_name, artist, score,
                    is_boosted, is_on_view, True,
                )

                if score >= config.MINIMUM_QUALITY_SCORE:
                    stats["eligible"] = 1
                    self._log_fetch_stats(stats)
                    self._record_health_success()
                    medium_type = ArtworkScorer.classify_medium(raw_medium, classification, object_name)
                    logger.info(f"✓ AIC Eseri Onaylandı ({score}/100): '{title}' by {artist} [{medium_type}]")
                    return Artwork(
                        museum="aic",
                        id=artwork_id,
                        title=title,
                        artist=artist,
                        artist_bio=artist_bio,
                        date=date_str,
                        image_url=image_url,
                        original_source_url=original_source_url,
                        museum_name=self.museum_name,
                        location_info=location_info,
                        dimensions=dimensions,
                        medium_type=medium_type,
                        raw_medium=raw_medium,
                        score=score,
                        style_or_era=style_title,
                        alt_text=f"{title} by {artist}. {raw_medium}.",
                        is_public_domain=bool(item.get("is_public_domain")),
                    )
                stats["rejected_quality"] += 1

        except (requests.Timeout, requests.ConnectionError) as e:
            # Paylaşılan helper retry'ları tüketti: tek mantıksal kaynak arızası.
            logger.error("source=aic api_failure type=%s", type(e).__name__)
            self._record_health_failure(
                "timeout" if isinstance(e, requests.Timeout) else "connection_error"
            )
        except json.JSONDecodeError as e:
            logger.error("source=aic api_failure type=%s", type(e).__name__)
            self._record_health_failure("malformed_json")
        except Exception as e:
            logger.error("source=aic api_failure type=%s", type(e).__name__)
            self._record_health_failure("internal_error")

        self._log_fetch_stats(stats)
        return None

    def record_pool(self, artworks, posted_ids: Set[str], iiif_url: str = None):
        """Shadow-telemetry for the full materialized AIC candidate pool."""
        source = "aic"
        self._set_pool_coverage(source, "full", len(artworks))
        self.pool_telemetry.record_duplicate(
            source, sum(
                1 for item in artworks
                if is_persisted_duplicate(posted_ids, item.get("id"))
            )
        )
        for item in artworks:
            artwork_id = str(item.get("id"))
            if artwork_id in posted_ids:
                continue
            image_id = item.get("image_id")
            image_url = build_aic_iiif_image_url(
                image_id,
                iiif_url,
                item.get("is_public_domain", False),
            )
            artist_raw = (item.get("artist_display") or "").strip() or "Unknown Artist"
            self._record_pool_candidate(
                source,
                (item.get("title") or "Untitled").strip() or "Untitled",
                artist_raw.split("\n")[0].strip(),
                (item.get("date_display") or "").strip() or "Unknown Date",
                item.get("medium_display", "") or "",
                item.get("classification_title", "") or "",
                item.get("artwork_type_title", "") or "",
                image_url,
                item.get("is_boosted", False),
                True,
                item.get("is_on_view", False),
            )
