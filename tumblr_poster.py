"""
tumblr_poster.py - Tumblr API Paylaşım ve Formatlama Modülü
Resim, Heykel, Çizim ve Değerli Objeler için dinamik SEO etiketleme ve açıklama formatlayıcı.
"""

import re
import random
import logging
from typing import List, Optional
import pytumblr
from museum_api import Artwork
import config

QUESTIONS = [
    "Sizce bu eserdeki en dikkat çekici detay nedir?",
    "Bu eseri evinizin hangi odasına asardınız?",
    "Eserdeki renk kullanımı size hangi duyguyu hissettiriyor?",
    "Sizce karakterin gözlerindeki ifade ne anlatıyor?",
    "Sanatçı burada ne anlatmak istemiş olabilir?",
]


logger = logging.getLogger("artfolio_bot.tumblr_poster")


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
        Eser bilgilerini kullanarak (ve varsa renk paleti HTML'i ekleyerek) post açıklamasını oluşturur.
        """
        medium_info = f"{artwork.medium_type} ({artwork.raw_medium})" if artwork.raw_medium else artwork.medium_type

        caption_lines = [
            f"<p><b>Title:</b> {artwork.title}</p>",
            f"<p><b>Artist:</b> {artwork.artist}</p>",
            f"<p><b>Date:</b> {artwork.date}</p>",
            f"<p><b>Type:</b> {medium_info}</p>",
            f"<p><b>Museum:</b> {artwork.museum_name}</p>",
            "<br>"
        ]
            
        caption_lines.append(f"<p>{config.INSTAGRAM_CALLOUT}</p>")
        
        # Günün sorusu ekle
        question = random.choice(QUESTIONS)
        caption_lines.append(f"<br><p><i>{question}</i></p>")
        
        return "".join(caption_lines)

    def generate_tags(self, artwork: Artwork) -> List[str]:
        """
        Eserin türüne (Resim, Heykel, Çizim, Obje) göre optimize edilmiş TAM 5 adet Tumblr SEO etiketi üretir.
        """
        # Tür bazlı ana etiket
        medium_tag_map = {
            "Painting": "oil painting",
            "Sculpture": "sculpture",
            "Drawing": "drawing",
            "Object": "artifact"
        }
        type_tag = medium_tag_map.get(artwork.medium_type, "fine art")

        # 1-3. Temel Etiketler
        base_tags = ["art", "classical art", type_tag, "museum"]

        # 5. Dinamik Etiket (Dönem / Stil / Akım veya Fallback)
        fifth_tag = "fine art"
        if artwork.style_or_era:
            clean_style = re.sub(r"[^a-zA-Z0-9\s]", "", artwork.style_or_era).strip().lower()
            if clean_style and len(clean_style) <= 25 and clean_style not in base_tags:
                fifth_tag = clean_style
        elif artwork.medium_type == "Sculpture":
            fifth_tag = "classical sculpture"
        elif artwork.medium_type == "Drawing":
            fifth_tag = "master drawing"
        elif artwork.medium_type == "Painting":
            fifth_tag = "renaissance"

        tags = base_tags + [fifth_tag]

        # Tekilleştir ve tam olarak 5 etiket döndür
        unique_tags = []
        for t in tags:
            if t not in unique_tags:
                unique_tags.append(t)

        fallbacks = ["renaissance", "masterpiece", "fine art", "history of art", "visual art"]
        for fb in fallbacks:
            if len(unique_tags) >= 5:
                break
            if fb not in unique_tags:
                unique_tags.append(fb)

        return unique_tags[:5]

    def post_artwork(self, artwork: Artwork, image_paths: Optional[List[str]] = None) -> bool:
        """
        Sanat eserini Tumblr blogunda fotoğraf postu olarak paylaşır.
        Eğer image_paths verilirse o dosyaları, verilmezse görsel linkini kullanır.
        """
        caption = self.format_caption(artwork)
        tags = self.generate_tags(artwork)

        logger.info(f"Tumblr gönderisi hazırlanıyor: '{artwork.title}' [{artwork.medium_type}, Score: {artwork.score}/100]")
        logger.info(f"Kullanılan etiketler (Tam 5 adet): {tags}")

        try:
            kwargs = {
                "state": "published",
                "caption": caption,
                "tags": tags
            }
            if artwork.alt_text:
                kwargs["alt_text"] = artwork.alt_text

            if image_paths and len(image_paths) > 0:
                response = self.client.create_photo(self.blog_name, data=image_paths, **kwargs)
            else:
                response = self.client.create_photo(self.blog_name, source=artwork.image_url, **kwargs)

            if isinstance(response, dict) and "id" in response:
                post_id = response["id"]
                logger.info(f"✓ Tumblr paylaşımı BAŞARILI! Post ID: {post_id}")
                return True
            elif isinstance(response, dict) and "meta" in response:
                status = response["meta"].get("status")
                msg = response["meta"].get("msg")
                logger.error(f"Tumblr API Hatası: [{status}] {msg}")
                return False
            else:
                logger.warning(f"Tumblr API beklenmeyen yanıt: {response}")
                return False

        except Exception as e:
            logger.error(f"Tumblr paylaşımı sırasında beklenmedik hata oluştu: {e}", exc_info=True)
            return False
