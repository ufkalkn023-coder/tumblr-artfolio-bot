"""Statens Museum for Kunst (SMK) - Kopenhag kaynak adaptörü."""

import json
import random
from typing import Optional, Set

import config
import requests

from artfolio.domain.artwork import Artwork
from artfolio.scoring.scorer import ArtworkScorer

from .base import HTTP_TIMEOUT, MuseumSource, is_persisted_duplicate, logger
from http_requests import request_with_bounded_retry


class SMKSource(MuseumSource):
    source_id = "smk"
    museum_name = "Statens Museum for Kunst (SMK), Copenhagen"

    SEARCH_URL = "https://api.smk.dk/api/v1/art/search"

    TARGET_QUERY_TERMS = {
        "Painting": "maleri",
        "Sculpture": "skulptur",
        "Drawing": "tegning",
    }

    def fetch_artwork(self, posted_ids: Set[str], target_medium: str = None) -> Optional[Artwork]:
        """SMK (Danimarka) API'sinden eser seçer (API Key gerektirmez)."""
        stats = self._start_fetch_stats()
        logger.info(f"SMK (Statens Museum for Kunst) API taranıyor (Hedef: {target_medium or 'Karışık'})...")

        random_offset = random.randint(0, 1000)

        q_param = self.TARGET_QUERY_TERMS.get(target_medium, "*")

        params = {
            "keys": q_param,
            "filters": "has_image:true,public_domain:true",
            "offset": random_offset,
            "rows": 100
        }

        try:
            resp = request_with_bounded_retry(
                lambda: self.session.get(self.SEARCH_URL, params=params, timeout=HTTP_TIMEOUT),
                endpoint="smk_search",
                logger=logger,
            )
            if resp.status_code != 200:
                self._record_health_failure(f"http_{resp.status_code}")
                logger.error("source=smk api_failure type=http_status status=%s", resp.status_code)
                logger.warning(f"SMK arama hatası: HTTP {resp.status_code}")
                return None

            data = resp.json()
            artworks = data.get("items", [])
            if not artworks:
                self._log_fetch_stats(stats)
                return None

            stats["candidates"] = len(artworks)
            stats["duplicates"] = sum(
                1 for item in artworks
                if is_persisted_duplicate(posted_ids, item.get("object_number", ""))
            )
            self.scoring_telemetry.record_duplicate("smk", stats["duplicates"])
            self._safe_pool_operation(self.record_pool, artworks, posted_ids)
            logger.info("source=smk candidate_response_count=%d", stats["candidates"])

            random.shuffle(artworks)

            for item in artworks:
                artwork_id = str(item.get("object_number", ""))
                if not artwork_id or artwork_id in posted_ids:
                    continue
                self.scoring_telemetry.record_evaluated("smk")

                image_url = item.get("image_native")
                if not image_url:
                    image_url = item.get("image_thumbnail", "").replace("!1024", "full")
                if not image_url:
                    stats["rejected_image"] += 1
                    continue

                title = "Untitled"
                titles = item.get("titles", [])
                if titles:
                    title = titles[0].get("title", "Untitled")

                artist = "Unknown Artist"
                artist_bio = "Unknown Artist"
                production = item.get("production", [])
                if production:
                    artist = production[0].get("creator", "Unknown Artist")
                    artist_bio = artist

                date_str = "Unknown Date"
                prod_dates = item.get("production_date", [])
                if prod_dates:
                    date_str = prod_dates[0].get("period", "Unknown Date")

                raw_medium = ""
                techniques = item.get("techniques", [])
                if techniques:
                    raw_medium = techniques[0]

                classification = ""
                object_names = item.get("object_names", [])
                if object_names:
                    classification = object_names[0].get("name", "")

                dimensions = ""
                dim_list = item.get("dimensions", [])
                if dim_list and isinstance(dim_list, list):
                    dimensions = dim_list[0].get("value", "") + " " + dim_list[0].get("unit", "")

                location_info = "Statens Museum for Kunst (SMK), Copenhagen"
                original_source_url = f"https://open.smk.dk/en/artwork/image/{artwork_id}"

                score, log_summary = ArtworkScorer.calculate_score(
                    title=title, artist=artist, date_str=date_str,
                    raw_medium=raw_medium, classification=classification,
                    object_name="", image_url=image_url,
                    is_highlight=False, has_additional_images=False, on_view=item.get("on_display", False)
                )

                logger.debug(f"SMK ID {artwork_id} Değerlendirmesi: {log_summary}")
                self.scoring_telemetry.record_scored(
                    "smk", raw_medium, classification, "", artist, score,
                    False, item.get("on_display", False), False,
                )

                if score >= config.MINIMUM_QUALITY_SCORE:
                    is_public_domain = item.get("public_domain") is True
                    if not is_public_domain:
                        # Eser bazlı public_domain alanı doğrulanamadı: fail-closed, aramaya devam et.
                        stats["rejected_rights"] += 1
                        logger.warning(
                            "rights_rejection source=smk object_id=%s reason=rights_not_publishable",
                            artwork_id,
                        )
                        continue
                    stats["eligible"] = 1
                    self._log_fetch_stats(stats)
                    self._record_health_success()
                    medium_type = ArtworkScorer.classify_medium(raw_medium, classification, "")
                    logger.info(f"✓ SMK Eseri Onaylandı ({score}/100): '{title}' by {artist} [{medium_type}]")
                    return Artwork(
                        museum="smk", id=artwork_id, title=title, artist=artist, artist_bio=artist_bio, date=date_str,
                        image_url=image_url, original_source_url=original_source_url,
                        museum_name=self.museum_name, location_info=location_info, dimensions=dimensions,
                        medium_type=medium_type, raw_medium=raw_medium, score=score, style_or_era="Danish Art",
                        alt_text=f"{title} by {artist}. {raw_medium}.",
                        license_name=(item.get("license_text") or "").strip(),
                        is_public_domain=is_public_domain,
                    )
                stats["rejected_quality"] += 1
        except (requests.Timeout, requests.ConnectionError) as e:
            # Paylaşılan helper retry'ları tüketti: tek mantıksal kaynak arızası.
            logger.error("source=smk api_failure type=%s", type(e).__name__)
            self._record_health_failure(
                "timeout" if isinstance(e, requests.Timeout) else "connection_error"
            )
        except json.JSONDecodeError as e:
            logger.error("source=smk api_failure type=%s", type(e).__name__)
            self._record_health_failure("malformed_json")
        except Exception as e:
            logger.error("source=smk api_failure type=%s", type(e).__name__)
            self._record_health_failure("internal_error")
        self._log_fetch_stats(stats)
        return None

    def record_pool(self, artworks, posted_ids: Set[str]):
        """Shadow-telemetry for the full materialized SMK candidate pool."""
        source = "smk"
        self._set_pool_coverage(source, "full", len(artworks))
        self.pool_telemetry.record_duplicate(
            source, sum(
                1 for item in artworks
                if is_persisted_duplicate(posted_ids, item.get("object_number", ""))
            )
        )
        for item in artworks:
            artwork_id = str(item.get("object_number", ""))
            if not artwork_id or artwork_id in posted_ids:
                continue
            image_url = item.get("image_native")
            if not image_url:
                image_url = item.get("image_thumbnail", "").replace("!1024", "full")
            titles = item.get("titles", [])
            title = titles[0].get("title", "Untitled") if titles else "Untitled"
            production = item.get("production", [])
            artist = production[0].get("creator", "Unknown Artist") if production else "Unknown Artist"
            prod_dates = item.get("production_date", [])
            date_str = prod_dates[0].get("period", "Unknown Date") if prod_dates else "Unknown Date"
            techniques = item.get("techniques", [])
            raw_medium = techniques[0] if techniques else ""
            object_names = item.get("object_names", [])
            classification = object_names[0].get("name", "") if object_names else ""
            self._record_pool_candidate(
                source, title, artist, date_str, raw_medium, classification, "", image_url,
                False, False, item.get("on_display", False),
            )
