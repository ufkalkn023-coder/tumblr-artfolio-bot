"""Cleveland Museum of Art (CMA) kaynak adaptörü."""

import json
import random
from typing import Optional, Set

import config
import requests

from artfolio.domain.artwork import Artwork
from artfolio.scoring.scorer import ArtworkScorer

from .base import HTTP_TIMEOUT, MuseumSource, is_persisted_duplicate, logger
from http_requests import request_with_bounded_retry


class CMASource(MuseumSource):
    source_id = "cma"
    museum_name = "The Cleveland Museum of Art"

    SEARCH_URL = "https://openaccess-api.clevelandart.org/api/artworks/"

    TARGET_TYPE_PARAMS = {
        "Painting": "Painting",
        "Sculpture": "Sculpture",
        "Drawing": "Drawing",
        "Object": "Decorative Art",
    }

    def fetch_artwork(self, posted_ids: Set[str], target_medium: str = None) -> Optional[Artwork]:
        """Cleveland Museum of Art Açık Erişim API'sinden kalite eşiğini (config.MINIMUM_QUALITY_SCORE) sağlayan eser seçer."""
        stats = self._start_fetch_stats()
        logger.info(f"Cleveland Museum of Art (CMA) API taranıyor (Hedef: {target_medium or 'Karışık'})...")

        random_skip = random.randint(0, 30) * 40
        params = {
            "has_image": "1",
            "is_public_domain": "1",
            "limit": 40,
            "skip": random_skip
        }

        if target_medium in self.TARGET_TYPE_PARAMS:
            params["type"] = self.TARGET_TYPE_PARAMS[target_medium]

        try:
            resp = request_with_bounded_retry(
                lambda: self.session.get(self.SEARCH_URL, params=params, timeout=HTTP_TIMEOUT),
                endpoint="cma_search",
                logger=logger,
            )
            if resp.status_code != 200:
                self._record_health_failure(f"http_{resp.status_code}")
                logger.error("source=cma api_failure type=http_status status=%s", resp.status_code)
                logger.warning(f"CMA arama hatası: HTTP {resp.status_code}")
                return None

            data = resp.json()
            artworks = data.get("data", [])
            if not artworks:
                self._log_fetch_stats(stats)
                return None

            stats["candidates"] = len(artworks)
            stats["duplicates"] = sum(
                1 for item in artworks
                if is_persisted_duplicate(posted_ids, item.get("id"))
            )
            self.scoring_telemetry.record_duplicate("cma", stats["duplicates"])
            self._safe_pool_operation(self.record_pool, artworks, posted_ids)
            logger.info("source=cma candidate_response_count=%d", stats["candidates"])

            random.shuffle(artworks)

            for item in artworks:
                artwork_id = str(item.get("id"))
                if artwork_id in posted_ids:
                    continue
                self.scoring_telemetry.record_evaluated("cma")

                images = item.get("images", {})
                image_url = None
                if images.get("web", {}).get("url"):
                    image_url = images["web"]["url"]
                elif images.get("print", {}).get("url"):
                    image_url = images["print"]["url"]

                if not image_url:
                    stats["rejected_image"] += 1
                    continue

                title = (item.get("title") or "Untitled").strip() or "Untitled"
                creators = item.get("creators", [])
                artist = "Unknown Artist"
                artist_bio = "Unknown Artist"
                if creators and isinstance(creators, list):
                    desc = creators[0].get("description") or "Unknown Artist"
                    artist = desc.split("(")[0].strip()
                    artist_bio = desc

                date_str = (item.get("creation_date") or "").strip() or "Unknown Date"
                raw_medium = item.get("technique", "") or item.get("type", "")
                classification = item.get("type", "")
                object_name = item.get("department", "")
                culture = item.get("culture", [""])[0] if isinstance(item.get("culture"), list) and item.get("culture") else None
                on_view = bool(item.get("current_location"))
                is_highlight = bool(item.get("share_license_status") == "CC0" and on_view)

                dimensions = (item.get("measurements") or "").strip() or "Unknown dimensions"
                current_location = (item.get("current_location") or "").strip()
                location_info = current_location if current_location else "The Cleveland Museum of Art"
                original_source_url = (item.get("url") or "").strip()

                # Puanlama
                score, log_summary = ArtworkScorer.calculate_score(
                    title=title,
                    artist=artist,
                    date_str=date_str,
                    raw_medium=raw_medium,
                    classification=classification,
                    object_name=object_name,
                    image_url=image_url,
                    is_highlight=is_highlight,
                    has_additional_images=bool(len(images) > 1),
                    on_view=on_view
                )

                logger.debug(f"CMA ID {artwork_id} Değerlendirmesi: {log_summary}")
                self.scoring_telemetry.record_scored(
                    "cma", raw_medium, classification, object_name, artist, score,
                    is_highlight, on_view, bool(len(images) > 1),
                )

                if score >= config.MINIMUM_QUALITY_SCORE:
                    is_public_domain = item.get("share_license_status") == "CC0"
                    if not is_public_domain:
                        # Sağlayıcı hak metadata'sı CC0 doğrulamıyor: adayı atla, aramaya devam et.
                        stats["rejected_rights"] += 1
                        logger.warning(
                            "rights_rejection source=cma object_id=%s reason=rights_not_publishable",
                            artwork_id,
                        )
                        continue
                    stats["eligible"] = 1
                    self._log_fetch_stats(stats)
                    self._record_health_success()
                    medium_type = ArtworkScorer.classify_medium(raw_medium, classification, object_name)
                    logger.info(f"✓ CMA Eseri Onaylandı ({score}/100): '{title}' by {artist} [{medium_type}]")
                    return Artwork(
                        museum="cma",
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
                        style_or_era=culture,
                        alt_text=f"{title} by {artist}. {raw_medium}. {item.get('description', '')[:100]}",
                        license_name=(item.get("share_license_status") or "").strip(),
                        is_public_domain=is_public_domain,
                    )
                stats["rejected_quality"] += 1

        except (requests.Timeout, requests.ConnectionError) as e:
            # Paylaşılan helper retry'ları tüketti: tek mantıksal kaynak arızası.
            logger.error("source=cma api_failure type=%s", type(e).__name__)
            self._record_health_failure(
                "timeout" if isinstance(e, requests.Timeout) else "connection_error"
            )
        except json.JSONDecodeError as e:
            logger.error("source=cma api_failure type=%s", type(e).__name__)
            self._record_health_failure("malformed_json")
        except Exception as e:
            logger.error("source=cma api_failure type=%s", type(e).__name__)
            self._record_health_failure("internal_error")

        self._log_fetch_stats(stats)
        return None

    def record_pool(self, artworks, posted_ids: Set[str]):
        """Shadow-telemetry for the full materialized CMA candidate pool."""
        source = "cma"
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
            images = item.get("images", {}) or {}
            image_url = ""
            if images.get("web", {}).get("url"):
                image_url = images["web"]["url"]
            elif images.get("print", {}).get("url"):
                image_url = images["print"]["url"]
            creators = item.get("creators", [])
            artist = "Unknown Artist"
            if creators and isinstance(creators, list):
                artist = (creators[0].get("description") or "Unknown Artist").split("(")[0].strip()
            self._record_pool_candidate(
                source,
                (item.get("title") or "Untitled").strip() or "Untitled",
                artist,
                (item.get("creation_date") or "").strip() or "Unknown Date",
                item.get("technique", "") or item.get("type", "") or "",
                item.get("type", "") or "",
                item.get("department", "") or "",
                image_url,
                bool(item.get("share_license_status") == "CC0" and item.get("current_location")),
                bool(len(images) > 1),
                bool(item.get("current_location")),
            )
