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
# Yayın işlem günlüğü (transaction journal) — published_ids.json'ın yanında durur.
PUBLICATION_STATE_FILE = BASE_DIR / "publication_state.json"

# Tumblr API Kimlik Bilgileri
TUMBLR_CONSUMER_KEY = os.getenv("TUMBLR_CONSUMER_KEY", "").strip()
TUMBLR_CONSUMER_SECRET = os.getenv("TUMBLR_CONSUMER_SECRET", "").strip()
TUMBLR_OAUTH_TOKEN = os.getenv("TUMBLR_OAUTH_TOKEN", "").strip()
TUMBLR_OAUTH_SECRET = os.getenv("TUMBLR_OAUTH_SECRET", "").strip()
TUMBLR_BLOG_NAME = os.getenv("TUMBLR_BLOG_NAME", "").strip()

# Üretim kalite eşiği: Bu puanın altındaki eserler yayınlanmaz (0-100 arası).
MINIMUM_QUALITY_SCORE = int(os.getenv("MINIMUM_QUALITY_SCORE", "80"))

if not 0 <= MINIMUM_QUALITY_SCORE <= 100:
    raise ValueError(
        "MINIMUM_QUALITY_SCORE must be between 0 and 100"
    )

# Yayın rezervasyonu zaman aşımı (dakika). Süresi dolan 'reserved' kayıtları tekrar denenebilir olur.
PUBLICATION_RESERVATION_TTL_MINUTES = int(
    os.getenv("PUBLICATION_RESERVATION_TTL_MINUTES", "120")
)

if PUBLICATION_RESERVATION_TTL_MINUTES <= 0:
    raise ValueError(
        "PUBLICATION_RESERVATION_TTL_MINUTES must be a positive integer"
    )

# Kaynak devre kesici (circuit breaker): operasyonel kaynak arızaları (timeout,
# bağlantı hatası, bozuk yanıt) ardışık bu eşik Reach'te kaynağı bir süre atlar.
# İçerik reddi (kalite/hak/duplicate) arıza sayılmaz; state işlem başına fresh'tir.
SOURCE_CIRCUIT_FAILURE_THRESHOLD = int(
    os.getenv("SOURCE_CIRCUIT_FAILURE_THRESHOLD", "3")
)
SOURCE_CIRCUIT_COOLDOWN_SECONDS = int(
    os.getenv("SOURCE_CIRCUIT_COOLDOWN_SECONDS", "300")
)

if SOURCE_CIRCUIT_FAILURE_THRESHOLD < 1:
    raise ValueError(
        "SOURCE_CIRCUIT_FAILURE_THRESHOLD must be >= 1"
    )

if SOURCE_CIRCUIT_COOLDOWN_SECONDS < 1:
    raise ValueError(
        "SOURCE_CIRCUIT_COOLDOWN_SECONDS must be >= 1"
    )

# Yayın geçmişine dayalı son çeşitlilik korumaları. Sanatçı penceresi yayın
# sayısıdır (zaman değil); 0 yalnızca sanatçı cooldown'unu devre dışı bırakır.
ARTIST_COOLDOWN_POSTS = int(os.getenv("ARTIST_COOLDOWN_POSTS", "12"))
SOURCE_MAX_CONSECUTIVE_POSTS = int(
    os.getenv("SOURCE_MAX_CONSECUTIVE_POSTS", "3")
)
MEDIUM_MAX_CONSECUTIVE_POSTS = int(
    os.getenv("MEDIUM_MAX_CONSECUTIVE_POSTS", "3")
)

if ARTIST_COOLDOWN_POSTS < 0:
    raise ValueError("ARTIST_COOLDOWN_POSTS must be >= 0")

if SOURCE_MAX_CONSECUTIVE_POSTS < 1:
    raise ValueError("SOURCE_MAX_CONSECUTIVE_POSTS must be >= 1")

if MEDIUM_MAX_CONSECUTIVE_POSTS < 1:
    raise ValueError("MEDIUM_MAX_CONSECUTIVE_POSTS must be >= 1")


# Rijksmuseum API
# Kullanıcı özel bir key girmezse, bilinen bir public (açık) anahtarı ('0fiuZFhW') varsayılan olarak kullanır.
RIJKSMUSEUM_API_KEY = os.getenv("RIJKSMUSEUM_API_KEY", "0fiuZFhW").strip()

# Harvard Art Museums API
HARVARD_API_KEY = os.getenv("HARVARD_API_KEY", "").strip()

# Çapraz Yönlendirme Metni (Kaldırıldı)

# Tumblr SEO Etiketleri (Tam 5 adet olacak şekilde dinamik olarak tamamlanır)
DEFAULT_TAGS = ["art", "classical art", "oil painting", "museum"]
FALLBACK_TAG = "fine art"

# İçerik Türü Ağırlıkları (Yüzde Oranları)
CONTENT_WEIGHTS = {
    "Painting": 75,
    "Sculpture": 25,
    "Drawing": 0
}

# Loglama Yapılandırması
def setup_logging():
    """Konsol ve GitHub Actions için standart loglama biçimlendiricisi."""
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    return logging.getLogger("artfolio_bot")
