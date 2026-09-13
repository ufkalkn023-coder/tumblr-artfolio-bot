"""Normalize edilmiş eser alan modeli. Sağlayıcıya özgü API ayrıştırma BURADA OLMAZ."""

from dataclasses import dataclass
from typing import Optional


@dataclass
class Artwork:
    """Paylaşılacak sanat eseri veri yapısı."""
    museum: str           # 'met' | 'aic' | 'cma'
    id: str               # Müze içi benzersiz ID
    title: str            # Eser adı
    artist: str           # Sanatçı adı
    artist_bio: str       # Sanatçı künyesi (Milliyet, Doğum-Ölüm Yılı)
    date: str             # Yapım yılı / dönemi
    image_url: str        # Yüksek çözünürlüklü görsel bağlantısı
    original_source_url: str # Orijinal müze bağlantısı
    museum_name: str      # Tam müze adı
    location_info: str    # Müze içi oda/galeri lokasyonu
    dimensions: str       # Fiziksel boyutlar
    medium_type: str      # 'Painting', 'Sculpture', 'Drawing', 'Object'
    raw_medium: str       # Ham teknik/malzeme bilgisi (örn: "Oil on canvas", "Bronze", "Marble")
    score: int            # 0 - 100 arası kalite puanı
    style_or_era: Optional[str] = None  # Dönem veya akım bilgisi
    alt_text: str = ""    # Görme engelliler ve SEO için alternatif metin
    # Yayın hakları (fail-closed): Yalnızca sağlayıcının eser bazlı metadata'sı ile
    # doğrulanmış kamu malı eserler yayınlanabilir. Bilinmeyen haklar = yayınlanamaz.
    is_public_domain: bool = False
    license_name: str = ""
    license_url: str = ""
    rights_statement: str = ""

    @property
    def is_publishable(self) -> bool:
        """Yayın izni yalnızca sağlayıcı metadata'sıyla doğrulanmış kamu malı eserlere verilir."""
        return self.is_public_domain is True
