# 🎨 artfolio.db - Otomatik Tumblr Sanat Kürasyon Botu

GitHub Actions üzerinde sıfır maliyetle çalışan, günde 48 adet (her 30 dakikada bir) **The Metropolitan Museum of Art (The Met)**, **Art Institute of Chicago (AIC)** ve **Cleveland Museum of Art (CMA)** açık erişim koleksiyonlarından **85/100 kalite puanı ve üzerindeki** yüksek çözünürlüklü klasik resim sanatı (paintings), usta heykelleri (sculptures), çizimleri (drawings) ve değerli tarihi objeleri derleyip Tumblr'da paylaşan tam otomatik bot.

---

## 🌟 Temel Özellikler

- **Müze API Entegrasyonları:** The Met, AIC ve CMA Açık Erişim API'leri.
- **85/100 Kalite Puanlama Sistemi (ArtworkScorer):**
  - Çözünürlük ve görsel netliği (Maks 25 puan)
  - Müze öne çıkarması (Highlight, On-View galeride sergilenme) (Maks 25 puan)
  - Sanatçı değeri ve dünya çapında usta sanatçı bonusu (Maks 25 puan)
  - Tür & malzeme kalitesi (Yağlı boya, mermer, bronz, altın, usta eskiz) (Maks 20 puan)
  - Başlık & tarih bütünlüğü (Maks 5 puan)
  - *Kırık/değersiz arkeolojik fragmanlar, çizikler, madeni paralar otomatik olarak elenir.*
- **Otomatik Renk Paleti Çıkarımı (Color Palette):** `Pillow` kullanılarak her eserin en baskın 5 rengi analiz edilir ve açıklama kısmına çok şık bir renk bloğu / hex kodu paleti olarak (`Palette: █ #HEX`) eklenir. Tasarımcılar ve moodboard kitleleri için viral/reblog etkisini artırır.
- **Çoklu Sanat Türü Desteği:** Resimler (`Painting`), Heykeller (`Sculpture`), Usta Çizimleri (`Drawing`) ve Değerli Objeler (`Object`).
- **Tekrar Önleme (State Management):** Paylaşılan eserlerin ID'leri `posted_ids.json` dosyasında tutulur ve GitHub Actions her paylaşımdan sonra bu dosyayı depoya otomatik commit/push eder.
- **Tumblr SEO:** Türüne göre optimize edilmiş **tam 5 adet** hedeflenmiş etiket eklenir (örn: `#art`, `#classical art`, `#sculpture`, `#museum`, `#classical sculpture`).
- **Çapraz Yönlendirme:** Her paylaşımın altında sabit `Follow on Instagram for more: @artfolio.db` imzası yer alır.
- **0 Maliyet:** Tamamen GitHub Actions cron iş akışı ile bulutta sunucusuz çalışır.

---

## 📁 Proje Dosya Yapısı

```
.
├── .github/
│   └── workflows/
│       └── tumblr.yml       # Her 30 dakikada bir çalışan GitHub Actions Cron
├── .env.example             # Yerel testler için örnek ortam değişkenleri
├── config.py                # Konfigürasyon ve sabitler
├── museum_api.py            # The Met, AIC, CMA API istemcisi ve 85/100 Puanlayıcı
├── tumblr_poster.py         # Tumblr API paylaşım ve dinamik 5 SEO etiket motoru
├── main.py                  # Ana orkestratör
├── posted_ids.json          # Paylaşılan eser ID kayıtları
├── requirements.txt         # Gerekli Python kütüphaneleri
├── test_bot.py              # Birim ve entegrasyon testleri
└── README.md                # Kurulum ve kullanım kılavuzu
```

---

## 🔑 Tumblr API Anahtarlarını Alma

1. [Tumblr Applications](https://www.tumblr.com/oauth/apps) sayfasına gidin ve **"Register an Application"** butonuna tıklayın.
2. Uygulama oluşturduktan sonra size verilen `OAuth Consumer Key` ve `OAuth Consumer Secret` bilgilerini alın.
3. Kullanıcı yetkilendirmesi ile `OAuth Token` ve `OAuth Token Secret` değerlerini elde edin.

---

## ⚙️ GitHub Secrets Yapılandırması

Projeyi GitHub'a yükledikten sonra:
1. GitHub Deponuz > **Settings** > **Secrets and variables** > **Actions** sayfasına gidin.
2. **New repository secret** butonuna tıklayarak aşağıdaki 5 gizli anahtarı ekleyin:

| Secret Adı | Açıklama |
| :--- | :--- |
| `TUMBLR_CONSUMER_KEY` | Tumblr OAuth Consumer Key |
| `TUMBLR_CONSUMER_SECRET` | Tumblr OAuth Consumer Secret |
| `TUMBLR_OAUTH_TOKEN` | Tumblr OAuth Token |
| `TUMBLR_OAUTH_SECRET` | Tumblr OAuth Token Secret |
| `TUMBLR_BLOG_NAME` | Paylaşım yapılacak blog adı (örn: `artfolio-db.tumblr.com`) |

3. **Workflow İzinleri:**
   - **Settings** > **Actions** > **General** > **Workflow permissions** altında **"Read and write permissions"** seçeneğini işaretleyin.
