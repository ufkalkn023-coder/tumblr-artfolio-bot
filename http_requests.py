"""Shared, transparent HTTP request policy for external museum/image endpoints.

Tek sınırlı (bounded) geçici-hata politikası:
    istek → başarı: döndür
         → geçici durum kodu / Timeout / ConnectionError
         → sınırlı exponential backoff + jitter (+ Retry-After varsa onun)
         → denemeler tükenirse son hata yükseltilir (caller karar verir)

Tumblr create_photo gibi idempotent OLMAYAN çağrılar bu yardımcıya bağlanmaz;
şeffaf retry muhtemel double-post üretir (README: Network Resilience).
"""

import math
import random
import time
from urllib.parse import urlsplit, urlunsplit

import requests

BOT_USER_AGENT = "artfolio-bot/1.0 (+https://github.com/ufkalkn023-coder/tumblr-artfolio-bot)"
JSON_REQUEST_HEADERS = {
    "User-Agent": BOT_USER_AGENT,
    "Accept": "application/json",
}
IMAGE_REQUEST_HEADERS = {
    "User-Agent": BOT_USER_AGENT,
    "Accept": "image/*",
}

# Varsayılan geçici durum kümesi. 403 KASIMLI olarak hariçtir: kalıcı bir istemci
# hatasıdır ve yalnızca kaynağa özgü gerekçesi olan uçlarda explicit olarak eklenir.
RETRYABLE_HTTP_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
# AIC barındırması otomatik istemcilere Cloudflare challenge (403) döndürdüğü için
# yalnızca AIC metadata çağrısı 403'ü geçici sayar (kaynağa özgü yapılandırma).
AIC_METADATA_RETRYABLE_STATUS_CODES = RETRYABLE_HTTP_STATUS_CODES | {403}
# Retry-After başlığının anlamlı olduğu durumlar.
RETRY_AFTER_STATUS_CODES = frozenset({429, 503})
# Yalnızca güvenle tekrarlanabilen taşıma hataları; genel `Exception` avcısı YOKTUR.
RETRYABLE_EXCEPTIONS = (requests.Timeout, requests.ConnectionError)

MAX_TRANSIENT_HTTP_ATTEMPTS = 3
RETRY_BASE_DELAY_SECONDS = 0.5
MAX_RETRY_DELAY_SECONDS = 10.0
MAX_RETRY_AFTER_SECONDS = 30.0
JITTER_FRACTION = 0.25


def safe_url_for_logging(value) -> str:
    """Return a URL representation without credential-bearing components."""
    try:
        parsed = urlsplit(str(value))
        if not parsed.scheme or not parsed.hostname:
            return "<invalid-url>"

        hostname = parsed.hostname
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        netloc = f"{hostname}:{parsed.port}" if parsed.port is not None else hostname
        safe_path = "".join(
            character
            for character in parsed.path
            if character >= " " and character != "\x7f"
        )
        return urlunsplit((parsed.scheme.lower(), netloc, safe_path, "", ""))
    except (TypeError, ValueError):
        return "<invalid-url>"


def parse_retry_after(value):
    """Retry-After başlığını saniyeye çevirir.

    Sayısal ve negatif olmayan değer → saniye (MAX_RETRY_AFTER_SECONDS ile sınırlı).
    Bozuk / HTTP-date / negatif / NaN / sonsuz değer → None (normal backoff kullanılır).
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        return None
    if math.isnan(seconds) or math.isinf(seconds) or seconds < 0:
        return None
    return min(seconds, MAX_RETRY_AFTER_SECONDS)


def _transient_delay(attempt, response=None, jitter_fn=None):
    """Deneme gecikmesi: Retry-After (varsa) değilse exponential backoff + jitter."""
    if response is not None and getattr(response, "status_code", None) in RETRY_AFTER_STATUS_CODES:
        headers = getattr(response, "headers", None) or {}
        retry_after = parse_retry_after(headers.get("Retry-After"))
        if retry_after is not None:
            return retry_after
    base = RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
    jitter = (jitter_fn or random.uniform)(0, base * JITTER_FRACTION)
    return min(base + jitter, MAX_RETRY_DELAY_SECONDS)


def request_with_bounded_retry(
    request,
    *,
    endpoint: str,
    logger,
    retryable_status_codes=RETRYABLE_HTTP_STATUS_CODES,
    retryable_exceptions=RETRYABLE_EXCEPTIONS,
    jitter_fn=None,
):
    """Retry yalnızca geçici durumlar ve Timeout/ConnectionError için, kısa ve sınırlı.

    Denemeler tükenince SON yanıt döndürülür (durum kodu için) veya SON istisna
    yükseltilir (taşıma hatası için); sessizce None'a çevrilmez — degrade kararı
    caller katmanına aittir.
    """
    for attempt in range(1, MAX_TRANSIENT_HTTP_ATTEMPTS + 1):
        try:
            response = request()
        except retryable_exceptions as exc:
            if attempt == MAX_TRANSIENT_HTTP_ATTEMPTS:
                logger.error(
                    "http_transient_exhausted endpoint=%s reason=%s attempt=%d/%d",
                    endpoint,
                    type(exc).__name__,
                    attempt,
                    MAX_TRANSIENT_HTTP_ATTEMPTS,
                )
                raise
            delay = _transient_delay(attempt, jitter_fn=jitter_fn)
            logger.warning(
                "http_transient_retry endpoint=%s reason=%s attempt=%d/%d delay=%.2fs",
                endpoint,
                type(exc).__name__,
                attempt,
                MAX_TRANSIENT_HTTP_ATTEMPTS,
                delay,
            )
            time.sleep(delay)
            continue

        status_code = response.status_code
        if status_code not in retryable_status_codes or attempt == MAX_TRANSIENT_HTTP_ATTEMPTS:
            return response

        delay = _transient_delay(attempt, response, jitter_fn)
        logger.warning(
            "http_transient_retry endpoint=%s reason=status status=%s attempt=%d/%d delay=%.2fs",
            endpoint,
            status_code,
            attempt,
            MAX_TRANSIENT_HTTP_ATTEMPTS,
            delay,
        )
        try:
            response.close()
        except Exception:
            pass
        time.sleep(delay)
