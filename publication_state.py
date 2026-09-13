"""
publication_state.py - Yayın Durumu / İşlem Günlüğü (Publication Journal)

posted_ids.json tarihî uyumluluk state'i olarak kalır; bu modülün yönettiği
publication_state.json ise yetkili (authoritative) transaction journal'dır:

    reserved  -> Tumblr çağrısından ÖNCE rezervasyon (TTL içinde bloklar)
    published -> Tumblr paylaşımı doğrulandı (kalıcı blok)
    failed    -> Tumblr açıkça başarısız döndü (tekrar denenebilir)

Kimlik anahtarı daima "museum:artwork_id" biçimindedir; asla başlık veya
sanatçı adı kullanılmaz. Tüm zaman damgaları timezone-aware UTC'dir.
Bozuk state dosyası sessizce sıfırlanmaz: StateCorruptionError ile
yayın durdurulur (fail-closed).
"""

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional

import config
from artfolio.curation.diversity import normalize_artist_name

logger = logging.getLogger("artfolio_bot.publication_state")

SCHEMA_VERSION = 1
STATUS_RESERVED = "reserved"
STATUS_PUBLISHED = "published"
STATUS_FAILED = "failed"
_VALID_STATUSES = {STATUS_RESERVED, STATUS_PUBLISHED, STATUS_FAILED}

_ERROR_TEXT_LIMIT = 300


class StateCorruptionError(RuntimeError):
    """Yayın state dosyası bozuk/okunamaz; yayın güvenli şekilde durdurulmalı."""


@dataclass
class PublicationRecord:
    museum: str
    artwork_id: str
    status: str
    reserved_at: str
    published_at: str = ""
    tumblr_post_id: str = ""
    score: int = 0
    title: str = ""
    artist_name: str = ""
    artist_key: str = ""
    medium_type: str = ""
    error: str = ""


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_utc(value: str) -> datetime:
    """ISO zaman damgasını timezone-aware UTC datetime'a çevirir."""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"naive timestamp is not allowed: {value!r}")
    return parsed.astimezone(timezone.utc)


def _sanitize_error_text(value, limit: int = _ERROR_TEXT_LIMIT) -> str:
    """Hata metinlerini tek satıra indirip kırparak log/journal'a güvenli kılar."""
    text = " ".join(str(value or "").split())
    return text[:limit]


def atomic_write_json(path: Path, payload) -> None:
    """Crash-safe JSON yazımı: aynı dizinde geçici dosya + fsync + os.replace."""
    path = Path(path)
    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
        temp_path = None
    finally:
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


class PublicationStateStore:
    """publication_state.json transaction journal'ının bellek-içi yöneticisi."""

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else config.PUBLICATION_STATE_FILE
        self._records: Dict[str, dict] = {}

    # --- Kimlik ---------------------------------------------------------
    @staticmethod
    def identity_key(museum: str, artwork_id: str) -> str:
        return f"{museum}:{artwork_id}"

    # --- Yükleme / Kaydetme ---------------------------------------------
    def load(self) -> "PublicationStateStore":
        """Journal'ı yükler. Dosya yoksa boş başlar; bozuksa StateCorruptionError fırlatır."""
        if not self.path.exists():
            self._records = {}
            logger.info(
                "publication_state_missing file=%s initialized_empty=true",
                self.path.name,
            )
            return self

        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("unsupported or missing schema_version")
            records = data.get("records")
            if not isinstance(records, dict):
                raise ValueError("records must be an object")
            parsed: Dict[str, dict] = {}
            for key, raw in records.items():
                if not isinstance(raw, dict):
                    raise ValueError(f"record {key!r} must be an object")
                if raw.get("status") not in _VALID_STATUSES:
                    raise ValueError(f"record {key!r} has invalid status")
                if not isinstance(raw.get("museum"), str) or not isinstance(raw.get("artwork_id"), str):
                    raise ValueError(f"record {key!r} missing identity fields")
                if key != self.identity_key(raw["museum"], raw["artwork_id"]):
                    raise ValueError(f"record {key!r} does not match its identity fields")
                if not isinstance(raw.get("reserved_at"), str):
                    raise ValueError(f"record {key!r} missing reserved_at")
                _parse_utc(raw["reserved_at"])  # naive/bozuk zaman damgası reddedilir
                parsed[key] = raw
            self._records = parsed
        except (json.JSONDecodeError, ValueError, TypeError, OSError) as exc:
            logger.error(
                "publication_state_corrupted file=%s error_type=%s",
                self.path.name,
                type(exc).__name__,
            )
            raise StateCorruptionError(
                f"publication state file {self.path.name} is malformed: {type(exc).__name__}"
            ) from exc

        logger.info(
            "publication_state_loaded file=%s records=%d",
            self.path.name,
            len(self._records),
        )
        return self

    def save_atomic(self) -> None:
        """Journal'ı atomik olarak diske yazar."""
        payload = {"schema_version": SCHEMA_VERSION, "records": self._records}
        atomic_write_json(self.path, payload)
        logger.info(
            "publication_state_saved file=%s records=%d",
            self.path.name,
            len(self._records),
        )

    # --- Sorgulama -------------------------------------------------------
    def get_record(self, museum: str, artwork_id: str) -> Optional[PublicationRecord]:
        raw = self._records.get(self.identity_key(museum, artwork_id))
        return PublicationRecord(**raw) if raw else None

    def get_recent_published(self, limit: Optional[int] = None) -> list[PublicationRecord]:
        """Return valid-timestamp published records ordered newest-first.

        Legacy records with absent or malformed publication timestamps remain
        duplicate-blocking journal entries but cannot safely participate in a
        publication-count diversity window, so they are skipped here.
        """
        if limit is not None and limit <= 0:
            return []

        published = []
        for raw in self._records.values():
            if raw.get("status") != STATUS_PUBLISHED:
                continue
            try:
                published_at = _parse_utc(raw.get("published_at", ""))
                record = PublicationRecord(**raw)
            except (TypeError, ValueError):
                logger.warning(
                    "publication_history_skipped source=%s object_id=%s reason=invalid_published_at",
                    raw.get("museum", ""),
                    raw.get("artwork_id", ""),
                )
                continue
            published.append((published_at, record))

        published.sort(key=lambda item: item[0], reverse=True)
        records = [record for _, record in published]
        return records if limit is None else records[:limit]

    def _is_reservation_expired(self, record: dict, now: datetime) -> bool:
        if record.get("status") != STATUS_RESERVED:
            return False
        try:
            reserved_at = _parse_utc(record["reserved_at"])
        except (ValueError, TypeError):
            return True
        ttl_minutes = config.PUBLICATION_RESERVATION_TTL_MINUTES
        return now - reserved_at > timedelta(minutes=ttl_minutes)

    def is_blocked(self, museum: str, artwork_id: str, now: Optional[datetime] = None) -> bool:
        """published -> kalıcı blok; TTL içindeki reserved -> blok; expired reserved ve failed -> serbest."""
        record = self._records.get(self.identity_key(museum, artwork_id))
        if not record:
            return False
        if record["status"] == STATUS_PUBLISHED:
            return True
        if record["status"] == STATUS_RESERVED:
            current = now or datetime.now(timezone.utc)
            if self._is_reservation_expired(record, current):
                logger.warning(
                    "publication_reservation_expired source=%s object_id=%s",
                    museum,
                    artwork_id,
                )
                return False
            return True
        return False  # failed: normal seçim mantığına göre tekrar denenebilir

    def active_blocks(self, now: Optional[datetime] = None) -> Dict[str, list]:
        """Seçim dışında tutulacak ID'ler: {museum: [artwork_id, ...]}."""
        blocked: Dict[str, list] = {}
        for key, record in self._records.items():
            museum = record.get("museum", "")
            artwork_id = record.get("artwork_id", "")
            if not museum or not artwork_id:
                continue
            if self.is_blocked(museum, artwork_id, now=now):
                blocked.setdefault(museum, []).append(artwork_id)
        return blocked

    # --- Yaşam Döngüsü ----------------------------------------------------
    def reserve(self, artwork) -> Optional[PublicationRecord]:
        """Eser için yayın rezervasyonu oluşturur. Aktif blok varsa None döner (fail-closed)."""
        museum = artwork.museum
        artwork_id = artwork.id
        if self.is_blocked(museum, artwork_id):
            logger.warning(
                "publication_reservation_blocked source=%s object_id=%s",
                museum,
                artwork_id,
            )
            return None

        record = PublicationRecord(
            museum=museum,
            artwork_id=str(artwork_id),
            status=STATUS_RESERVED,
            reserved_at=_utc_now_iso(),
            score=int(getattr(artwork, "score", 0) or 0),
            title=str(getattr(artwork, "title", "") or ""),
        )
        self._records[self.identity_key(museum, artwork_id)] = asdict(record)
        logger.info(
            "publication_reservation_created source=%s object_id=%s",
            museum,
            artwork_id,
        )
        return record

    def mark_published(
        self,
        museum: str,
        artwork_id: str,
        tumblr_post_id,
        artwork=None,
    ) -> Optional[PublicationRecord]:
        record = self._records.get(self.identity_key(museum, artwork_id))
        if record is None:
            # Rezervasyonsuz mark_published (savunma amaçlı): kayıt oluştur.
            record = PublicationRecord(
                museum=museum,
                artwork_id=str(artwork_id),
                status=STATUS_RESERVED,
                reserved_at=_utc_now_iso(),
            )
            raw = asdict(record)
            self._records[self.identity_key(museum, artwork_id)] = raw
        now_iso = _utc_now_iso()
        record["status"] = STATUS_PUBLISHED
        record["published_at"] = now_iso
        record["tumblr_post_id"] = str(tumblr_post_id or "")
        if artwork is not None:
            record["artist_name"] = str(getattr(artwork, "artist", "") or "")
            record["artist_key"] = normalize_artist_name(
                getattr(artwork, "artist", "")
            )
            record["medium_type"] = str(
                getattr(artwork, "medium_type", "") or ""
            )
        record["error"] = ""
        logger.info(
            "publication_marked_published source=%s object_id=%s post_id=%s",
            museum,
            artwork_id,
            record["tumblr_post_id"],
        )
        return PublicationRecord(**record)

    def mark_failed(self, museum: str, artwork_id: str, error: str = "") -> Optional[PublicationRecord]:
        record = self._records.get(self.identity_key(museum, artwork_id))
        if record is None:
            logger.warning(
                "publication_marked_failed source=%s object_id=%s reason=no_record",
                museum,
                artwork_id,
            )
            return None
        record["status"] = STATUS_FAILED
        record["error"] = _sanitize_error_text(error)
        logger.info(
            "publication_marked_failed source=%s object_id=%s",
            museum,
            artwork_id,
        )
        return PublicationRecord(**record)

    def mark_uncertain(self, museum: str, artwork_id: str, error: str = "") -> Optional[PublicationRecord]:
        """Sonuç belirsiz (taşıma hatası / beklenmeyen yanıt): istek Tumblr'a ulaşmış olabilir.

        Durum 'reserved' olarak korunur; böylece aynı eser TTL süresi dolana kadar
        yeniden seçilemez ve muhtemel double-post engellenir. TTL dolduğunda kayıt
        doğal olarak tekrar denenebilir olur (kalıcı kilitleme yaratmaz).
        """
        record = self._records.get(self.identity_key(museum, artwork_id))
        if record is None:
            logger.warning(
                "publication_marked_uncertain source=%s object_id=%s reason=no_record",
                museum,
                artwork_id,
            )
            return None
        record["error"] = _sanitize_error_text(error)
        # status bilinçli olarak 'reserved' bırakılır (değiştirilmez).
        logger.warning(
            "publication_marked_uncertain source=%s object_id=%s reason=outcome_unknown reservation_retained=true",
            museum,
            artwork_id,
        )
        return PublicationRecord(**record)
