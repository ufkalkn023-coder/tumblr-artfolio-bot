"""
main.py - artfolio.db Tumblr Kürasyon Botu Ana Çalıştırıcı
"""

import sys
import json
import logging
from pathlib import Path
from typing import Dict, List

import config
from museum_api import MuseumAPIClient, Artwork
from tumblr_poster import TumblrPoster

logger = config.setup_logging()


def load_posted_ids() -> Dict[str, List[str]]:
    """posted_ids.json dosyasını okur; dosya yoksa veya bozuksa varsayılan yapıyı döner."""
    default_data = {"met": [], "aic": [], "cma": []}
    if not config.POSTED_IDS_FILE.exists():
        logger.info(f"{config.POSTED_IDS_FILE} bulunamadı, yeni oluşturuluyor.")
        save_posted_ids(default_data)
        return default_data

    try:
        with open(config.POSTED_IDS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Tüm müze anahtarlarının varlığını garanti et
            for key in ["met", "aic", "cma"]:
                if key not in data:
                    data[key] = []
            return data
    except Exception as e:
        logger.error(f"posted_ids.json okunurken hata: {e}. Varsayılan yapı kullanılacak.")
        return default_data


def save_posted_ids(data: Dict[str, List[str]]) -> None:
    """Güncellenmiş paylaşılan eser ID listesini posted_ids.json dosyasına yazar."""
    try:
        with open(config.POSTED_IDS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info(f"posted_ids.json başarıyla güncellendi.")
    except Exception as e:
        logger.error(f"posted_ids.json kaydedilirken hata: {e}")


def run_curation_cycle():
    """Tek bir kürasyon ve paylaşım döngüsünü yürütür."""
    logger.info("=== artfolio.db Tumblr Kürasyon Döngüsü Başlatıldı ===")

    # 1. State Yükle
    posted_data = load_posted_ids()
    total_posted = sum(len(ids) for ids in posted_data.values())
    logger.info(f"Hafızada toplam {total_posted} önceden paylaşılmış eser kayıtlı.")

    # 2. Müzelerden Uygun Eser Çek (Hata payına karşı 3 defa deneme)
    import random
    
    # Hedef tür belirleme (Yüzdelik oranlara göre)
    mediums = list(config.CONTENT_WEIGHTS.keys())
    weights = list(config.CONTENT_WEIGHTS.values())
    target_medium = random.choices(mediums, weights=weights, k=1)[0]
    logger.info(f"Rastgele belirlenen hedef eser türü: {target_medium}")

    museum_client = MuseumAPIClient()
    artwork = None
    
    for attempt in range(1, 4):
        artwork = museum_client.get_random_artwork(posted_data, target_medium)
        if artwork:
            break
        logger.warning(f"Deneme {attempt}/3 başarısız. 5 saniye sonra tekrar deneniyor...")
        import time
        time.sleep(5)

    if not artwork:
        logger.error("Kürasyon döngüsü iptal: 3 denemenin ardından geçerli ve paylaşılmamış bir eser bulunamadı.")
        sys.exit(1)

    logger.info(f"Seçilen Eser: '{artwork.title}' | Sanatçı: {artwork.artist} | Müze: {artwork.museum_name}")
    logger.info(f"Görsel URL: {artwork.image_url}")

    # 3. Tumblr'a Gönder
    try:
        poster = TumblrPoster()
    except ValueError as e:
        logger.error(f"Tumblr istemcisi başlatılamadı: {e}")
        sys.exit(1)

    success = poster.post_artwork(artwork)

    if success:
        # 4. State Güncelle ve Kaydet
        posted_data[artwork.museum].append(artwork.id)
        save_posted_ids(posted_data)
        logger.info("=== Kürasyon döngüsü BAŞARIYLA tamamlandı! ===")
    else:
        logger.error("Tumblr paylaşımı başarısız oldu. ID kaydedilmedi.")
        sys.exit(1)


if __name__ == "__main__":
    run_curation_cycle()
