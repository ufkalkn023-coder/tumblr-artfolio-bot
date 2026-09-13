"""Kalite puanlama motoru. Ağırlıklar ve sınıflandırma semantiği değişmez."""

from typing import Tuple

# Dünya çapında tanınan usta sanatçılar listesi (Puanlama bonusu için)
FAMOUS_ARTISTS = {
    "da vinci", "leonardo", "michelangelo", "raphael", "rembrandt", "caravaggio",
    "bernini", "rodin", "donatello", "canova", "vermeer", "monet", "van gogh",
    "renoir", "degas", "durer", "velazquez", "goya", "turner", "rubens",
    "titian", "botticelli", "cezanne", "klimt", "munch", "el greco", "hokusai",
    "courbet", "delacroix", "ingres", "david", "bruegel", "bosch", "giotto",
    "manet", "gauguin", "seurat", "whistler", "sargent", "morisot", "cassatt",
    "tintoretto", "veronese", "holbein", "van eyck", "friedrich", "constable"
}

# Değersiz / düşük kaliteli arkeolojik parçaları engellemek için anahtar kelimeler
FRAGMENT_KEYWORDS = ["fragment", "shard", "sherd", "nail", "splinter", "scrap", "bead", "coin", "sample", "strip", "specimen"]


class ArtworkScorer:
    """
    Sanat eserlerini estetik, çözünürlük, sanatçı değeri ve müze öne çıkarmasına göre
    0-100 arasında puanlayan kalite değerlendirme motoru.
    """

    @staticmethod
    def classify_medium(raw_medium: str, classification: str, object_name: str) -> str:
        """Eserin türünü belirler: Painting, Sculpture, Drawing veya Object."""
        text = f"{raw_medium} {classification} {object_name}".lower()

        if any(w in text for w in ["painting", "oil on canvas", "tempera", "fresco", "acrylic", "panel", "maleri", "olie på lærred"]):
            return "Painting"
        if any(w in text for w in ["sculpture", "statue", "marble", "bronze", "terracotta", "bust", "relief", "alabaster", "skulptur"]):
            return "Sculpture"
        if any(w in text for w in ["drawing", "ink on paper", "chalk", "charcoal", "pastel", "etching", "engraving", "woodcut", "watercolor", "print", "tegning", "grafik", "træsnit", "radering"]):
            return "Drawing"
        return "Object"

    @classmethod
    def calculate_score(
        cls,
        title: str,
        artist: str,
        date_str: str,
        raw_medium: str,
        classification: str,
        object_name: str,
        image_url: str,
        is_highlight: bool = False,
        has_additional_images: bool = False,
        on_view: bool = False
    ) -> Tuple[int, str]:
        """
        Eser için 0-100 arası toplam puan ve detaylı puanlama özeti hesaplar.
        """
        score = 0
        reasons = []

        title_lower = title.lower()
        artist_lower = artist.lower()
        medium_lower = f"{raw_medium} {classification} {object_name}".lower()

        # 0. Anti-Filtre (Kırık/Değersiz parça, madeni para veya önemsiz fragmanları anında düşür)
        if any(w in title_lower or w in object_name.lower() for w in FRAGMENT_KEYWORDS):
            return 20, "Fragment/Küçük parça elendi"

        if not image_url or not image_url.startswith("http"):
            return 0, "Görsel URL eksik"

        # 1. Müze Öne Çıkarması ve Galeri Durumu (Maks 25 Puan)
        if is_highlight:
            score += 20
            reasons.append("MuseumHighlight(+20)")
        if on_view:
            score += 5
            reasons.append("OnViewInGallery(+5)")

        # 2. Sanatçı ve Atıf Gücü (Maks 25 Puan)
        is_unknown = any(w in artist_lower for w in ["unknown", "anonymous", "unidentified", "various", "maker unknown"])
        if not is_unknown and len(artist.strip()) > 3:
            score += 15
            reasons.append("KnownArtist(+15)")
            # Ünlü Usta Sanatçı Bonusu
            if any(famous in artist_lower for famous in FAMOUS_ARTISTS):
                score += 10
                reasons.append("FamousMasterBonus(+10)")
        elif is_highlight:
            # Anonim ama müzenin en değerli başyapıtıysa (örn: Antik Yunan / Mısır heykelleri)
            score += 10
            reasons.append("MasterpieceAntique(+10)")

        # 3. Görsel Kalitesi ve Çoklu Açı (Maks 25 Puan)
        if image_url:
            score += 20
            reasons.append("HighResImage(+20)")
        if has_additional_images:
            score += 5
            reasons.append("MultiView(+5)")

        # 4. Tür ve Malzeme Kalitesi (Maks 20 Puan)
        medium_type = cls.classify_medium(raw_medium, classification, object_name)
        if medium_type == "Painting":
            score += 20
            reasons.append("MasterPainting(+20)")
        elif medium_type == "Sculpture":
            if any(w in medium_lower for w in ["marble", "bronze", "terracotta", "limestone", "alabaster"]):
                score += 20
                reasons.append("ClassicSculpture(+20)")
            else:
                score += 15
                reasons.append("Sculpture(+15)")
        elif medium_type == "Drawing":
            if not is_unknown:
                score += 18
                reasons.append("MasterDrawing(+18)")
            else:
                score += 10
                reasons.append("Drawing(+10)")
        else: # Object / Decorative Arts
            if any(w in medium_lower for w in ["gold", "silver", "tapestry", "porcelain", "enamel", "ivory", "mosaic"]):
                score += 18
                reasons.append("PreciousArtifact(+18)")
            else:
                score += 12
                reasons.append("HistoricalObject(+12)")

        # 5. Başlık ve Tarih Bütünlüğü (Maks 5 Puan)
        if title and title.lower() not in ["untitled", "sans titre", "unknown"] and len(title) > 3:
            if date_str and date_str.lower() not in ["unknown date", "n.d."]:
                score += 5
                reasons.append("CompleteMetadata(+5)")

        # Skor sınırlandırması: 0-100
        score = max(0, min(100, score))
        summary = f"Score: {score}/100 [{', '.join(reasons)}]"
        return score, summary

