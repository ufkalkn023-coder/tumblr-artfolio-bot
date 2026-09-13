"""museum_api.py - GERİYE DÖNÜK UYUMLULUK CEPHESİ (compatibility facade).

Bu modül artık uygulama merkez değildir; yalnızca artfolio paketinden
güncel genel sembolleri yeniden dışa aktarır:

    artfolio/domain/artwork.py      -> Artwork
    artfolio/scoring/scorer.py      -> ArtworkScorer
    artfolio/scoring/telemetry.py   -> ScoringTelemetry, CandidatePoolTelemetry
    artfolio/sources/*.py           -> kaynak adaptörleri (met/aic/cma/smk/harvard)
    artfolio/curation/museum_client.py -> MuseumAPIClient

Mevcut çağıranlar (main.py, testler) bu import'lara dokunmadan çalışmaya
devam eder; yeni kod doğrudan artfolio.* modüllerinden import etmelidir.
Bu modüle YENİ iş mantığı eklenmez.
"""

from artfolio.curation.museum_client import MuseumAPIClient
from artfolio.domain.artwork import Artwork
from artfolio.scoring.scorer import ArtworkScorer
from artfolio.scoring.telemetry import CandidatePoolTelemetry, ScoringTelemetry
from artfolio.sources.aic import (
    AIC_DEFAULT_IIIF_URL,
    AIC_LARGE_PUBLIC_DOMAIN_IMAGE_SIZE,
    AIC_PUBLISHING_ENABLED,  # import-time anlık görüntüsü; patch için kanonik yol: artfolio.sources.aic.AIC_PUBLISHING_ENABLED
    AIC_STANDARD_IMAGE_SIZE,
    build_aic_iiif_image_url,
)

__all__ = [
    "Artwork",
    "ArtworkScorer",
    "MuseumAPIClient",
    "ScoringTelemetry",
    "CandidatePoolTelemetry",
    "AIC_DEFAULT_IIIF_URL",
    "AIC_LARGE_PUBLIC_DOMAIN_IMAGE_SIZE",
    "AIC_PUBLISHING_ENABLED",
    "AIC_STANDARD_IMAGE_SIZE",
    "build_aic_iiif_image_url",
]
