"""
museum_api.py - Açık Erişim Müze API Entegrasyonları (The Met, AIC, CMA)
Resim, Heykel, Çizim ve Değerli Objeler için 85/100 Kalite Puanlama Sistemi içerir.
"""

import re
import random
import logging
from dataclasses import dataclass
from typing import Optional, Dict, List, Set, Tuple
import requests

logger = logging.getLogger("artfolio_bot.museum_api")

HTTP_TIMEOUT = 20
USER_AGENT = "artfolio-bot/1.0 (Tumblr Art Curation Bot)"
MINIMUM_QUALITY_SCORE = 85

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


@dataclass
class Artwork:
    """Paylaşılacak sanat eseri veri yapısı."""
    museum: str           # 'met' | 'aic' | 'cma'
    id: str               # Müze içi benzersiz ID
    title: str            # Eser adı
    artist: str           # Sanatçı adı
    date: str             # Yapım yılı / dönemi
    image_url: str        # Yüksek çözünürlüklü görsel bağlantısı
    museum_name: str      # Tam müze adı
    medium_type: str      # 'Painting', 'Sculpture', 'Drawing', 'Object'
    raw_medium: str       # Ham teknik/malzeme bilgisi (örn: "Oil on canvas", "Bronze", "Marble")
    score: int            # 0 - 100 arası kalite puanı
    style_or_era: Optional[str] = None  # Dönem veya akım bilgisi


class ArtworkScorer:
    """
    Sanat eserlerini estetik, çözünürlük, sanatçı değeri ve müze öne çıkarmasına göre
    0-100 arasında puanlayan kalite değerlendirme motoru.
    """

    @staticmethod
    def classify_medium(raw_medium: str, classification: str, object_name: str) -> str:
        """Eserin türünü belirler: Painting, Sculpture, Drawing veya Object."""
        text = f"{raw_medium} {classification} {object_name}".lower()
        
        if any(w in text for w in ["painting", "oil on canvas", "tempera", "fresco", "acrylic", "panel"]):
            return "Painting"
        if any(w in text for w in ["sculpture", "statue", "marble", "bronze", "terracotta", "bust", "relief", "alabaster"]):
            return "Sculpture"
        if any(w in text for w in ["drawing", "ink on paper", "chalk", "charcoal", "pastel", "etching", "engraving", "woodcut", "watercolor", "print"]):
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

    # ----------------------------------------------------------------------
    # 1. The Metropolitan Museum of Art (The Met)
    # ----------------------------------------------------------------------
    def fetch_met_artwork(self, posted_ids: Set[str]) -> Optional[Artwork]:
        """The Met API'den 85+ puanlı kamu malı resim, heykel, çizim veya obje seçer."""
        logger.info("The Met API taranıyor (Resim, Heykel, Çizim, Başyapıtlar)...")
        search_terms = ["masterpiece", "portrait", "sculpture", "painting", "drawing", "renaissance", "marble", "bronze"]
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
                logger.warning(f"The Met arama hatası: HTTP {resp.status_code}")
                return None

            data = resp.json()
            object_ids = data.get("objectIDs")
            if not object_ids:
                return None

            random.shuffle(object_ids)
            sample_ids = [str(oid) for oid in object_ids if str(oid) not in posted_ids][:20]

            for obj_id in sample_ids:
                detail_url = f"https://collectionapi.metmuseum.org/public/collection/v1/objects/{obj_id}"
                obj_resp = self.session.get(detail_url, timeout=HTTP_TIMEOUT)
                if obj_resp.status_code != 200:
                    continue

                obj_data = obj_resp.json()

                if not obj_data.get("isPublicDomain", False):
                    continue

                primary_image = obj_data.get("primaryImage", "").strip()
                if not primary_image:
                    continue

                title = obj_data.get("title", "Untitled").strip() or "Untitled"
                artist = obj_data.get("artistDisplayName", "").strip() or "Unknown Artist"
                date_str = obj_data.get("objectDate", "").strip() or "Unknown Date"
                department = obj_data.get("department", "")
                raw_medium = obj_data.get("medium", "")
                classification = obj_data.get("classification", "")
                object_name = obj_data.get("objectName", "")
                is_highlight = obj_data.get("isHighlight", False)
                additional_images = bool(obj_data.get("additionalImages", []))

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

                if score >= MINIMUM_QUALITY_SCORE:
                    medium_type = ArtworkScorer.classify_medium(raw_medium, classification, object_name)
                    logger.info(f"✓ The Met Eseri Onaylandı ({score}/100): '{title}' by {artist} [{medium_type}]")
                    return Artwork(
                        museum="met",
                        id=str(obj_id),
                        title=title,
                        artist=artist,
                        date=date_str,
                        image_url=primary_image,
                        museum_name="The Metropolitan Museum of Art, New York",
                        medium_type=medium_type,
                        raw_medium=raw_medium,
                        score=score,
                        style_or_era=department
                    )

        except Exception as e:
            logger.error(f"The Met API işleminde hata: {e}")

        return None

    # ----------------------------------------------------------------------
    # 2. Art Institute of Chicago (AIC)
    # ----------------------------------------------------------------------
    def fetch_aic_artwork(self, posted_ids: Set[str]) -> Optional[Artwork]:
        """Art Institute of Chicago API'den 85+ puanlı eser seçer."""
        logger.info("Art Institute of Chicago (AIC) API taranıyor...")
        search_url = "https://api.artic.edu/api/v1/artworks/search"

        random_page = random.randint(1, 30)
        payload = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"is_public_domain": True}}
                    ],
                    "should": [
                        {"match": {"artwork_type_title": "Painting"}},
                        {"match": {"artwork_type_title": "Sculpture"}},
                        {"match": {"artwork_type_title": "Drawing and Watercolor"}}
                    ],
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
                logger.warning(f"AIC arama hatası: HTTP {resp.status_code}")
                return None

            data = resp.json()
            artworks = data.get("data", [])
            if not artworks:
                return None

            random.shuffle(artworks)

            for item in artworks:
                artwork_id = str(item.get("id"))
                if artwork_id in posted_ids:
                    continue

                image_id = item.get("image_id")
                if not image_id:
                    continue

                image_url = f"https://www.artic.edu/iiif/2/{image_id}/full/1686,/0/default.jpg"
                title = item.get("title", "Untitled").strip() or "Untitled"
                artist_raw = item.get("artist_display", "").strip() or "Unknown Artist"
                artist = artist_raw.split("\n")[0].strip()
                date_str = item.get("date_display", "").strip() or "Unknown Date"
                raw_medium = item.get("medium_display", "") or ""
                classification = item.get("classification_title", "") or ""
                object_name = item.get("artwork_type_title", "") or ""
                is_boosted = item.get("is_boosted", False)
                is_on_view = item.get("is_on_view", False)
                style_title = item.get("style_title")

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

                if score >= MINIMUM_QUALITY_SCORE:
                    medium_type = ArtworkScorer.classify_medium(raw_medium, classification, object_name)
                    logger.info(f"✓ AIC Eseri Onaylandı ({score}/100): '{title}' by {artist} [{medium_type}]")
                    return Artwork(
                        museum="aic",
                        id=artwork_id,
                        title=title,
                        artist=artist,
                        date=date_str,
                        image_url=image_url,
                        museum_name="Art Institute of Chicago",
                        medium_type=medium_type,
                        raw_medium=raw_medium,
                        score=score,
                        style_or_era=style_title
                    )

        except Exception as e:
            logger.error(f"AIC API işleminde hata: {e}")

        return None

    # ----------------------------------------------------------------------
    # 3. Cleveland Museum of Art (CMA)
    # ----------------------------------------------------------------------
    def fetch_cma_artwork(self, posted_ids: Set[str]) -> Optional[Artwork]:
        """Cleveland Museum of Art Açık Erişim API'sinden 85+ puanlı eser seçer."""
        logger.info("Cleveland Museum of Art (CMA) API taranıyor...")
        search_url = "https://openaccess-api.clevelandart.org/api/artworks/"

        random_skip = random.randint(0, 30) * 40
        params = {
            "has_image": "1",
            "is_public_domain": "1",
            "limit": 40,
            "skip": random_skip
        }

        try:
            resp = self.session.get(search_url, params=params, timeout=HTTP_TIMEOUT)
            if resp.status_code != 200:
                logger.warning(f"CMA arama hatası: HTTP {resp.status_code}")
                return None

            data = resp.json()
            artworks = data.get("data", [])
            if not artworks:
                return None

            random.shuffle(artworks)

            for item in artworks:
                artwork_id = str(item.get("id"))
                if artwork_id in posted_ids:
                    continue

                images = item.get("images", {})
                image_url = None
                if images.get("web", {}).get("url"):
                    image_url = images["web"]["url"]
                elif images.get("print", {}).get("url"):
                    image_url = images["print"]["url"]

                if not image_url:
                    continue

                title = item.get("title", "Untitled").strip() or "Untitled"
                creators = item.get("creators", [])
                artist = "Unknown Artist"
                if creators and isinstance(creators, list):
                    artist = creators[0].get("description", "Unknown Artist")

                date_str = item.get("creation_date", "").strip() or "Unknown Date"
                raw_medium = item.get("technique", "") or item.get("type", "")
                classification = item.get("type", "")
                object_name = item.get("department", "")
                culture = item.get("culture", [""])[0] if isinstance(item.get("culture"), list) and item.get("culture") else None
                on_view = bool(item.get("current_location"))
                is_highlight = bool(item.get("share_license_status") == "CC0" and on_view)

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

                if score >= MINIMUM_QUALITY_SCORE:
                    medium_type = ArtworkScorer.classify_medium(raw_medium, classification, object_name)
                    logger.info(f"✓ CMA Eseri Onaylandı ({score}/100): '{title}' by {artist} [{medium_type}]")
                    return Artwork(
                        museum="cma",
                        id=artwork_id,
                        title=title,
                        artist=artist,
                        date=date_str,
                        image_url=image_url,
                        museum_name="The Cleveland Museum of Art",
                        medium_type=medium_type,
                        raw_medium=raw_medium,
                        score=score,
                        style_or_era=culture
                    )

        except Exception as e:
            logger.error(f"CMA API işleminde hata: {e}")

        return None

    # ----------------------------------------------------------------------
    # Rastgele Müze Seçimi ve Fallback Orkestrasyonu
    # ----------------------------------------------------------------------
    def get_random_artwork(self, posted_ids_by_museum: Dict[str, List[str]]) -> Optional[Artwork]:
        """
        Müzeler arasında rastgele seçim yapar. Yalnızca 85+ puan alan eserleri kabul eder.
        """
        museum_fetchers = [
            ("met", self.fetch_met_artwork),
            ("aic", self.fetch_aic_artwork),
            ("cma", self.fetch_cma_artwork)
        ]

        random.shuffle(museum_fetchers)

        for museum_key, fetcher_func in museum_fetchers:
            posted_set = set(posted_ids_by_museum.get(museum_key, []))
            artwork = fetcher_func(posted_set)
            if artwork and artwork.score >= MINIMUM_QUALITY_SCORE:
                return artwork
            logger.warning(f"{museum_key.upper()} üzerinden 85+ puanlı eser arayışı devam ediyor...")

        logger.error("Hiçbir müze API'sinden 85/100 kriterini sağlayan bir eser bulunamadı!")
        return None
