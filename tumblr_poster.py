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
        Eser bilgilerini kullanarak post açıklamasını oluşturur.
        """
        medium_info = f"{artwork.medium_type} ({artwork.raw_medium})" if artwork.raw_medium else artwork.medium_type

        # Use artist_bio if available, otherwise just artist
        artist_display = getattr(artwork, 'artist_bio', artwork.artist)
        
        caption_lines = [
            f"<p><b>Title:</b> {artwork.title}</p>",
            f"<p><b>Artist:</b> {artist_display}</p>",
            f"<p><b>Date:</b> {artwork.date}</p>",
        ]
        
        if getattr(artwork, 'dimensions', ''):
            caption_lines.append(f"<p><b>Dimensions:</b> {artwork.dimensions}</p>")
            
        caption_lines.append(f"<p><b>Type:</b> {medium_info}</p>")
        
        # Cross-Tag Navigation
        clean_artist_tag = re.sub(r'[^a-z0-9 ]', '', artwork.artist.lower()).replace(' ', '')
        if clean_artist_tag:
            caption_lines.append(f'<br><p>More from this artist: <a href="/tagged/my:{clean_artist_tag}">#my:{clean_artist_tag}</a></p>')
        
        return "".join(caption_lines)

    def generate_tags(self, artwork: Artwork) -> List[str]:
        """
        Eserin türüne (Resim, Heykel, Çizim, Obje) göre optimize edilmiş TAM 5 adet Tumblr SEO etiketi üretir.
        """
        GENERAL_TAGS = ["classical art", "art history", "fine art", "traditional art", "museum art", "art curation", "masterpiece", "visual art", "art appreciation"]
        MOVEMENT_TAGS = ["renaissance art", "baroque art", "romanticism", "pre raphaelite", "rococo", "neoclassicism", "impressionism", "post impressionism", "dutch golden age", "symbolism art"]
        AESTHETIC_TAGS = ["dark academia", "light academia", "classical aesthetic", "vintage aesthetic", "romantic aesthetic", "moody art", "antique aesthetic", "ethereal art", "historical aesthetic", "poetic art"]
        GENRE_TAGS = ["oil portrait", "classical portrait", "classical landscape", "still life painting", "mythology art", "greek mythology art", "botanical painting", "chiaroscuro", "female portrait", "historical painting"]
        
        tags = []
        
        # 1. Genel Sanat Etiketi
        tags.append(random.choice(GENERAL_TAGS))
        
        # 2. Akım Etiketi
        era_tag = random.choice(MOVEMENT_TAGS)
        if artwork.style_or_era:
            clean_style = re.sub(r"[^a-zA-Z0-9\s]", "", artwork.style_or_era).strip().lower()
            if clean_style and len(clean_style) <= 25:
                # If specific style matches any movement
                for m_tag in MOVEMENT_TAGS:
                    if m_tag in clean_style or clean_style in m_tag:
                        era_tag = m_tag
                        break
        tags.append(era_tag)
        
        # 3. Estetik Etiketi
        tags.append(random.choice(AESTHETIC_TAGS))
        
        # 4. Teknik Etiketi
        medium_tag_map = {
            "Painting": "oil painting",
            "Sculpture": "sculpture",
            "Drawing": "drawing",
            "Object": "artifact"
        }
        tech_tag = medium_tag_map.get(artwork.medium_type, "fine art")
        if artwork.raw_medium:
            raw_clean = artwork.raw_medium.lower()
            if "marble" in raw_clean: tech_tag = "marble sculpture"
            elif "bronze" in raw_clean: tech_tag = "bronze sculpture"
            elif "watercolor" in raw_clean: tech_tag = "watercolor art"
            elif "fresco" in raw_clean: tech_tag = "fresco"
        tags.append(tech_tag)
        
        # 5. Konu/Karakter Etiketi
        subject_tag = random.choice(GENRE_TAGS)
        title_clean = artwork.title.lower()
        if "portrait" in title_clean: subject_tag = "classical portrait"
        elif "landscape" in title_clean: subject_tag = "classical landscape"
        elif "flower" in title_clean: subject_tag = "botanical painting"
        elif "myth" in title_clean or "venus" in title_clean or "apollo" in title_clean: subject_tag = "mythology art"
        tags.append(subject_tag)
        
        # Ensure tags are clean, lowercase, no special characters, and unique
        final_tags = []
        for t in tags:
            clean_t = re.sub(r"[^a-z0-9\s]", "", t.lower()).strip()
            if clean_t and clean_t not in final_tags:
                final_tags.append(clean_t)
                
        # Fill with fallback if less than 5
        fallbacks = ["art", "classical art", "museum", "history of art", "aesthetic"]
        for fb in fallbacks:
            if len(final_tags) >= 5: break
            if fb not in final_tags: final_tags.append(fb)
            
        return final_tags[:5]

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
            if getattr(artwork, 'original_source_url', ''):
                kwargs["link"] = artwork.original_source_url

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
