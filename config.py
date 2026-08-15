"""
config.py - Proje Konfigürasyonu ve Sabitler
"""

import os
import logging
from pathlib import Path
from dotenv import load_dotenv

# .env dosyasını yükle (yerel çalışma için)
load_dotenv()

# Dizin Yolları
BASE_DIR = Path(__file__).resolve().parent
POSTED_IDS_FILE = BASE_DIR / "posted_ids.json"

# Tumblr API Kimlik Bilgileri
TUMBLR_CONSUMER_KEY = os.getenv("TUMBLR_CONSUMER_KEY", "").strip()
TUMBLR_CONSUMER_SECRET = os.getenv("TUMBLR_CONSUMER_SECRET", "").strip()
TUMBLR_OAUTH_TOKEN = os.getenv("TUMBLR_OAUTH_TOKEN", "").strip()
TUMBLR_OAUTH_SECRET = os.getenv("TUMBLR_OAUTH_SECRET", "").strip()
TUMBLR_BLOG_NAME = os.getenv("TUMBLR_BLOG_NAME", "").strip()

# Çapraz Yönlendirme Metni
INSTAGRAM_CALLOUT = "Follow on Instagram for more: @artfolio.db"

# Tumblr SEO Etiketleri (Tam 5 adet olacak şekilde dinamik olarak tamamlanır)
DEFAULT_TAGS = ["art", "classical art", "oil painting", "museum"]
FALLBACK_TAG = "fine art"

# Loglama Yapılandırması
def setup_logging():
    """Konsol ve GitHub Actions için standart loglama biçimlendiricisi."""
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    return logging.getLogger("artfolio_bot")
