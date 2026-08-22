"""
tumblr_poster.py - Tumblr API Paylaşım ve Formatlama Modülü
Resim, Heykel, Çizim ve Değerli Objeler için dinamik SEO etiketleme ve açıklama formatlayıcı.
"""

import re
import random
import logging
from html import escape
from typing import List, Optional
import pytumblr
from museum_api import Artwork
import config

logger = logging.getLogger("artfolio_bot.tumblr_poster")

_PROTECTED_ARTIST_TERMS = (
    "unknown", "anonymous", "workshop of", "studio of", "attributed to",
    "circle of", "school of", "after", "follower of", "museum", "institute",
    "academy", "foundation", "department", "collection", "culture",
)
_DIMENSION_UNIT_RE = re.compile(
    r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>millimeters?|centimeters?|centimetres?|meters?|inches?)\b",
    re.IGNORECASE,
)
_SIMPLE_DIMENSION_RE = re.compile(
    r"^\s*(?P<first>\d+(?:[.,]\d+)?)\s*(?P<unit>mm|cm|m)"
    r"(?:\s*[x×]\s*(?P<value>\d+(?:[.,]\d+)?)\s*(?P=unit))+\s*$",
    re.IGNORECASE,
)
_SIMPLE_YEAR_RE = re.compile(r"^\s*(?P<year>\d{4})\s*$")
_TAG_PLACEHOLDERS = {"", "none", "unknown", "unknown artist", "unknown date", "untitled", "n/a", "na"}
_MOVEMENT_TAGS = {
    "renaissance": "renaissance art",
    "baroque": "baroque art",
    "romanticism": "romanticism",
    "impressionism": "impressionism",
    "post impressionism": "post impressionism",
    "neoclassicism": "neoclassical art",
    "symbolism": "symbolism art",
}


def _clean_display_text(value) -> str:
    """Trim harmless whitespace without changing source meaning."""
    if value is None:
        return ""
    return " ".join(str(value).split())


def _normalize_artist_name(value, protected_terms) -> str:
    artist = _clean_display_text(value)
    if not artist or artist.count(",") != 1:
        return artist

    lowered = artist.casefold()
    if any(term in lowered for term in protected_terms):
        return artist
    # Artist biographies commonly contain commas inside parentheses; those are
    # not surname/given-name values and must remain untouched.
    if any(mark in artist for mark in ("(", ")", "[", "]")):
        return artist

    surname, given = (part.strip() for part in artist.split(",", 1))
    if not surname or not given or any(char in surname + given for char in ("<", ">", "\n")):
        return artist
    return f"{given} {surname}"


def normalize_artist_name(value) -> str:
    """Safely display a small subset of ``Surname, Given`` artist values."""
    return _normalize_artist_name(value, _PROTECTED_ARTIST_TERMS)


def normalize_dimensions(value) -> str:
    """Normalize explicit metric units while preserving unknown formats."""
    dimensions = _clean_display_text(value)
    if not dimensions:
        return ""

    unit_names = {
        "millimeter": "mm", "millimeters": "mm",
        "centimeter": "cm", "centimeters": "cm", "centimetre": "cm", "centimetres": "cm",
        "meter": "m", "meters": "m", "inch": "in", "inches": "in",
    }

    def replace_unit(match):
        return f"{match.group('value')} {unit_names[match.group('unit').casefold()]}"

    normalized = _DIMENSION_UNIT_RE.sub(replace_unit, dimensions)
    simple_match = _SIMPLE_DIMENSION_RE.fullmatch(normalized)
    if simple_match:
        values = re.findall(r"\d+(?:[.,]\d+)?", normalized)
        return f" × ".join(values) + f" {simple_match.group('unit').lower()}"
    return normalized


def normalize_medium_display(medium_type, raw_medium) -> str:
    """Clean display whitespace and remove only exact duplicate medium text."""
    medium = _clean_display_text(medium_type)
    raw = _clean_display_text(raw_medium)
    if not raw:
        return medium
    if medium and medium.casefold() == raw.casefold():
        return medium
    return f"{medium} ({raw})" if medium else raw


def artist_internal_tag(value) -> str:
    """Return the legacy raw-value slug used by the existing ``my:`` link."""
    # Internal navigation deliberately does not use display normalization:
    # existing posts use the raw artist order in this slug.
    raw_artist = _clean_display_text(value)
    return re.sub(r"[^a-z0-9 ]", "", raw_artist.casefold()).replace(" ", "")


def normalize_public_tag(value) -> str:
    """Make one readable, lowercase Tumblr tag or return an empty tag."""
    text = _clean_display_text(value).casefold()
    if text in _TAG_PLACEHOLDERS:
        return ""
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = " ".join(text.split()).strip(" -")
    if text in _TAG_PLACEHOLDERS or len(text) > 80:
        return ""
    return text


def _public_artist_tag(value) -> str:
    artist = normalize_artist_name(value)
    lowered = artist.casefold()
    if any(
        lowered == term or lowered.startswith(f"{term} ")
        for term in (
            "unknown", "anonymous", "unidentified", "workshop of", "studio of",
            "attributed to", "circle of", "school of", "follower of",
        )
    ):
        return ""
    return normalize_public_tag(artist)


def century_tag(value) -> str:
    """Return a century tag only for an unambiguous four-digit year."""
    match = _SIMPLE_YEAR_RE.fullmatch(_clean_display_text(value))
    if not match:
        return ""
    year = int(match.group("year"))
    if year < 1 or year > 9999:
        return ""
    return f"{(year - 1) // 100 + 1}th century art"


def _specific_medium_tag(medium_type, raw_medium) -> str:
    raw = _clean_display_text(raw_medium).casefold()
    medium = _clean_display_text(medium_type)
    if "oil" in raw and any(term in raw for term in ("canvas", "panel", "wood")):
        return "oil painting"
    if "watercolor" in raw or "watercolour" in raw:
        return "watercolor art"
    if "fresco" in raw:
        return "fresco"
    if "marble" in raw and medium.casefold() == "sculpture":
        return "marble sculpture"
    if "bronze" in raw and medium.casefold() == "sculpture":
        return "bronze sculpture"
    return ""


def generate_public_tags(artwork: Artwork) -> List[str]:
    """Build a deterministic, metadata-backed public tag list."""
    candidates = [
        _public_artist_tag(artwork.artist),
        normalize_public_tag(artwork.title),
        normalize_public_tag(artwork.medium_type),
        normalize_public_tag(_specific_medium_tag(artwork.medium_type, artwork.raw_medium)),
    ]

    style = _clean_display_text(artwork.style_or_era)
    style_key = style.casefold()
    candidates.append(normalize_public_tag(_MOVEMENT_TAGS.get(style_key, style)))
    candidates.append(normalize_public_tag(century_tag(artwork.date)))

    candidates.extend(["art", "art history", "museum", "artfolio db"])

    tags = []
    seen = set()
    for candidate in candidates:
        if not candidate:
            continue
        key = candidate.casefold()
        if key in seen:
            continue
        seen.add(key)
        tags.append(candidate)
        if len(tags) == 5:
            break
    return tags


class TumblrPoster:
    """Tumblr fotoğraf gönderilerini oluşturan ve yayınlayan istemci."""

    def __init__(self):
        self._validate_credentials()
        self.client = pytumblr.TumblrRestClient(
            config.TUMBLR_CONSUMER_KEY,
            config.TUMBLR_CONSUMER_SECRET,
            config.TUMBLR_OAUTH_TOKEN,
            config.TUMBLR_OAUTH_SECRET
        )
        self.blog_name = config.TUMBLR_BLOG_NAME

    def _validate_credentials(self):
        """Çevre değişkenlerinin eksiksiz olduğunu doğrular."""
        required = [
            ("TUMBLR_CONSUMER_KEY", config.TUMBLR_CONSUMER_KEY),
            ("TUMBLR_CONSUMER_SECRET", config.TUMBLR_CONSUMER_SECRET),
            ("TUMBLR_OAUTH_TOKEN", config.TUMBLR_OAUTH_TOKEN),
            ("TUMBLR_OAUTH_SECRET", config.TUMBLR_OAUTH_SECRET),
            ("TUMBLR_BLOG_NAME", config.TUMBLR_BLOG_NAME),
        ]
        missing = [name for name, val in required if not val]
        if missing:
            raise ValueError(f"Eksik Tumblr API kimlik bilgileri: {', '.join(missing)}")

    def format_caption(self, artwork: Artwork) -> str:
        """
        Eser bilgilerini kullanarak post açıklamasını oluşturur.
        """
        def escape_value(value) -> str:
            return escape("" if value is None else str(value), quote=True)

        medium_info = normalize_medium_display(artwork.medium_type, artwork.raw_medium)

        # Use artist_bio if available, otherwise just artist
        artist_display = normalize_artist_name(getattr(artwork, 'artist_bio', None))
        if not artist_display:
            artist_display = normalize_artist_name(artwork.artist)
        title = _clean_display_text(artwork.title)
        date = _clean_display_text(artwork.date)
        dimensions = normalize_dimensions(getattr(artwork, 'dimensions', None))

        caption_lines = []
        if title:
            caption_lines.append(f"<p><b>Title:</b> {escape_value(title)}</p>")
        if artist_display:
            caption_lines.append(f"<p><b>Artist:</b> {escape_value(artist_display)}</p>")
        if date:
            caption_lines.append(f"<p><b>Date:</b> {escape_value(date)}</p>")

        if dimensions:
            caption_lines.append(f"<p><b>Dimensions:</b> {escape_value(dimensions)}</p>")

        if medium_info:
            caption_lines.append(f"<p><b>Type:</b> {escape_value(medium_info)}</p>")

        # Cross-Tag Navigation
        clean_artist_tag = artist_internal_tag(artwork.artist)
        if clean_artist_tag:
            caption_lines.append(f'<br><p>More from this artist: <a href="/tagged/my:{clean_artist_tag}">#my:{clean_artist_tag}</a></p>')
        
        return "".join(caption_lines)

    def generate_tags(self, artwork: Artwork) -> List[str]:
        """
        Eserin türüne (Resim, Heykel, Çizim, Obje) göre optimize edilmiş TAM 5 adet Tumblr SEO etiketi üretir.
        """
        return generate_public_tags(artwork)

    def post_artwork(self, artwork: Artwork, image_paths: Optional[List[str]] = None) -> bool:
        """
        Sanat eserini Tumblr blogunda fotoğraf postu olarak paylaşır.
        Eğer image_paths verilirse o dosyaları, verilmezse görsel linkini kullanır.
        """
        caption = self.format_caption(artwork)
        tags = self.generate_tags(artwork)

        logger.info(
            "tumblr_publish_start source=%s object_id=%s title=%r artist=%r score=%s",
            artwork.museum,
            artwork.id,
            artwork.title,
            artwork.artist,
            artwork.score,
        )
        logger.info(f"Tumblr gönderisi hazırlanıyor: '{artwork.title}' [{artwork.medium_type}, Score: {artwork.score}/100]")
        logger.info(f"Kullanılan etiketler (Tam 5 adet): {tags}")



        try:
            kwargs = {
                "state": "published",
                "caption": caption,
                "tags": tags
            }
            if getattr(artwork, 'original_source_url', ''):
                kwargs["link"] = artwork.original_source_url

            if image_paths and len(image_paths) > 0:
                response = self.client.create_photo(self.blog_name, data=image_paths, **kwargs)
            else:
                response = self.client.create_photo(self.blog_name, source=artwork.image_url, **kwargs)

            if isinstance(response, dict) and "id" in response:
                post_id = response["id"]
                logger.info("tumblr_publish_success source=%s object_id=%s post_id=%s", artwork.museum, artwork.id, post_id)
                logger.info(f"✓ Tumblr paylaşımı BAŞARILI! Post ID: {post_id}")
                return True
            elif isinstance(response, dict) and "meta" in response:
                status = response["meta"].get("status")
                msg = response["meta"].get("msg")
                logger.error("tumblr_publish_failure source=%s object_id=%s status=%s", artwork.museum, artwork.id, status)
                logger.error(f"Tumblr API Hatası: [{status}] {msg}")
                return False
            else:
                logger.error("tumblr_publish_failure source=%s object_id=%s reason=unexpected_response", artwork.museum, artwork.id)
                response_keys = sorted(response.keys()) if isinstance(response, dict) else []
                logger.warning(
                    "Tumblr API beklenmeyen yanıt: type=%s keys=%s",
                    type(response).__name__,
                    response_keys,
                )
                return False

        except Exception as e:
            logger.error("tumblr_publish_failure source=%s object_id=%s reason=exception", artwork.museum, artwork.id)
            logger.error("Tumblr paylaşımı sırasında beklenmedik hata oluştu: type=%s", type(e).__name__)
            return False
