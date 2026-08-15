import os
import tempfile
import requests
from io import BytesIO
from PIL import Image, ImageOps
import logging

logger = logging.getLogger("artfolio_bot.image_processor")

def download_image(url: str) -> str:
    """Görseli indirir ve geçici bir dosyaya kaydeder."""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        
        fd, path = tempfile.mkstemp(suffix=".jpg")
        with os.fdopen(fd, 'wb') as f:
            f.write(response.content)
            
        return path
    except Exception as e:
        logger.error(f"Görsel indirilemedi ({url}): {e}")
        return None

def crop_detail(image_path: str) -> str:
    """Orijinal görselin merkezinden %50'lik (altın oran civarı) bir detay kırpar."""
    try:
        with Image.open(image_path) as img:
            width, height = img.size
            
            # Merkez etrafında %50 kırp
            new_width = width // 2
            new_height = height // 2
            
            left = (width - new_width) // 2
            top = (height - new_height) // 2
            right = left + new_width
            bottom = top + new_height
            
            detail_img = img.crop((left, top, right, bottom))
            
            fd, path = tempfile.mkstemp(suffix="_detail.jpg")
            with os.fdopen(fd, 'wb') as f:
                detail_img.save(f, format="JPEG", quality=90)
                
            return path
    except Exception as e:
        logger.error(f"Detay kırpma başarısız: {e}")
        return None

def add_passepartout(image_path: str, border_size: float = 0.05, color: str = "black") -> str:
    """Görsele estetik bir çerçeve (paspartu) ekler. Border size görselin %'si kadardır."""
    try:
        with Image.open(image_path) as img:
            # En uzun kenara göre çerçeve kalınlığı
            max_dim = max(img.size)
            border_px = int(max_dim * border_size)
            
            framed_img = ImageOps.expand(img, border=border_px, fill=color)
            
            fd, path = tempfile.mkstemp(suffix="_framed.jpg")
            with os.fdopen(fd, 'wb') as f:
                framed_img.save(f, format="JPEG", quality=90)
                
            return path
    except Exception as e:
        logger.error(f"Paspartu ekleme başarısız: {e}")
        return None

def create_wallpaper(image_path: str) -> str:
    """Görseli telefon duvar kağıdı (9:16) formatında kırpar."""
    try:
        with Image.open(image_path) as img:
            width, height = img.size
            target_ratio = 9 / 16.0
            current_ratio = width / height
            
            if current_ratio > target_ratio:
                # Çok geniş -> yanlardan kırp
                new_width = int(height * target_ratio)
                left = (width - new_width) // 2
                right = left + new_width
                top = 0
                bottom = height
            else:
                # Çok uzun -> üst/alttan kırp (veya merkeze al)
                new_height = int(width / target_ratio)
                top = (height - new_height) // 2
                bottom = top + new_height
                left = 0
                right = width
                
            wp_img = img.crop((left, top, right, bottom))
            
            fd, path = tempfile.mkstemp(suffix="_wallpaper.jpg")
            with os.fdopen(fd, 'wb') as f:
                wp_img.save(f, format="JPEG", quality=90)
                
            return path
    except Exception as e:
        logger.error(f"Duvar kağıdı oluşturma başarısız: {e}")
        return None

def create_grid(image_paths: list) -> str:
    """4 görseli (veya bulabildiği kadarını) 2x2 grid olarak birleştirir."""
    if not image_paths:
        return None
        
    try:
        images = [Image.open(p) for p in image_paths[:4]]
        # En küçük boyuta göre kare kırp
        min_dim = min(min(img.size) for img in images)
        
        processed_images = []
        for img in images:
            # Merkezden kare olarak kırp ve yeniden boyutlandır
            w, h = img.size
            if w > h:
                left = (w - h) // 2
                img = img.crop((left, 0, left + h, h))
            elif h > w:
                top = (h - w) // 2
                img = img.crop((0, top, w, top + w))
                
            img = img.resize((500, 500), Image.Resampling.LANCZOS)
            processed_images.append(img)
            
        # 2x2 tuval
        grid = Image.new('RGB', (1000, 1000))
        
        if len(processed_images) > 0: grid.paste(processed_images[0], (0, 0))
        if len(processed_images) > 1: grid.paste(processed_images[1], (500, 0))
        if len(processed_images) > 2: grid.paste(processed_images[2], (0, 500))
        if len(processed_images) > 3: grid.paste(processed_images[3], (500, 500))
        
        fd, path = tempfile.mkstemp(suffix="_grid.jpg")
        with os.fdopen(fd, 'wb') as f:
            grid.save(f, format="JPEG", quality=90)
            
        for img in images:
            img.close()
            
        return path
    except Exception as e:
        logger.error(f"Grid oluşturma başarısız: {e}")
        return None

