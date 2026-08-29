"""
main.py - artfolio.db Tumblr Kürasyon Botu Ana Çalıştırıcı
"""

import sys
import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Dict, List

import config
from museum_api import MuseumAPIClient, Artwork
from tumblr_poster import TumblrPoster
import image_processor
from datetime import datetime, timezone

logger = config.setup_logging()


def is_content_type_enabled(content_type: str) -> bool:
    """Return whether a configured content type may be used in production."""
    return config.CONTENT_WEIGHTS.get(content_type, 0) > 0


def get_enabled_content_types() -> List[str]:
    """Return configured content types with a positive production weight."""
    return [content_type for content_type in config.CONTENT_WEIGHTS if is_content_type_enabled(content_type)]


def get_highest_weight_content_type(exclude: str = None) -> str:
    """Return the highest-weight enabled content type, optionally excluding one."""
    candidates = [content_type for content_type in get_enabled_content_types() if content_type != exclude]
    return max(candidates, key=lambda content_type: config.CONTENT_WEIGHTS[content_type]) if candidates else None


def build_curation_attempt_media(initial_medium: str, attempts: int = 3) -> List[str]:
    """Build the deterministic category plan used when curation needs fallback."""
    if not is_content_type_enabled(initial_medium):
        raise ValueError(f"Disabled content type cannot be selected: {initial_medium}")

    fallback_medium = get_highest_weight_content_type(exclude=initial_medium) or initial_medium
    reliability_medium = get_highest_weight_content_type()
    return [initial_medium, fallback_medium, reliability_medium][:attempts]


def apply_scheduled_medium_theme(selected_medium: str, weekday: int) -> str:
    """Apply an enabled weekday theme without reviving disabled content types."""
    themed_medium = {0: "Sculpture", 2: "Drawing"}.get(weekday)
    return themed_medium if themed_medium and is_content_type_enabled(themed_medium) else selected_medium


def export_scoring_telemetry(museum_client, publish_success, output_dir=Path("output/telemetry")):
    """Write run telemetry without allowing export failures to affect publishing."""
    timestamp = datetime.now(timezone.utc)
    output_path = Path(output_dir) / f"scoring-{timestamp.strftime('%Y%m%dT%H%M%SZ')}.json"
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        museum_client.write_scoring_telemetry(
            output_path,
            publish_success=publish_success,
            run_timestamp=timestamp.isoformat(),
        )
        logger.info("telemetry_export_success file=%s", output_path)
    except Exception as exc:
        logger.warning("telemetry_export_failure type=%s", type(exc).__name__)


def load_posted_ids() -> Dict[str, List[str]]:
    """Daha önce paylaşılan eserlerin ID'lerini yükler (Her müze için ayrı liste)."""
    default_structure = {"met": [], "aic": [], "cma": [], "rijksmuseum": [], "smk": [], "harvard": []}
    if not config.POSTED_IDS_FILE.exists():
        logger.info(f"{config.POSTED_IDS_FILE} bulunamadı, yeni oluşturuluyor.")
        save_posted_ids(default_structure)
        return default_structure

    try:
        with open(config.POSTED_IDS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            # Eksik anahtarları tamamla (Geriye dönük uyumluluk)
            for key in default_structure:
                if key not in data:
                    data[key] = []
            return data
    except Exception as e:
        logger.error(f"posted_ids.json okunurken hata: {e}. Varsayılan yapı kullanılacak.")
        return default_structure


def save_posted_ids(data: Dict[str, List[str]]) -> None:
    """Güncellenmiş paylaşılan eser ID listesini posted_ids.json dosyasına yazar."""
    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp(
            dir=config.POSTED_IDS_FILE.parent,
            prefix=f".{config.POSTED_IDS_FILE.name}.",
            suffix=".tmp",
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, config.POSTED_IDS_FILE)
        temp_path = None
        logger.info(f"posted_ids.json başarıyla güncellendi.")
    except Exception as e:
        logger.error("state_save_failure file=%s", config.POSTED_IDS_FILE.name)
        logger.error(f"posted_ids.json kaydedilirken hata: {e}")
        raise
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def run_curation_cycle():
    """Tek bir kürasyon ve paylaşım döngüsünü yürütür."""
    run_started_at = time.monotonic()
    logger.info("=== artfolio.db Tumblr Kürasyon Döngüsü Başlatıldı ===")

    # 1. State Yükle
    posted_data = load_posted_ids()
    total_posted = sum(len(ids) for ids in posted_data.values())
    logger.info(f"Hafızada toplam {total_posted} önceden paylaşılmış eser kayıtlı.")

    # 2. Müzelerden Uygun Eser Çek (Hata payına karşı 3 defa deneme)
    import random
    
    # Hedef tür belirleme (yalnızca etkin yüzdelik oranlara göre)
    enabled_mediums = get_enabled_content_types()
    if not enabled_mediums:
        logger.error("Kürasyon döngüsü iptal: etkin içerik türü yapılandırılmamış.")
        sys.exit(1)
    weights = [config.CONTENT_WEIGHTS[medium] for medium in enabled_mediums]
    target_medium = random.choices(enabled_mediums, weights=weights, k=1)[0]
    
    # Feature 9: Tematik Günler (Zamanlanmış Yayın)
    weekday = datetime.today().weekday()
    target_medium = apply_scheduled_medium_theme(target_medium, weekday)
        
    attempt_media = build_curation_attempt_media(target_medium)
    logger.info(
        "curation_target original_medium=%s weekday=%d attempt_media=%s",
        target_medium,
        weekday,
        ",".join(attempt_media),
    )

    museum_client = MuseumAPIClient()
    artwork = None
    cycle_stats = {
        "source": "none",
        "candidates": 0,
        "duplicates": 0,
        "rejected_image": 0,
        "rejected_quality": 0,
        "eligible": 0,
    }
    
    image_paths = None
    attempted_media = []
    for attempt, attempt_medium in enumerate(attempt_media, start=1):
        fallback_used = attempt_medium != target_medium
        if attempt > 1 and attempt_medium != attempt_media[attempt - 2]:
            logger.info(
                "medium_fallback from=%s to=%s reason=no_eligible_artwork",
                attempt_media[attempt - 2],
                attempt_medium,
            )
        attempted_media.append(attempt_medium)
        logger.info(
            "curation_attempt=%d/3 target_medium=%s fallback=%s",
            attempt,
            attempt_medium,
            str(fallback_used).lower(),
        )
        artwork = museum_client.get_random_artwork(posted_data, attempt_medium)
        selection_stats = getattr(museum_client, "last_run_stats", {})
        for key in ("candidates", "duplicates", "rejected_image", "rejected_quality", "eligible"):
            cycle_stats[key] += selection_stats.get(key, 0)
        logger.info(
            "curation_attempt_result=%d/3 target_medium=%s source=%s selected=%s",
            attempt,
            attempt_medium,
            selection_stats.get("source", "none"),
            getattr(artwork, "id", "none"),
        )
        if artwork:
            cycle_stats["source"] = artwork.museum
            logger.info(f"Seçilen Eser: '{artwork.title}' | Sanatçı: {artwork.artist} | Müze: {artwork.museum_name}")
            logger.info("selected_artwork source=%s object_id=%s title=%r artist=%r score=%s", artwork.museum, artwork.id, artwork.title, artwork.artist, artwork.score)
            logger.info(f"Görsel URL: {artwork.image_url}")
            
            if artwork.image_url:
                main_img = image_processor.download_image(
                    artwork.image_url,
                    aic_iiif=artwork.museum == "aic",
                )
                if main_img:
                    effect = random.choices(["none", "detail", "passepartout", "wallpaper"], weights=[50, 30, 10, 10], k=1)[0]
                    
                    if effect == "detail":
                        detail_img = image_processor.crop_detail(main_img)
                        image_paths = [main_img, detail_img] if detail_img else [main_img]
                        logger.info("Detay kırpma (Photoset) uygulandı.")
                    elif effect == "passepartout":
                        framed_img = image_processor.add_passepartout(main_img)
                        image_paths = [framed_img] if framed_img else [main_img]
                        logger.info("Paspartu çerçeve eklendi.")
                    elif effect == "wallpaper":
                        wp_img = image_processor.create_wallpaper(main_img)
                        image_paths = [wp_img] if wp_img else [main_img]
                        logger.info("Duvar kağıdı formatına dönüştürüldü.")
                    else:
                        image_paths = [main_img]
                    
                    # Görsel başarıyla indirildi ve işlendi, döngüden çık
                    break
                else:
                    cycle_stats["rejected_image"] += 1
                    logger.error("Görsel indirilemediği için bu eser atlanıyor, yeni bir eser aranacak...")
                    artwork = None

        if attempt < len(attempt_media):
            logger.warning(f"Deneme {attempt}/3 başarısız. 5 saniye sonra tekrar deneniyor...")
            time.sleep(5)

    if not artwork:
        export_scoring_telemetry(museum_client, publish_success=False)
        museum_client.log_scoring_telemetry()
        logger.info(
            "run_summary original_medium=%s attempted_media=%s source=none candidates=%d duplicates=%d rejected_quality=%d eligible=%d selected=none published=no duration=%.2fs",
            target_medium, ",".join(attempted_media), cycle_stats["candidates"], cycle_stats["duplicates"], cycle_stats["rejected_quality"],
            cycle_stats["eligible"], time.monotonic() - run_started_at,
        )
        logger.error("Kürasyon döngüsü iptal: 3 denemenin ardından geçerli ve paylaşılmamış bir eser bulunamadı.")
        sys.exit(1)

    # 3. Tumblr'a Gönder
    try:
        poster = TumblrPoster()
    except ValueError as e:
        export_scoring_telemetry(museum_client, publish_success=False)
        museum_client.log_scoring_telemetry()
        logger.error(f"Tumblr istemcisi başlatılamadı: {e}")
        sys.exit(1)

    success = poster.post_artwork(artwork, image_paths=image_paths)

    # Geçici dosyaları temizle
    if image_paths:
        import os
        for p in image_paths:
            try:
                os.remove(p)
            except:
                pass

    if success:
        # 4. State Güncelle ve Kaydet
        logger.info("state_update_start source=%s object_id=%s", artwork.museum, artwork.id)
        posted_data[artwork.museum].append(artwork.id)
        logger.info("state_artwork_recorded source=%s object_id=%s", artwork.museum, artwork.id)
        try:
            save_posted_ids(posted_data)
        except Exception:
            export_scoring_telemetry(museum_client, publish_success=True)
            museum_client.log_scoring_telemetry()
            raise
        logger.info("state_save_success source=%s object_id=%s", artwork.museum, artwork.id)
        export_scoring_telemetry(museum_client, publish_success=True)
        museum_client.log_scoring_telemetry()
        logger.info(
            "run_summary original_medium=%s attempted_media=%s selected_medium=%s source=%s candidates=%d duplicates=%d rejected_quality=%d eligible=%d selected=%s published=yes duration=%.2fs",
            target_medium, ",".join(attempted_media), artwork.medium_type, cycle_stats["source"], cycle_stats["candidates"], cycle_stats["duplicates"],
            cycle_stats["rejected_quality"], cycle_stats["eligible"], artwork.id,
            time.monotonic() - run_started_at,
        )
        logger.info("=== Kürasyon döngüsü BAŞARIYLA tamamlandı! ===")
    else:
        export_scoring_telemetry(museum_client, publish_success=False)
        museum_client.log_scoring_telemetry()
        logger.info(
            "run_summary original_medium=%s attempted_media=%s selected_medium=%s source=%s candidates=%d duplicates=%d rejected_quality=%d eligible=%d selected=%s published=no duration=%.2fs",
            target_medium, ",".join(attempted_media), artwork.medium_type, cycle_stats["source"], cycle_stats["candidates"], cycle_stats["duplicates"],
            cycle_stats["rejected_quality"], cycle_stats["eligible"], artwork.id,
            time.monotonic() - run_started_at,
        )
        logger.error("Tumblr paylaşımı başarısız oldu. ID kaydedilmedi.")
        sys.exit(1)


if __name__ == "__main__":
    run_curation_cycle()
