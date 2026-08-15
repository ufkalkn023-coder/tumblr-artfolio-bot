"""
color_extractor.py - Görselden Renk Paleti Çıkarma Modülü
Pillow kullanarak resimlerdeki en baskın 5 rengi analiz eder ve Hex formatına çevirir.
"""

import logging
import requests
from io import BytesIO
from PIL import Image

logger = logging.getLogger("artfolio_bot.color_extractor")

def get_dominant_colors(image_url: str, num_colors: int = 5) -> list[str]:
    """
    Verilen URL'deki görseli indirip en baskın `num_colors` adet rengi bulur.
    Hex formatında (örn: '#1A1A1A') bir liste döndürür.
    """
    try:
        response = requests.get(image_url, timeout=15)
        response.raise_for_status()
        
        # Görseli hafızaya al
        img = Image.open(BytesIO(response.content))
        
        # Sadece RGB modunda çalıştığından emin ol
        if img.mode != 'RGB':
            img = img.convert('RGB')
            
        # Performans ve daha net kümeleme için görseli küçült (Hızlı analiz)
        img.thumbnail((150, 150))
        
        # Görseli `num_colors` renge kuantize et (kısalt/sıkıştır)
        q_img = img.quantize(colors=num_colors, method=Image.Quantize.MEDIANCUT)
        
        # Her bir rengin kullanım miktarını ve indeksini al
        counts = q_img.getcolors(maxcolors=256)
        
        if not counts:
            logger.warning("Renk çıkarımı başarısız oldu (counts None döndü).")
            return []
            
        # Renkleri piksel sayısına (baskınlığa) göre büyükten küçüğe sırala
        counts.sort(reverse=True, key=lambda x: x[0])
        
        palette = q_img.getpalette()
        
        hex_colors = []
        for count, index in counts[:num_colors]:
            r = palette[index * 3]
            g = palette[index * 3 + 1]
            b = palette[index * 3 + 2]
            hex_colors.append(f"#{r:02x}{g:02x}{b:02x}".upper())
            
        logger.info(f"Baskın renkler başarıyla çıkarıldı: {hex_colors}")
        return hex_colors
        
    except Exception as e:
        logger.error(f"Renk paleti çıkarılırken hata oluştu: {e}")
        return []

def format_palette_html(hex_colors: list[str]) -> str:
    """
    Renk kodlarını Tumblr caption'ına uyumlu estetik bir HTML dizesine çevirir.
    """
    if not hex_colors:
        return ""
        
    # Renk karelerini span içinde renklendiriyoruz ve yanına kodunu ekliyoruz.
    blocks = [
        f'<span style="color: {hex_code}; font-size: 1.2em;">█</span> {hex_code}'
        for hex_code in hex_colors
    ]
    
    # Aralarına boşluk veya ayracı ( | ) koyarak tek bir satır oluşturuyoruz
    palette_line = " &nbsp;&nbsp; ".join(blocks)
    
    return f"<p><b>Palette:</b><br>{palette_line}</p>"
