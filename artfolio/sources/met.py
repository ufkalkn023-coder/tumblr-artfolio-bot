"""The Metropolitan Museum of Art (The Met) kaynak adaptörü."""

import json
import random
from typing import Optional, Set

import config
import requests

from artfolio.domain.artwork import Artwork
from artfolio.scoring.scorer import ArtworkScorer

from .base import HTTP_TIMEOUT, MuseumSource, is_persisted_duplicate, logger
from http_requests import request_with_bounded_retry


class MetSource(MuseumSource):
    source_id = "met"
    museum_name = "The Metropolitan Museum of Art, New York"

    SEARCH_URL = "https://collectionapi.metmuseum.org/public/collection/v1/search"
    OBJECT_URL_TEMPLATE = "https://collectionapi.metmuseum.org/public/collection/v1/objects/{obj_id}"

    DEFAULT_SEARCH_TERMS = ["masterpiece", "portrait", "sculpture", "painting", "drawing", "renaissance", "marble", "bronze"]
    RARE_SEARCH_TERMS = ["self portrait", "last work", "final work"]
    TARGET_SEARCH_TERMS = {
        "Painting": ["painting", "portrait", "oil on canvas", "fresco"],
        "Sculpture": ["sculpture", "statue", "marble", "bronze"],
        "Drawing": ["drawing", "sketch", "watercolor", "ink on paper"],
        "Object": ["artifact", "vase", "jewelry", "armor", "pottery", "sword"],
    }

    def fetch_artwork(self, posted_ids: Set[str], target_medium: str = None) -> Optional[Artwork]:
        """The Met API'den kalite eşiğini (config.MINIMUM_QUALITY_SCORE) sağlayan kamu malı resim, heykel, çizim veya obje seçer."""
        stats = self._start_fetch_stats()
        logger.info(f"The Met API taranıyor (Hedef: {target_medium or 'Karışık'})...")

        search_terms = list(self.DEFAULT_SEARCH_TERMS)
        if random.random() < 0.1:
            search_terms.extend(self.RARE_SEARCH_TERMS)
        if target_medium in self.TARGET_SEARCH_TERMS:
            search_terms = list(self.TARGET_SEARCH_TERMS[target_medium])

        query = random.choice(search_terms)

        params = {
            "isPublicDomain": "true",
            "hasImages": "true",
            "q": query
        }

        try:
            resp = request_with_bounded_retry(
                lambda: self.session.get(self.SEARCH_URL, params=params, timeout=HTTP_TIMEOUT),
                endpoint="met_search",
                logger=logger,
            )
            if resp.status_code != 200:
                self._record_health_failure(f"http_{resp.status_code}")
                logger.error("source=met api_failure type=http_status status=%s", resp.status_code)
                logger.warning(f"The Met arama hatası: HTTP {resp.status_code}")
                return None

            data = resp.json()
            object_ids = data.get("objectIDs")
            if not object_ids:
                self._log_fetch_stats(stats)
                return None

            stats["candidates"] = len(object_ids)
            stats["duplicates"] = sum(
                1 for oid in object_ids if is_persisted_duplicate(posted_ids, oid)
            )
            self.scoring_telemetry.record_duplicate("met", stats["duplicates"])
            self._set_pool_coverage("met", "partial", 0)
            self._safe_pool_operation(self.pool_telemetry.record_duplicate, "met", stats["duplicates"])
            logger.info("source=met candidate_response_count=%d", stats["candidates"])

            random.shuffle(object_ids)
            sample_ids = [str(oid) for oid in object_ids if str(oid) not in posted_ids][:20]

            for obj_id in sample_ids:
                self.scoring_telemetry.record_evaluated("met")
                detail_url = self.OBJECT_URL_TEMPLATE.format(obj_id=obj_id)
                obj_resp = request_with_bounded_retry(
                    lambda: self.session.get(detail_url, timeout=HTTP_TIMEOUT),
                    endpoint="met_object",
                    logger=logger,
                )
                if obj_resp.status_code != 200:
                    # Detay isteği retry politikasını tüketti: operasyonel arıza sinyali.
                    self._record_health_failure(f"http_{obj_resp.status_code}")
                    continue

                obj_data = obj_resp.json()
                self.pool_coverage["met"]["materialized"] += 1
                self._safe_pool_operation(self.pool_telemetry.record_evaluated, "met")

                if not obj_data.get("isPublicDomain", False):
                    stats["rejected_rights"] += 1
                    logger.warning(
                        "rights_rejection source=met object_id=%s reason=rights_not_publishable",
                        obj_id,
                    )
                    continue

                primary_image = (obj_data.get("primaryImage") or "").strip()
                if not primary_image:
                    stats["rejected_image"] += 1
                    continue

                title = (obj_data.get("title") or "Untitled").strip() or "Untitled"
                artist = (obj_data.get("artistDisplayName") or "").strip() or "Unknown Artist"

                artistNationality = (obj_data.get("artistNationality") or "").strip()
                artistBeginDate = (obj_data.get("artistBeginDate") or "").strip()
                artistEndDate = (obj_data.get("artistEndDate") or "").strip()
                artist_bio = artist
                if artistNationality or artistBeginDate or artistEndDate:
                    bio_parts = []
                    if artistNationality: bio_parts.append(artistNationality)
                    years = f"{artistBeginDate}–{artistEndDate}".strip("–")
                    if years: bio_parts.append(years)
                    if bio_parts:
                        artist_bio = f"{artist} ({', '.join(bio_parts)})"

                date_str = (obj_data.get("objectDate") or "").strip() or "Unknown Date"
                department = obj_data.get("department", "")
                raw_medium = obj_data.get("medium", "")
                classification = obj_data.get("classification", "")
                object_name = obj_data.get("objectName", "")
                is_highlight = obj_data.get("isHighlight", False)
                additional_images = bool(obj_data.get("additionalImages", []))

                dimensions = (obj_data.get("dimensions") or "").strip() or "Unknown dimensions"
                gallery_num = (obj_data.get("GalleryNumber") or "").strip()
                repository = obj_data.get("repository", "The Metropolitan Museum of Art")
                location_info = f"Gallery {gallery_num}, {repository}" if gallery_num else repository
                original_source_url = (obj_data.get("objectURL") or "").strip()

                # Puanlama
                score, log_summary = ArtworkScorer.calculate_score(
                    title=title,
                    artist=artist,
                    date_str=date_str,
                    raw_medium=raw_medium,
                    classification=classification,
                    object_name=object_name,
                    image_url=primary_image,
                    is_highlight=is_highlight,
                    has_additional_images=additional_images,
                    on_view=False
                )

                logger.debug(f"The Met ID {obj_id} Değerlendirmesi: {log_summary}")
                self.scoring_telemetry.record_scored(
                    "met", raw_medium, classification, object_name, artist, score,
                    is_highlight, False, additional_images,
                )
                self._safe_pool_operation(
                    self.pool_telemetry.record_scored,
                    "met", raw_medium, classification, object_name, artist, score,
                    is_highlight, False, additional_images,
                )

                if score >= config.MINIMUM_QUALITY_SCORE:
                    stats["eligible"] = 1
                    self._log_fetch_stats(stats)
                    self._record_health_success()
                    medium_type = ArtworkScorer.classify_medium(raw_medium, classification, object_name)
                    logger.info(f"✓ The Met Eseri Onaylandı ({score}/100): '{title}' by {artist} [{medium_type}]")
                    return Artwork(
                        museum="met",
                        id=str(obj_id),
                        title=title,
                        artist=artist,
                        artist_bio=artist_bio,
                        date=date_str,
                        image_url=primary_image,
                        original_source_url=original_source_url,
                        museum_name=self.museum_name,
                        location_info=location_info,
                        dimensions=dimensions,
                        medium_type=medium_type,
                        raw_medium=raw_medium,
                        score=score,
                        style_or_era=department,
                        alt_text=f"{title} by {artist}. {raw_medium}. {department}.",
                        is_public_domain=bool(obj_data.get("isPublicDomain")),
                        rights_statement=(obj_data.get("rightsAndReproduction") or "").strip(),
                    )
                stats["rejected_quality"] += 1

        except (requests.Timeout, requests.ConnectionError) as e:
            # Paylaşılan helper retry'ları tüketti: tek mantıksal kaynak arızası.
            logger.error("source=met api_failure type=%s", type(e).__name__)
            self._record_health_failure(
                "timeout" if isinstance(e, requests.Timeout) else "connection_error"
            )
        except json.JSONDecodeError as e:
            logger.error("source=met api_failure type=%s", type(e).__name__)
            self._record_health_failure("malformed_json")
        except Exception as e:
            logger.error("source=met api_failure type=%s", type(e).__name__)
            self._record_health_failure("internal_error")

        self._log_fetch_stats(stats)
        return None
