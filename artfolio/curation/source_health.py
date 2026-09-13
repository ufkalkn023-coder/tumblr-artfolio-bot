"""Kaynak sağlık takibi ve devre kesici (circuit breaker) — işlem başına state.

Amaç: tek bir koşu içinde operasyonel olarak arızalı bir kaynağın (timeout,
bağlantı hatası, bozuk yanıt) kalan denemeleri tekrar tekrar yavaşlatmasını
engellemek. İÇERİK sonuçları (boş arama, kalite/hak/duplicate reddi, hedef tür
uyuşmazlığı) arıza DEĞİLDİR ve devreyi açmaz.

Durum makinesi:
    CLOSED   -> normal çalışma
    OPEN     -> ardışık arıza eşiği aşıldı; cooldown bitene kadar kaynak atlanır
    HALF_OPEN-> cooldown doldu; tek kontrollü denemeye izin verilir

State tamamen işlem/içerik yaşam döngüsündedir: kalıcılaştırılmaz, run'lar
arası taşınmaz, yeni MuseumAPIClient taze state ile başlar.
"""

import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, Optional

import config

# Curation katmanı log kimliği: mevcut üretim log akışıyla aynı namespace.
logger = logging.getLogger("artfolio_bot.museum_api")

_URL_PATTERN = re.compile(r"https?://\S+")
_REASON_LIMIT = 120


def _sanitize_reason(reason) -> str:
    """Arıza nedenini güvenli etikete indirger: URL'ler kırpılır, tek satır, sınırlı uzunluk."""
    text = _URL_PATTERN.sub("<url>", str(reason or ""))
    return " ".join(text.split())[:_REASON_LIMIT]


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class SourceHealth:
    source_id: str
    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    total_failures: int = 0
    total_successes: int = 0
    circuit_skips: int = 0
    opened_at: Optional[float] = None
    last_failure_reason: str = ""


class SourceHealthManager:
    """Kaynak başına devre kesici durumu (tek process, tek koşu).

    clock enjekte edilebilir (time.monotonic varsayılan) → testler deterministik.
    """

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        failure_threshold: int = None,
        cooldown_seconds: int = None,
    ):
        self.clock = clock
        self.failure_threshold = (
            failure_threshold
            if failure_threshold is not None
            else config.SOURCE_CIRCUIT_FAILURE_THRESHOLD
        )
        self.cooldown_seconds = (
            cooldown_seconds
            if cooldown_seconds is not None
            else config.SOURCE_CIRCUIT_COOLDOWN_SECONDS
        )
        self._health: Dict[str, SourceHealth] = {}

    # --- dahili -----------------------------------------------------------
    def _ensure(self, source_id: str) -> SourceHealth:
        if source_id not in self._health:
            self._health[source_id] = SourceHealth(source_id=source_id)
        return self._health[source_id]

    # --- sorgu --------------------------------------------------------------
    def can_attempt(self, source_id: str) -> bool:
        """Kaynağa deneme yapılabilir mi? OPEN kaynak cooldown bitene dek atlanır."""
        health = self._health.get(source_id)
        if health is None or health.state == CircuitState.CLOSED:
            return True

        if health.state == CircuitState.OPEN:
            elapsed = self.clock() - (health.opened_at or 0.0)
            if elapsed >= self.cooldown_seconds:
                health.state = CircuitState.HALF_OPEN
                logger.info("source_circuit_half_open source=%s", source_id)
                return True
            health.circuit_skips += 1
            logger.warning("source_circuit_skipped source=%s state=open", source_id)
            return False

        return True  # HALF_OPEN: tek kontrollü denemeye izin verilir

    # --- sonuç kayıtları -----------------------------------------------------
    def record_success(self, source_id: str):
        """Operasyonel başarılı etkileşim: ardışık arıza serisi sıfırlanır."""
        health = self._ensure(source_id)
        health.total_successes += 1
        if health.state != CircuitState.CLOSED:
            health.state = CircuitState.CLOSED
            health.opened_at = None
            logger.info("source_circuit_closed source=%s", source_id)
        health.consecutive_failures = 0

    def record_failure(self, source_id: str, reason: str = ""):
        """Operasyonel arıza; eşiği aşan ardışık seri devreyi açar."""
        health = self._ensure(source_id)
        health.total_failures += 1
        health.consecutive_failures += 1
        # URL'ler (apikey gibi sorgu parametreleri taşıyabilir) ve satır sonları temizlenir.
        health.last_failure_reason = _sanitize_reason(reason)

        if health.state == CircuitState.HALF_OPEN:
            # Kontrollü deneme de arızalı: devre yeniden açılır, cooldown yeniden başlar.
            health.state = CircuitState.OPEN
            health.opened_at = self.clock()
            logger.warning(
                "source_circuit_reopened source=%s reason=%s",
                source_id, health.last_failure_reason,
            )
            return

        # NOT: durum zaten OPEN iken gelen ek record_failure çağrıları cooldown'u
        # yeniler. Üretim akışında ulaşılamazdır (her fetch can_attempt kapısından
        # geçer); yalnızca doğrudan manager kullanımında mümkündür.

        if health.consecutive_failures >= self.failure_threshold:
            health.state = CircuitState.OPEN
            health.opened_at = self.clock()
            logger.warning(
                "source_circuit_opened source=%s failures=%d reason=%s",
                source_id, health.consecutive_failures, health.last_failure_reason,
            )

    # --- raporlama -------------------------------------------------------------
    def snapshot(self) -> Dict[str, dict]:
        """Serileştirilebilir sağlık raporu (izleme için; kalıcılık yok)."""
        return {
            source_id: {
                "state": health.state.value,
                "consecutive_failures": health.consecutive_failures,
                "total_failures": health.total_failures,
                "total_successes": health.total_successes,
                "circuit_skips": health.circuit_skips,
                "last_failure_reason": health.last_failure_reason,
            }
            for source_id, health in sorted(self._health.items())
        }

    def log_summary(self):
        """Koşu sonunda kısa, yapılandırılmış sağlık özeti."""
        if not self._health:
            return
        for source_id, health in sorted(self._health.items()):
            logger.info(
                "source_health_summary source=%s state=%s successes=%d failures=%d circuit_skips=%d",
                source_id, health.state.value, health.total_successes,
                health.total_failures, health.circuit_skips,
            )
