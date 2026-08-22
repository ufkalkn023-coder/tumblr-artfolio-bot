"""
museum_api.py - Açık Erişim Müze API Entegrasyonları (The Met, AIC, CMA)
Resim, Heykel, Çizim ve Değerli Objeler için 85/100 Kalite Puanlama Sistemi içerir.
"""

import re
import random
import logging
import json
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional, Dict, List, Set, Tuple
import requests

import config

logger = logging.getLogger("artfolio_bot.museum_api")

HTTP_TIMEOUT = 20
USER_AGENT = "artfolio-bot/1.0 (Tumblr Art Curation Bot)"
MINIMUM_QUALITY_SCORE = 60

# Dünya çapında tanınan usta sanatçılar listesi (Puanlama bonusu için)
FAMOUS_ARTISTS = {
    "da vinci", "leonardo", "michelangelo", "raphael", "rembrandt", "caravaggio",
    "bernini", "rodin", "donatello", "canova", "vermeer", "monet", "van gogh",
    "renoir", "degas", "durer", "velazquez", "goya", "turner", "rubens",
    "titian", "botticelli", "cezanne", "klimt", "munch", "el greco", "hokusai",
    "courbet", "delacroix", "ingres", "david", "bruegel", "bosch", "giotto",
    "manet", "gauguin", "seurat", "whistler", "sargent", "morisot", "cassatt",
    "tintoretto", "veronese", "holbein", "van eyck", "friedrich", "constable"
}

# Değersiz / düşük kaliteli arkeolojik parçaları engellemek için anahtar kelimeler
FRAGMENT_KEYWORDS = ["fragment", "shard", "sherd", "nail", "splinter", "scrap", "bead", "coin", "sample", "strip", "specimen"]
SCORING_BANDS = ((0, 19), (20, 39), (40, 59), (60, 79), (80, 100))


def classify_secondary_telemetry_label(raw_medium: str, classification: str, object_name: str) -> str:
    """Add conservative, telemetry-only labels without affecting selection."""
    text = f"{raw_medium} {classification} {object_name}".casefold()
    matches = set()
    if "manuscript" in text:
        matches.add("manuscript")
    if any(term in text for term in ("print", "engraving", "etching", "woodcut", "lithograph")):
        matches.add("print")
    if any(term in text for term in ("ceramic", "pottery", "porcelain", "earthenware")):
        matches.add("ceramic")
    if any(term in text for term in ("decorative art", "decorative arts", "artifact", "vessel", "object")):
        matches.add("decorative/object")
    return matches.pop() if len(matches) == 1 else "other"


class ScoringTelemetry:
    """In-memory scoring aggregates; never participates in selection."""

    def __init__(self):
        self.sources = {}
        self.categories = {category: self._new_bucket() for category in ("Painting", "Sculpture", "Drawing", "Object")}
        self.secondary_categories = {}
        self.artists = {"known": self._new_bucket(), "unknown": self._new_bucket()}
        self.flags = {flag: self._new_bucket() for flag in ("highlight", "on_view", "additional_images")}

    @staticmethod
    def _new_bucket():
        return {
            "evaluated": 0,
            "scored": 0,
            "eligible": 0,
            "score_sum": 0,
            "min": None,
            "max": None,
            "bands": {f"{low}-{high}": 0 for low, high in SCORING_BANDS},
            "duplicates": 0,
            "attempts": 0,
            "first_eligible": 0,
            "first_eligible_scores": [],
            "selected_scores": [],
        }

    def _source_bucket(self, source):
        if source not in self.sources:
            self.sources[source] = self._new_bucket()
        return self.sources[source]

    def _record_score(self, bucket, score):
        bucket["scored"] += 1
        bucket["score_sum"] += score
        bucket["min"] = score if bucket["min"] is None else min(bucket["min"], score)
        bucket["max"] = score if bucket["max"] is None else max(bucket["max"], score)
        for low, high in SCORING_BANDS:
            if low <= score <= high:
                bucket["bands"][f"{low}-{high}"] += 1
                break
        if score >= MINIMUM_QUALITY_SCORE:
            bucket["eligible"] += 1

    def record_duplicate(self, source, count=1):
        self._source_bucket(source)["duplicates"] += count

    def record_evaluated(self, source):
        self._source_bucket(source)["evaluated"] += 1

    def record_attempt(self, source):
        self._source_bucket(source)["attempts"] += 1

    def record_first_eligible(self, source, score):
        bucket = self._source_bucket(source)
        bucket["first_eligible"] += 1
        bucket["first_eligible_scores"].append(score)

    def record_selected(self, source, score):
        self._source_bucket(source)["selected_scores"].append(score)

    def record_scored(self, source, raw_medium, classification, object_name, artist, score, is_highlight, on_view, has_additional_images):
        source_bucket = self._source_bucket(source)
        self._record_score(source_bucket, score)

        category = ArtworkScorer.classify_medium(raw_medium, classification, object_name)
        category_bucket = self.categories[category]
        category_bucket["evaluated"] += 1
        self._record_score(category_bucket, score)

        secondary = classify_secondary_telemetry_label(raw_medium, classification, object_name)
        secondary_bucket = self.secondary_categories.setdefault(secondary, self._new_bucket())
        secondary_bucket["evaluated"] += 1
        self._record_score(secondary_bucket, score)

        artist_key = "unknown" if any(term in (artist or "").casefold() for term in ("unknown", "anonymous", "unidentified", "various", "maker unknown")) else "known"
        artist_bucket = self.artists[artist_key]
        artist_bucket["evaluated"] += 1
        self._record_score(artist_bucket, score)

        for flag, enabled in (("highlight", is_highlight), ("on_view", on_view), ("additional_images", has_additional_images)):
            if enabled:
                flag_bucket = self.flags[flag]
                flag_bucket["evaluated"] += 1
                self._record_score(flag_bucket, score)

    @staticmethod
    def _average(bucket):
        return "n/a" if not bucket["scored"] else f"{bucket['score_sum'] / bucket['scored']:.1f}"

    @staticmethod
    def _export_bucket(bucket):
        return {
            "evaluated": bucket["evaluated"],
            "scored": bucket["scored"],
            "duplicates": bucket["duplicates"],
            "eligible": bucket["eligible"],
            "rejected_score": bucket["scored"] - bucket["eligible"],
            "avg_score": None if not bucket["scored"] else round(bucket["score_sum"] / bucket["scored"], 1),
            "min_score": bucket["min"],
            "max_score": bucket["max"],
            "score_bands": dict(bucket["bands"]),
        }

    def export_selection_path(self):
        sources = {}
        selected_source = None
        selected_score = None
        for source in sorted(self.sources):
            bucket = self.sources[source]
            source_selected_score = bucket["selected_scores"][-1] if bucket["selected_scores"] else None
            sources[source] = {
                "attempts": bucket["attempts"],
                "examined": bucket["evaluated"],
                "rejected_before_first_eligible": bucket["evaluated"] - bucket["first_eligible"],
                "first_eligible": bucket["first_eligible"],
                "selected_score": source_selected_score,
            }
            if source_selected_score is not None:
                selected_source = source
                selected_score = source_selected_score
        return {
            "sources": sources,
            "selected_source": selected_source,
            "selected_score": selected_score,
        }

    def log(self, logger):
        if not self.sources:
            logger.info("selection_path_stats empty=true")
            return

        for source in sorted(self.sources):
            bucket = self.sources[source]
            selected_score = bucket["selected_scores"][-1] if bucket["selected_scores"] else "none"
            logger.info(
                "selection_path_stats source=%s attempts=%d examined=%d rejected=%d first_eligible=%d selected_score=%s",
                source, bucket["attempts"], bucket["evaluated"], bucket["evaluated"] - bucket["first_eligible"],
                bucket["first_eligible"], selected_score,
            )

    def log_pool(self, logger, coverage):
        if not self.sources:
            logger.info("pool_stats empty=true")
            return

        for source in sorted(self.sources):
            bucket = self.sources[source]
            bands = ",".join(f"{low}-{high}:{bucket['bands'][f'{low}-{high}']}" for low, high in SCORING_BANDS)
            source_coverage = coverage.get(source, {"coverage": "partial", "materialized": 0})
            logger.info(
                "pool_stats source=%s coverage=%s materialized=%d evaluated=%d scored=%d duplicates=%d eligible=%d rejected_score=%d avg=%s min=%s max=%s bands=%s",
                source, source_coverage["coverage"], source_coverage["materialized"], bucket["evaluated"], bucket["scored"],
                bucket["duplicates"], bucket["eligible"], bucket["scored"] - bucket["eligible"], self._average(bucket),
                bucket["min"] if bucket["min"] is not None else "n/a", bucket["max"] if bucket["max"] is not None else "n/a", bands,
            )
            category_map = getattr(self, "source_categories", {}).get(source, self.categories)
            for category in ("Painting", "Sculpture", "Drawing", "Object"):
                category_bucket = category_map.get(category, self._new_bucket())
                logger.info(
                    "pool_category_stats source=%s category=%s evaluated=%d eligible=%d avg=%s",
                    source, category, category_bucket["evaluated"], category_bucket["eligible"], self._average(category_bucket),
                )

            secondary_map = getattr(self, "source_secondary_categories", {}).get(source, self.secondary_categories)
            for category in sorted(secondary_map):
                category_bucket = secondary_map[category]
                logger.info(
                    "pool_secondary_category_stats source=%s category=%s evaluated=%d eligible=%d avg=%s",
                    source, category, category_bucket["evaluated"], category_bucket["eligible"], self._average(category_bucket),
                )

            artist_map = getattr(self, "source_artists", {}).get(source, self.artists)
            known = artist_map.get("known", self._new_bucket())
            unknown = artist_map.get("unknown", self._new_bucket())
            known_rate = "n/a" if not known["evaluated"] else f"{known['eligible'] / known['evaluated']:.1%}"
            unknown_rate = "n/a" if not unknown["evaluated"] else f"{unknown['eligible'] / unknown['evaluated']:.1%}"
            logger.info(
                "pool_artist_stats source=%s known evaluated=%d eligible=%d eligibility_rate=%s avg=%s unknown evaluated=%d eligible=%d eligibility_rate=%s avg=%s",
                source, known["evaluated"], known["eligible"], known_rate, self._average(known),
                unknown["evaluated"], unknown["eligible"], unknown_rate, self._average(unknown),
            )

            flag_map = getattr(self, "source_flags", {}).get(source, self.flags)
            for flag in ("highlight", "on_view", "additional_images"):
                flag_bucket = flag_map.get(flag, self._new_bucket())
                logger.info(
                    "pool_flag_stats source=%s flag=%s evaluated=%d eligible=%d avg=%s",
                    source, flag, flag_bucket["evaluated"], flag_bucket["eligible"], self._average(flag_bucket),
                )


class CandidatePoolTelemetry(ScoringTelemetry):
    def __init__(self):
        super().__init__()
        self.source_categories = {}
        self.source_secondary_categories = {}
        self.source_artists = {}
        self.source_flags = {}

    @staticmethod
    def _dimension_bucket(collection, source, key):
        source_buckets = collection.setdefault(source, {})
        return source_buckets.setdefault(key, ScoringTelemetry._new_bucket())

    def record_scored(self, source, raw_medium, classification, object_name, artist, score, is_highlight, on_view, has_additional_images):
        super().record_scored(
            source, raw_medium, classification, object_name, artist, score,
            is_highlight, on_view, has_additional_images,
        )

        category = ArtworkScorer.classify_medium(raw_medium, classification, object_name)
        category_bucket = self._dimension_bucket(self.source_categories, source, category)
        category_bucket["evaluated"] += 1
        self._record_score(category_bucket, score)

        secondary = classify_secondary_telemetry_label(raw_medium, classification, object_name)
        secondary_bucket = self._dimension_bucket(self.source_secondary_categories, source, secondary)
        secondary_bucket["evaluated"] += 1
        self._record_score(secondary_bucket, score)

        artist_key = "unknown" if any(term in (artist or "").casefold() for term in ("unknown", "anonymous", "unidentified", "various", "maker unknown")) else "known"
        artist_bucket = self._dimension_bucket(self.source_artists, source, artist_key)
        artist_bucket["evaluated"] += 1
        self._record_score(artist_bucket, score)

        for flag, enabled in (("highlight", is_highlight), ("on_view", on_view), ("additional_images", has_additional_images)):
            if enabled:
                flag_bucket = self._dimension_bucket(self.source_flags, source, flag)
                flag_bucket["evaluated"] += 1
                self._record_score(flag_bucket, score)

    @staticmethod
    def _export_dimension_map(dimension_map, source):
        return {
            key: ScoringTelemetry._export_bucket(dimension_map[source][key])
            for key in sorted(dimension_map.get(source, {}))
        }

    def export_pool(self, coverage):
        sources = {}
        for source in sorted(self.sources):
            source_coverage = coverage.get(source, {"coverage": "partial", "materialized": 0})
            source_data = ScoringTelemetry._export_bucket(self.sources[source])
            source_data.update({
                "coverage": source_coverage["coverage"],
                "materialized": source_coverage["materialized"],
                "primary_category_stats": self._export_dimension_map(self.source_categories, source),
                "secondary_category_stats": self._export_dimension_map(self.source_secondary_categories, source),
                "artist_stats": self._export_dimension_map(self.source_artists, source),
                "flag_stats": self._export_dimension_map(self.source_flags, source),
            })
            sources[source] = source_data
        return {"sources": sources}


@dataclass
class Artwork:
    """Paylaşılacak sanat eseri veri yapısı."""
    museum: str           # 'met' | 'aic' | 'cma'
    id: str               # Müze içi benzersiz ID
    title: str            # Eser adı
    artist: str           # Sanatçı adı
    artist_bio: str       # Sanatçı künyesi (Milliyet, Doğum-Ölüm Yılı)
    date: str             # Yapım yılı / dönemi
    image_url: str        # Yüksek çözünürlüklü görsel bağlantısı
    original_source_url: str # Orijinal müze bağlantısı
    museum_name: str      # Tam müze adı
    location_info: str    # Müze içi oda/galeri lokasyonu
    dimensions: str       # Fiziksel boyutlar
    medium_type: str      # 'Painting', 'Sculpture', 'Drawing', 'Object'
    raw_medium: str       # Ham teknik/malzeme bilgisi (örn: "Oil on canvas", "Bronze", "Marble")
    score: int            # 0 - 100 arası kalite puanı
    style_or_era: Optional[str] = None  # Dönem veya akım bilgisi
    alt_text: str = ""    # Görme engelliler ve SEO için alternatif metin


class ArtworkScorer:
    """
    Sanat eserlerini estetik, çözünürlük, sanatçı değeri ve müze öne çıkarmasına göre
    0-100 arasında puanlayan kalite değerlendirme motoru.
    """

    @staticmethod
    def classify_medium(raw_medium: str, classification: str, object_name: str) -> str:
        """Eserin türünü belirler: Painting, Sculpture, Drawing veya Object."""
        text = f"{raw_medium} {classification} {object_name}".lower()
        
        if any(w in text for w in ["painting", "oil on canvas", "tempera", "fresco", "acrylic", "panel", "maleri", "olie på lærred"]):
            return "Painting"
        if any(w in text for w in ["sculpture", "statue", "marble", "bronze", "terracotta", "bust", "relief", "alabaster", "skulptur"]):
            return "Sculpture"
        if any(w in text for w in ["drawing", "ink on paper", "chalk", "charcoal", "pastel", "etching", "engraving", "woodcut", "watercolor", "print", "tegning", "grafik", "træsnit", "radering"]):
            return "Drawing"
        return "Object"

    @classmethod
    def calculate_score(
        cls,
        title: str,
        artist: str,
        date_str: str,
        raw_medium: str,
        classification: str,
        object_name: str,
        image_url: str,
        is_highlight: bool = False,
        has_additional_images: bool = False,
        on_view: bool = False
    ) -> Tuple[int, str]:
        """
        Eser için 0-100 arası toplam puan ve detaylı puanlama özeti hesaplar.
        """
        score = 0
        reasons = []

        title_lower = title.lower()
        artist_lower = artist.lower()
        medium_lower = f"{raw_medium} {classification} {object_name}".lower()

        # 0. Anti-Filtre (Kırık/Değersiz parça, madeni para veya önemsiz fragmanları anında düşür)
        if any(w in title_lower or w in object_name.lower() for w in FRAGMENT_KEYWORDS):
            return 20, "Fragment/Küçük parça elendi"

        if not image_url or not image_url.startswith("http"):
            return 0, "Görsel URL eksik"

        # 1. Müze Öne Çıkarması ve Galeri Durumu (Maks 25 Puan)
        if is_highlight:
            score += 20
            reasons.append("MuseumHighlight(+20)")
        if on_view:
            score += 5
            reasons.append("OnViewInGallery(+5)")

        # 2. Sanatçı ve Atıf Gücü (Maks 25 Puan)
        is_unknown = any(w in artist_lower for w in ["unknown", "anonymous", "unidentified", "various", "maker unknown"])
        if not is_unknown and len(artist.strip()) > 3:
            score += 15
            reasons.append("KnownArtist(+15)")
            # Ünlü Usta Sanatçı Bonusu
            if any(famous in artist_lower for famous in FAMOUS_ARTISTS):
                score += 10
                reasons.append("FamousMasterBonus(+10)")
        elif is_highlight:
            # Anonim ama müzenin en değerli başyapıtıysa (örn: Antik Yunan / Mısır heykelleri)
            score += 10
            reasons.append("MasterpieceAntique(+10)")

        # 3. Görsel Kalitesi ve Çoklu Açı (Maks 25 Puan)
        if image_url:
            score += 20
            reasons.append("HighResImage(+20)")
        if has_additional_images:
            score += 5
            reasons.append("MultiView(+5)")

        # 4. Tür ve Malzeme Kalitesi (Maks 20 Puan)
        medium_type = cls.classify_medium(raw_medium, classification, object_name)
        if medium_type == "Painting":
            score += 20
            reasons.append("MasterPainting(+20)")
        elif medium_type == "Sculpture":
            if any(w in medium_lower for w in ["marble", "bronze", "terracotta", "limestone", "alabaster"]):
                score += 20
                reasons.append("ClassicSculpture(+20)")
            else:
                score += 15
                reasons.append("Sculpture(+15)")
        elif medium_type == "Drawing":
            if not is_unknown:
                score += 18
                reasons.append("MasterDrawing(+18)")
            else:
                score += 10
                reasons.append("Drawing(+10)")
        else: # Object / Decorative Arts
            if any(w in medium_lower for w in ["gold", "silver", "tapestry", "porcelain", "enamel", "ivory", "mosaic"]):
                score += 18
                reasons.append("PreciousArtifact(+18)")
            else:
                score += 12
                reasons.append("HistoricalObject(+12)")

        # 5. Başlık ve Tarih Bütünlüğü (Maks 5 Puan)
        if title and title.lower() not in ["untitled", "sans titre", "unknown"] and len(title) > 3:
            if date_str and date_str.lower() not in ["unknown date", "n.d."]:
                score += 5
                reasons.append("CompleteMetadata(+5)")

        # Skor sınırlandırması: 0-100
        score = max(0, min(100, score))
        summary = f"Score: {score}/100 [{', '.join(reasons)}]"
        return score, summary


class MuseumAPIClient:
    """The Met, AIC ve CMA API'lerinden 85+ puanlı eserleri filtreleyen istemci."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.last_fetch_stats = {}
        self.scoring_telemetry = ScoringTelemetry()
        self.pool_telemetry = CandidatePoolTelemetry()
        self.pool_coverage = {}

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
        payload = self.build_scoring_telemetry_export(publish_success, run_timestamp)
        with open(path, "w", encoding="utf-8") as telemetry_file:
            json.dump(payload, telemetry_file, indent=2, ensure_ascii=False, sort_keys=True)
            telemetry_file.write("\n")

    def _set_pool_coverage(self, source: str, coverage: str, materialized: int):
        self.pool_coverage[source] = {
            "coverage": coverage,
            "materialized": materialized,
        }

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

    def _record_aic_pool(self, artworks, posted_ids: Set[str]):
        source = "aic"
        self._set_pool_coverage(source, "full", len(artworks))
        self.pool_telemetry.record_duplicate(
            source, sum(1 for item in artworks if str(item.get("id")) in posted_ids)
        )
        for item in artworks:
            artwork_id = str(item.get("id"))
            if artwork_id in posted_ids:
                continue
            image_id = item.get("image_id")
            image_url = f"https://www.artic.edu/iiif/2/{image_id}/full/1686,/0/default.jpg" if image_id else ""
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

    def _record_cma_pool(self, artworks, posted_ids: Set[str]):
        source = "cma"
        self._set_pool_coverage(source, "full", len(artworks))
        self.pool_telemetry.record_duplicate(
            source, sum(1 for item in artworks if str(item.get("id")) in posted_ids)
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

    def _record_smk_pool(self, artworks, posted_ids: Set[str]):
        source = "smk"
        self._set_pool_coverage(source, "full", len(artworks))
        self.pool_telemetry.record_duplicate(
            source, sum(1 for item in artworks if str(item.get("object_number", "")) in posted_ids)
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

    def _record_harvard_pool(self, artworks, posted_ids: Set[str]):
        source = "harvard"
        self._set_pool_coverage(source, "full", len(artworks))
        self.pool_telemetry.record_duplicate(
            source, sum(1 for item in artworks if str(item.get("id", "")) in posted_ids)
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

    def _start_fetch_stats(self, source: str):
        self.last_fetch_stats = {
            "source": source,
            "candidates": 0,
            "duplicates": 0,
            "rejected_image": 0,
            "rejected_quality": 0,
            "rejected_other": 0,
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

    # ----------------------------------------------------------------------
    # 1. The Metropolitan Museum of Art (The Met)
    # ----------------------------------------------------------------------
    def fetch_met_artwork(self, posted_ids: Set[str], target_medium: str = None) -> Optional[Artwork]:
        """The Met API'den 85+ puanlı kamu malı resim, heykel, çizim veya obje seçer."""
        stats = self._start_fetch_stats("met")
        logger.info(f"The Met API taranıyor (Hedef: {target_medium or 'Karışık'})...")
        
        search_terms = ["masterpiece", "portrait", "sculpture", "painting", "drawing", "renaissance", "marble", "bronze"]
        if random.random() < 0.1:
            search_terms.extend(["self portrait", "last work", "final work"])
        if target_medium == "Painting":
            search_terms = ["painting", "portrait", "oil on canvas", "fresco"]
        elif target_medium == "Sculpture":
            search_terms = ["sculpture", "statue", "marble", "bronze"]
        elif target_medium == "Drawing":
            search_terms = ["drawing", "sketch", "watercolor", "ink on paper"]
        elif target_medium == "Object":
            search_terms = ["artifact", "vase", "jewelry", "armor", "pottery", "sword"]
            
        query = random.choice(search_terms)

        search_url = "https://collectionapi.metmuseum.org/public/collection/v1/search"
        params = {
            "isPublicDomain": "true",
            "hasImages": "true",
            "q": query
        }

        try:
            resp = self.session.get(search_url, params=params, timeout=HTTP_TIMEOUT)
            if resp.status_code != 200:
                logger.error("source=met api_failure type=http_status status=%s", resp.status_code)
                logger.warning(f"The Met arama hatası: HTTP {resp.status_code}")
                return None

            data = resp.json()
            object_ids = data.get("objectIDs")
            if not object_ids:
                self._log_fetch_stats(stats)
                return None

            stats["candidates"] = len(object_ids)
            stats["duplicates"] = sum(1 for oid in object_ids if str(oid) in posted_ids)
            self.scoring_telemetry.record_duplicate("met", stats["duplicates"])
            self.pool_coverage["met"] = {"coverage": "partial", "materialized": 0}
            self._safe_pool_operation(self.pool_telemetry.record_duplicate, "met", stats["duplicates"])
            logger.info("source=met candidate_response_count=%d", stats["candidates"])

            random.shuffle(object_ids)
            sample_ids = [str(oid) for oid in object_ids if str(oid) not in posted_ids][:20]

            for obj_id in sample_ids:
                self.scoring_telemetry.record_evaluated("met")
                detail_url = f"https://collectionapi.metmuseum.org/public/collection/v1/objects/{obj_id}"
                obj_resp = self.session.get(detail_url, timeout=HTTP_TIMEOUT)
                if obj_resp.status_code != 200:
                    continue

                obj_data = obj_resp.json()
                self.pool_coverage["met"]["materialized"] += 1
                self._safe_pool_operation(self.pool_telemetry.record_evaluated, "met")

                if not obj_data.get("isPublicDomain", False):
                    stats["rejected_other"] += 1
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

                if score >= MINIMUM_QUALITY_SCORE:
                    stats["eligible"] = 1
                    self._log_fetch_stats(stats)
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
                        museum_name="The Metropolitan Museum of Art, New York",
                        location_info=location_info,
                        dimensions=dimensions,
                        medium_type=medium_type,
                        raw_medium=raw_medium,
                        score=score,
                        style_or_era=department,
                        alt_text=f"{title} by {artist}. {raw_medium}. {department}."
                    )
                stats["rejected_quality"] += 1

        except Exception as e:
            logger.error("source=met api_failure type=%s", type(e).__name__)

        self._log_fetch_stats(stats)
        return None

    # ----------------------------------------------------------------------
    # 2. Art Institute of Chicago (AIC)
    # ----------------------------------------------------------------------
    def fetch_aic_artwork(self, posted_ids: Set[str], target_medium: str = None) -> Optional[Artwork]:
        """Art Institute of Chicago API'den 85+ puanlı eser seçer."""
        stats = self._start_fetch_stats("aic")
        logger.info(f"Art Institute of Chicago (AIC) API taranıyor (Hedef: {target_medium or 'Karışık'})...")
        search_url = "https://api.artic.edu/api/v1/artworks/search"

        should_matches = [
            {"match": {"artwork_type_title": "Painting"}},
            {"match": {"artwork_type_title": "Sculpture"}},
            {"match": {"artwork_type_title": "Drawing and Watercolor"}}
        ]
        
        if target_medium == "Painting":
            should_matches = [{"match": {"artwork_type_title": "Painting"}}]
        elif target_medium == "Sculpture":
            should_matches = [{"match": {"artwork_type_title": "Sculpture"}}]
        elif target_medium == "Drawing":
            should_matches = [{"match": {"artwork_type_title": "Drawing and Watercolor"}}]
        elif target_medium == "Object":
            should_matches = [{"match": {"artwork_type_title": "Decorative Arts"}}, {"match": {"artwork_type_title": "Vessels"}}]

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
            "fields": [
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
            ],
            "limit": 40,
            "page": random_page
        }

        try:
            resp = self.session.post(search_url, json=payload, timeout=HTTP_TIMEOUT)
            if resp.status_code != 200:
                logger.error("source=aic api_failure type=http_status status=%s", resp.status_code)
                logger.warning(f"AIC arama hatası: HTTP {resp.status_code}")
                return None

            data = resp.json()
            artworks = data.get("data", [])
            if not artworks:
                self._log_fetch_stats(stats)
                return None

            stats["candidates"] = len(artworks)
            stats["duplicates"] = sum(1 for item in artworks if str(item.get("id")) in posted_ids)
            self.scoring_telemetry.record_duplicate("aic", stats["duplicates"])
            self._safe_pool_operation(self._record_aic_pool, artworks, posted_ids)
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

                image_url = f"https://www.artic.edu/iiif/2/{image_id}/full/1686,/0/default.jpg"
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

                if score >= MINIMUM_QUALITY_SCORE:
                    stats["eligible"] = 1
                    self._log_fetch_stats(stats)
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
                        museum_name="Art Institute of Chicago",
                        location_info=location_info,
                        dimensions=dimensions,
                        medium_type=medium_type,
                        raw_medium=raw_medium,
                        score=score,
                        style_or_era=style_title,
                        alt_text=f"{title} by {artist}. {raw_medium}."
                    )
                stats["rejected_quality"] += 1

        except Exception as e:
            logger.error("source=aic api_failure type=%s", type(e).__name__)

        self._log_fetch_stats(stats)
        return None

    # ----------------------------------------------------------------------
    # 3. Cleveland Museum of Art (CMA)
    # ----------------------------------------------------------------------
    def fetch_cma_artwork(self, posted_ids: Set[str], target_medium: str = None) -> Optional[Artwork]:
        """Cleveland Museum of Art Açık Erişim API'sinden 85+ puanlı eser seçer."""
        stats = self._start_fetch_stats("cma")
        logger.info(f"Cleveland Museum of Art (CMA) API taranıyor (Hedef: {target_medium or 'Karışık'})...")
        search_url = "https://openaccess-api.clevelandart.org/api/artworks/"

        random_skip = random.randint(0, 30) * 40
        params = {
            "has_image": "1",
            "is_public_domain": "1",
            "limit": 40,
            "skip": random_skip
        }
        
        if target_medium == "Painting":
            params["type"] = "Painting"
        elif target_medium == "Sculpture":
            params["type"] = "Sculpture"
        elif target_medium == "Drawing":
            params["type"] = "Drawing"
        elif target_medium == "Object":
            params["type"] = "Decorative Art"

        try:
            resp = self.session.get(search_url, params=params, timeout=HTTP_TIMEOUT)
            if resp.status_code != 200:
                logger.error("source=cma api_failure type=http_status status=%s", resp.status_code)
                logger.warning(f"CMA arama hatası: HTTP {resp.status_code}")
                return None

            data = resp.json()
            artworks = data.get("data", [])
            if not artworks:
                self._log_fetch_stats(stats)
                return None

            stats["candidates"] = len(artworks)
            stats["duplicates"] = sum(1 for item in artworks if str(item.get("id")) in posted_ids)
            self.scoring_telemetry.record_duplicate("cma", stats["duplicates"])
            self._safe_pool_operation(self._record_cma_pool, artworks, posted_ids)
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

                if score >= MINIMUM_QUALITY_SCORE:
                    stats["eligible"] = 1
                    self._log_fetch_stats(stats)
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
                        museum_name="The Cleveland Museum of Art",
                        location_info=location_info,
                        dimensions=dimensions,
                        medium_type=medium_type,
                        raw_medium=raw_medium,
                        score=score,
                        style_or_era=culture,
                        alt_text=f"{title} by {artist}. {raw_medium}. {item.get('description', '')[:100]}"
                    )
                stats["rejected_quality"] += 1

        except Exception as e:
            logger.error("source=cma api_failure type=%s", type(e).__name__)

        self._log_fetch_stats(stats)
        return None

    # ----------------------------------------------------------------------
    # 4. Statens Museum for Kunst (SMK) - Kopenhag
    # ----------------------------------------------------------------------
    def fetch_smk_artwork(self, posted_ids: Set[str], target_medium: str = None) -> Optional[Artwork]:
        """SMK (Danimarka) API'sinden eser seçer (API Key gerektirmez)."""
        stats = self._start_fetch_stats("smk")
        logger.info(f"SMK (Statens Museum for Kunst) API taranıyor (Hedef: {target_medium or 'Karışık'})...")
        
        search_url = "https://api.smk.dk/api/v1/art/search"
        random_offset = random.randint(0, 1000)
        
        q_param = "*"
        if target_medium == "Painting":
            q_param = "maleri"
        elif target_medium == "Sculpture":
            q_param = "skulptur"
        elif target_medium == "Drawing":
            q_param = "tegning"
        
        params = {
            "keys": q_param,
            "filters": "has_image:true,public_domain:true",
            "offset": random_offset,
            "rows": 100
        }

        try:
            resp = self.session.get(search_url, params=params, timeout=HTTP_TIMEOUT)
            if resp.status_code != 200:
                logger.error("source=smk api_failure type=http_status status=%s", resp.status_code)
                logger.warning(f"SMK arama hatası: HTTP {resp.status_code}")
                return None

            data = resp.json()
            artworks = data.get("items", [])
            if not artworks:
                self._log_fetch_stats(stats)
                return None

            stats["candidates"] = len(artworks)
            stats["duplicates"] = sum(1 for item in artworks if str(item.get("object_number", "")) in posted_ids)
            self.scoring_telemetry.record_duplicate("smk", stats["duplicates"])
            self._safe_pool_operation(self._record_smk_pool, artworks, posted_ids)
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

                if score >= MINIMUM_QUALITY_SCORE:
                    stats["eligible"] = 1
                    self._log_fetch_stats(stats)
                    medium_type = ArtworkScorer.classify_medium(raw_medium, classification, "")
                    logger.info(f"✓ SMK Eseri Onaylandı ({score}/100): '{title}' by {artist} [{medium_type}]")
                    return Artwork(
                        museum="smk", id=artwork_id, title=title, artist=artist, artist_bio=artist_bio, date=date_str,
                        image_url=image_url, original_source_url=original_source_url,
                        museum_name="Statens Museum for Kunst (SMK), Copenhagen", location_info=location_info, dimensions=dimensions,
                        medium_type=medium_type, raw_medium=raw_medium, score=score, style_or_era="Danish Art",
                        alt_text=f"{title} by {artist}. {raw_medium}."
                    )
                stats["rejected_quality"] += 1
        except Exception as e:
            logger.error("source=smk api_failure type=%s", type(e).__name__)
        self._log_fetch_stats(stats)
        return None

    # ----------------------------------------------------------------------
    # 5. Harvard Art Museums
    # ----------------------------------------------------------------------
    def fetch_harvard_artwork(self, posted_ids: Set[str], target_medium: str = None) -> Optional[Artwork]:
        """Harvard Art Museums API'sinden eser seçer (API Key gerektirir)."""
        stats = self._start_fetch_stats("harvard")
        if not config.HARVARD_API_KEY:
            logger.warning("Harvard API anahtarı (HARVARD_API_KEY) tanımlanmamış, atlanıyor.")
            return None

        logger.info(f"Harvard Art Museums API taranıyor (Hedef: {target_medium or 'Karışık'})...")
        
        search_url = "https://api.harvardartmuseums.org/object"
        random_page = random.randint(1, 50)
        
        params = {
            "apikey": config.HARVARD_API_KEY,
            "hasimage": 1,
            "permissionlevel": 0, # Public Domain
            "sort": "random",
            "page": random_page,
            "size": 20
        }
        
        if target_medium == "Painting":
            params["classification"] = "Paintings"
        elif target_medium == "Sculpture":
            params["classification"] = "Sculpture"
        elif target_medium == "Drawing":
            params["classification"] = "Drawings"

        try:
            resp = self.session.get(search_url, params=params, timeout=HTTP_TIMEOUT)
            if resp.status_code != 200:
                logger.error("source=harvard api_failure type=http_status status=%s", resp.status_code)
                logger.warning(f"Harvard arama hatası: HTTP {resp.status_code}")
                return None

            data = resp.json()
            artworks = data.get("records", [])
            if not artworks:
                self._log_fetch_stats(stats)
                return None

            stats["candidates"] = len(artworks)
            stats["duplicates"] = sum(1 for item in artworks if str(item.get("id", "")) in posted_ids)
            self.scoring_telemetry.record_duplicate("harvard", stats["duplicates"])
            self._safe_pool_operation(self._record_harvard_pool, artworks, posted_ids)
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
                artist_bio = "Unknown Artist"
                people = item.get("people", [])
                if people:
                    artist = people[0].get("name", "Unknown Artist")
                    artist_bio = artist
                    displaydate = people[0].get("displaydate", "")
                    culture = people[0].get("culture", "")
                    if displaydate or culture:
                        artist_bio = f"{artist} ({culture}, {displaydate})".replace(" (, ", " (").replace(", )", ")")

                date_str = str(item.get("dated", "Unknown Date"))
                raw_medium = item.get("medium", "") or ""
                classification = item.get("classification", "") or ""
                
                dimensions = (item.get("dimensions") or "").strip() or "Unknown dimensions"
                location_info = "Harvard Art Museums"
                original_source_url = (item.get("url") or "").strip()

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

                if score >= MINIMUM_QUALITY_SCORE:
                    stats["eligible"] = 1
                    self._log_fetch_stats(stats)
                    medium_type = ArtworkScorer.classify_medium(raw_medium, classification, "")
                    logger.info(f"✓ Harvard Eseri Onaylandı ({score}/100): '{title}' by {artist} [{medium_type}]")
                    return Artwork(
                        museum="harvard", id=artwork_id, title=title, artist=artist, artist_bio=artist_bio, date=date_str,
                        image_url=image_url, original_source_url=original_source_url, museum_name="Harvard Art Museums",
                        location_info=location_info, dimensions=dimensions,
                        medium_type=medium_type, raw_medium=raw_medium, score=score, style_or_era=item.get("culture", ""),
                        alt_text=f"{title} by {artist}. {raw_medium}."
                    )
                stats["rejected_quality"] += 1
        except Exception as e:
            logger.error("source=harvard api_failure type=%s", type(e).__name__)
        self._log_fetch_stats(stats)
        return None

    # ----------------------------------------------------------------------
    # Rastgele Müze Seçimi ve Fallback Orkestrasyonu
    # ----------------------------------------------------------------------
    def get_random_artwork(self, posted_ids_by_museum: Dict[str, List[str]], target_medium: str = None) -> Optional[Artwork]:
        """
        Müzeler arasında rastgele seçim yapar. Yalnızca 85+ puan alan eserleri kabul eder.
        Eğer target_medium belirtilmişse o kategoriye ağırlık verir.
        """
        museum_fetchers = [
            ("met", self.fetch_met_artwork),
            ("aic", self.fetch_aic_artwork),
            ("cma", self.fetch_cma_artwork),
            ("smk", self.fetch_smk_artwork),
            ("harvard", self.fetch_harvard_artwork)
        ]

        random.shuffle(museum_fetchers)
        run_stats = {
            "source": "none",
            "candidates": 0,
            "duplicates": 0,
            "rejected_image": 0,
            "rejected_quality": 0,
            "rejected_other": 0,
            "eligible": 0,
        }

        for museum_key, fetcher_func in museum_fetchers:
            posted_set = set(posted_ids_by_museum.get(museum_key, []))
            self.scoring_telemetry.record_attempt(museum_key)
            artwork = fetcher_func(posted_set, target_medium)
            fetch_stats = self.last_fetch_stats
            for key in ("candidates", "duplicates", "rejected_image", "rejected_quality", "rejected_other", "eligible"):
                run_stats[key] += fetch_stats.get(key, 0)
            if artwork and artwork.score >= MINIMUM_QUALITY_SCORE:
                self.scoring_telemetry.record_first_eligible(museum_key, artwork.score)
                # Eser türü uyuşmazlığına karşı ek bir kontrol
                if target_medium and artwork.medium_type != target_medium:
                    run_stats["rejected_other"] += 1
                    logger.warning(f"Bulunan eser ({artwork.medium_type}) hedeflenen tür ({target_medium}) ile uyuşmadı, bir sonraki müzeye geçiliyor.")
                    continue
                run_stats["source"] = museum_key
                self.last_run_stats = run_stats
                self.scoring_telemetry.record_selected(museum_key, artwork.score)
                logger.info(
                    "selection_summary source=%s candidates=%d duplicates=%d "
                    "rejected_quality=%d eligible=%d selected=%s",
                    run_stats["source"], run_stats["candidates"], run_stats["duplicates"],
                    run_stats["rejected_quality"], run_stats["eligible"], artwork.id,
                )
                return artwork
        self.last_run_stats = run_stats
        logger.info(
            "selection_summary source=none candidates=%d duplicates=%d rejected_quality=%d eligible=%d selected=none",
            run_stats["candidates"], run_stats["duplicates"], run_stats["rejected_quality"], run_stats["eligible"],
        )
        logger.error("Hiçbir müze API'sinden 65/100 kriterini sağlayan bir eser bulunamadı!")
        return None
