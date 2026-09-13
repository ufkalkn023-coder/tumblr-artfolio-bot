"""Müze kaynak adaptörleri.

Rijksmuseum: Yalnızca yapılandırma/state iskeleti mevcuttur (config.RIJKSMUSEUM_API_KEY
ve posted_ids yapısındaki boş "rijksmuseum" anahtarı); hiçbir fetcher uygulanmamıştır.
Sağlayıcının eser bazlı hak metadata'sı güvenilir doğrulanamadığı için uyarlama
yapılmamıştır — uydurma bir implementasyon doldurmak için eklenmez.
"""

from .aic import AIC_PUBLISHING_ENABLED, AICSource, build_aic_iiif_image_url
from .base import HTTP_TIMEOUT, MuseumSource
from .cma import CMASource
from .harvard import HarvardSource
from .met import MetSource
from .smk import SMKSource

__all__ = [
    "MuseumSource",
    "MetSource",
    "AICSource",
    "CMASource",
    "SMKSource",
    "HarvardSource",
    "AIC_PUBLISHING_ENABLED",
    "build_aic_iiif_image_url",
    "HTTP_TIMEOUT",
]
