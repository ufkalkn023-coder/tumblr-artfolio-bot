"""Shared, transparent HTTP request policy for external museum endpoints."""

import time


BOT_USER_AGENT = "artfolio-bot/1.0 (+https://github.com/ufkalkn023-coder/tumblr-artfolio-bot)"
JSON_REQUEST_HEADERS = {
    "User-Agent": BOT_USER_AGENT,
    "Accept": "application/json",
}
IMAGE_REQUEST_HEADERS = {
    "User-Agent": BOT_USER_AGENT,
    "Accept": "image/*",
}
RETRYABLE_HTTP_STATUS_CODES = frozenset({403, 429, 500, 502, 503, 504})
MAX_TRANSIENT_HTTP_ATTEMPTS = 3


def request_with_bounded_retry(
    request,
    *,
    endpoint: str,
    logger,
    retryable_status_codes=RETRYABLE_HTTP_STATUS_CODES,
):
    """Retry only transient HTTP statuses with a short, bounded backoff."""
    for attempt in range(1, MAX_TRANSIENT_HTTP_ATTEMPTS + 1):
        response = request()
        status_code = response.status_code
        if (
            status_code not in retryable_status_codes
            or attempt == MAX_TRANSIENT_HTTP_ATTEMPTS
        ):
            return response

        delay = 0.25 * (2 ** (attempt - 1))
        logger.warning(
            "http_transient_retry endpoint=%s status=%s attempt=%d/%d delay=%.2fs",
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
