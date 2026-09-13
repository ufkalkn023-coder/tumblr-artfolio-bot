# 🎨 artfolio.db - Otomatik Tumblr Sanat Kürasyon Botu

GitHub Actions üzerinde sıfır maliyetle çalışan ve **saatte bir (günde en fazla 24 yayın denemesi)** tetiklenen bot; The Metropolitan Museum of Art (The Met), Art Institute of Chicago (AIC), Cleveland Museum of Art (CMA), SMK ve Harvard açık koleksiyonlarından **80/100 kalite puanı ve üzerindeki**, eser bazında yayın hakkı doğrulanmış klasik sanat eserlerini derleyip Tumblr'da paylaşır.

---

## 🌟 Temel Özellikler

- **Müze API Entegrasyonları:** The Met, AIC, CMA, SMK ve Harvard adaptörleri. AIC üretimde devre dışıdır; Harvard adayları eser bazlı kamu malı bildirimi bulunmadığı için fail-closed reddedilir.
- **80/100 Kalite Puanlama Sistemi (ArtworkScorer):**
  - Çözünürlük ve görsel netliği (Maks 25 puan)
  - Müze öne çıkarması (Highlight, On-View galeride sergilenme) (Maks 25 puan)
  - Sanatçı değeri ve dünya çapında usta sanatçı bonusu (Maks 25 puan)
  - Tür & malzeme kalitesi (Yağlı boya, mermer, bronz, altın, usta eskiz) (Maks 20 puan)
  - Başlık & tarih bütünlüğü (Maks 5 puan)
  - *Kırık/değersiz arkeolojik fragmanlar, çizikler, madeni paralar otomatik olarak elenir.*
- **Çoklu Sanat Türü Desteği:** Resimler (`Painting`), Heykeller (`Sculpture`), Usta Çizimleri (`Drawing`) ve Değerli Objeler (`Object`).
- **Tekrar Önleme (State Management):** Paylaşılan eserlerin ID'leri `posted_ids.json` dosyasında tutulur ve GitHub Actions her paylaşımdan sonra bu dosyayı depoya otomatik commit/push eder.
- **Tumblr SEO:** Türüne göre optimize edilmiş **tam 5 adet** hedeflenmiş etiket eklenir (örn: `#art`, `#classical art`, `#sculpture`, `#museum`, `#classical sculpture`).
- **Yayın Günlüğü:** Tumblr çağrısından önce kalıcı rezervasyon oluşturan transaction journal, başarısız ve belirsiz sonuçları ayrı ele alır.
- **0 Maliyet:** Tamamen GitHub Actions cron iş akışı ile bulutta sunucusuz çalışır.

---

## 🔐 Publishing Rights Safety (Yayın Hakları Güvenliği)

Bot, yalnızca yayın hakları sağlayıcının **eser bazlı metadata'sı ile doğrulanmış** eserleri paylaşır. Bu katman kasıtlı olarak muhafazakârdır (fail-closed):

- **Eser bazlı normalizasyon:** Her müze kaynağından gelen hak bilgisi (`is_public_domain`, `license_name`, `license_url`, `rights_statement`) normalize edilmiş `Artwork` modeline taşınır.
- **Bilinmeyen haklar fail-closed'dur:** Hak durumu doğrulanamayan veya belirsiz eserler **asla yayınlanmaz**.
- **Seçim sırasında kontrol:** Müze API seçim katmanı, yayınlanamayan eseri reddeder (`rights_rejection` log'u; hak reddi kalite reddinden ayrı sayılır: `rejected_rights`).
- **TumblrPoster son kontrol:** Tumblr'a göndermeden hemen önce ikinci ve zorunlu bir hak kontrolü yapılır (`tumblr_publish_blocked ... reason=rights_not_publishable`). Seçim katmanı atlanırsa bile bu kapı yayın isteği üretmez (defense in depth).
- **Görsel varlığı izin anlamına gelmez:** Eserin görsel URL'sinin, yüksek çözünürlüklü görselinin veya yüksek kalite puanının olması yayın izni sağlamaz. Kalite puanı hak kısıtlamasını geçersiz kılamaz.
- **Sağlayıcı metadata belirleyicidir:** Müze adına, koleksiyon itibarına, eserin yaşına veya sanatçının ölüm tarihine dayanarak kamu malı çıkarımı **yapılmaz**.

### Kaynak bazlı hak kararı tablosu

| Kaynak | Kullanılan metadata | Yayınlanabilir koşulu | Durum |
|---|---|---|---|
| The Met | `isPublicDomain` (eser bazlı) + `rightsAndReproduction` | Objede `isPublicDomain: true` | ✅ Doğrulanmış |
| AIC | `is_public_domain` (eser bazlı) | Obje alanı `true` | ✅ Doğrulanmış (kaynak şu an IIIF sorunundan dolayı devre dışı; normalize edilen değer seçim kapısında denenir) |
| CMA | `share_license_status` (eser bazlı) | `share_license_status == "CC0"` | ✅ Doğrulanmış |
| SMK | `public_domain` + `license_text` (eser bazlı) | Obje alanı `public_domain: true` | ✅ Doğrulanmış (alan eksikse fail-closed) |
| Harvard | — | — | ⛔ **Tamamen fail-closed**: API eser bazlı CC0/kamu malı bildirimi içermiyor (`imagepermissionlevel` yalnızca görüntüleme izni verir); tüm Harvard adayları fetch aşamasında hak reddi olarak elenir |
| Rijksmuseum | — | — | ⛔ Uygulanmamış (anahtar yapılandırmada mevcut, fetcher yok) |

İstatistik notu: fetch ve run özetlerindeki `eligible` sayacı "kalite eşiğini geçen aday" anlamındadır; hak reddi (`rejected_rights`) her zaman ayrı bir sayaçtır ve kalite reddine karışmaz.

---

## 🗂️ Publication State & Duplicate Protection

Bot, aynı eserin iki kez paylaşılmasını önlemek için iki katmanlı bir durum yönetimi kullanır:

- **`posted_ids.json` (legacy):** Tarihî uyumluluk state'i olarak geçerlidir; silinmez ve geçmiş kayıtlar seçimde dışlama listesine aynen devam eder.
- **`publication_state.json` (transaction journal):** Yetkili yayın günlüğüdür. Kimlik anahtarı daima `museum:artwork_id`'dir (asla başlık/sanatçı değil). Kayıt durumları: `reserved`, `published`, `failed`.

### Yayın akışı

```text
seçim (legacy + journal blokları hariç)
  ↓
rezervasyon oluşturulur (reserved)
  ↓
journal diske atomik yazılır  ← yazılamazsa Tumblr HİÇ çağrılmaz
  ↓
Tumblr publish çağrısı
  ↓
başarı → journal published + Tumblr post ID kaydedilir → posted_ids.json güncellenir
kesin başarısızlık → journal failed → posted_ids.json DEĞİŞMEZ
belirsiz sonuç (transport hatası / beklenmeyen yanıt) → kayıt 'reserved' KALIR → TTL doğal olarak dolar
```

### Kural ayrıntıları

- **Yayından önce rezervasyon:** Tumblr çağrısından önce `reserved` kaydı kalıcılaştırılır; süreç o noktada çökerse bir sonraki çalıştırma bu eseri TTL süresince tekrar seçemez (crash sonrası mükerrer yayına karşı koruma).
- **TTL:** Süresi dolan (`PUBLICATION_RESERVATION_TTL_MINUTES`, varsayılan 120 dk) `reserved` kayıtları tekrar denenebilir olur ve `publication_reservation_expired` log'u üretir.
- **Atomik yazım:** Her iki state dosyası da geçici dosya + `fsync` + `os.replace` ile yazılır; bozuk/geçersiz state dosyası **sessizce sıfırlanmaz** — bot fail-closed olarak yayını durdurur.
- **Başarılı yayınlar** Tumblr post ID'sini journal'da saklar; `published` kaydı kalıcı olarak bloklar.
- **`failed` kayıtları** normal seçim mantığına göre tekrar denenebilir.
- **Belirsiz sonuçlar (outcome unknown):** Tumblr açıkça başarısız döndüyse kayıt `failed` olur. Ancak taşıma hatası (`exception:*`) veya beklenmeyen yanıt durumunda isteğin Tumblr'da işlenip işlenmediği bilinemez; bu durumda kayıt bilinçli olarak `reserved` bırakılır (`publication_marked_uncertain` log'u), böylece aynı eser TTL süresi dolana kadar yeniden seçilmez ve muhtemelen canlı olan postun mükerrer yayını engellenir. TTL dolduğunda kayıt doğal olarak tekrar denenebilir olur — kalıcı kilitleme oluşmaz.
- **Tam exactly-once garantisi YOKTUR:** Tumblr tarafında idempotency anahtarı / sunucu tarafı transaction desteği bulunmadığı için, ağ/taşıma hatası sonrası (i) post canlı olabilir ve (ii) TTL dolduktan sonra aynı eser tekrar seçilirse mükerrer yayın teorik olarak mümkündür. Bu belirsizlik penceresi kütüphane düzeyinde kapatılamaz; journal, bu pencereyi bilinen ve loglanan tek durumla sınırlar.

---

## Feed Diversity

Başarılı yayın geçmişi, uygun adaylar arasından seçim yaparken art arda benzer içerik gelmesini sınırlar:

- **Sanatçı bekleme penceresi:** `ARTIST_COOLDOWN_POSTS` (varsayılan `12`) en yeni N başarılı yayın içinde bulunan bir sanatçıyı geçici olarak dışlar; `0` bu kontrolü kapatır. Sanatçı adları yalnızca Unicode normalizasyonu, boşluk birleştirme ve büyük/küçük harf katlama ile eşleştirilir. `Unknown`, `Anonymous`, `Unknown Artist` ve `Unidentified` değerleri sanatçı kimliği oluşturmaz; dolayısıyla bilinmeyen sanatçılar tek bir ortak bekleme kovasına girmez.
- **Kaynak serisi sınırı:** `SOURCE_MAX_CONSECUTIVE_POSTS` (varsayılan `3`) aynı müzeden dördüncü ardışık yayını engeller.
- **Tür serisi sınırı:** `MEDIUM_MAX_CONSECUTIVE_POSTS` (varsayılan `3`) aynı içerik türünden dördüncü ardışık yayını engeller.
- Kurallar yalnızca `published` journal kayıtlarını, yayın zamanına göre en yeniden eskiye doğru değerlendirir. `reserved`, `failed`, belirsiz ve bozuk tarihli kayıtlar geçmişe dahil edilmez.
- Eski schema-v1 journal kayıtları çeşitlilik alanlarını içermese de okunmaya devam eder; eksik sanatçı veya tür metadata'sı yalnızca ilgili kontrol için bilgi sağlamaz.
- Duplicate, yayın hakkı ve kalite kapıları çeşitlilikten önce çalışır; çeşitlilik bu güvenlik kararlarını asla geçersiz kılamaz. Çeşitlilik reddi bir kaynağı sağlıksız saymaz ve kalite, hak veya HTTP retry telemetrisini değiştirmez. Uygunsa aynı kaynaktaki sınırlı sayıdaki alternatif denenir; ardından normal kaynak rotasyonu devam eder.

---

## 🌐 Network Resilience

Dış HTTP çağrıları (müze aramaları, görsel indirmeleri) tek bir sınırlı geçici-hata politikasını paylaşır (`http_requests.py`):

- **Sınırlı retry:** En fazla 3 deneme (`MAX_TRANSIENT_HTTP_ATTEMPTS`); sonsuz retry yoktur.
- **Geçici durumlar:** `429`, `500`, `502`, `503`, `504` yeniden denenir.
- **Geçici taşımalar:** `requests.Timeout` ve `requests.ConnectionError` yeniden denenir; denemeler tükenince son hata yükseltilir (sessiz None dönüşü yoktur — degrade kararı caller katmanındadır).
- **Kalıcı hatalar denenmez:** `400`, `401`, `404` ve genel `Exception` avcıları retry dışıdır. `403` varsayılan kümede **kasıtlı olarak yoktur**; yalnızca gerekçeli kaynaklara özel olarak eklenir (AIC metadata Cloudflare challenge'ı, görsel CDN hotlink durumları).
- **Backoff + jitter:** `0.5s` tabanlı exponential backoff, rastgele jitter ve `10s` üst sınır — GitHub Actions'ta uzun beklemeler oluşmaz.
- **Retry-After:** `429`/`503` yanıtlarında sayısal `Retry-After` değeri kullanılır; `30s` ile sınırlıdır. Bozuk/negatif/aşırı büyük değerler yok sayılır veya kırpılır — uzak sunucu bota sınırsız uyku dayatamaz.
- **Kaynak temizliği:** Retry öncesinde atılan yanıtlar `close()` ile kapatılır; son dönen yanıt kapatılmaz.
- **Loglar yapılandırılmıştır:** `http_transient_retry endpoint=<safe_label> reason=<status|Timeout|...> attempt=1/3 delay=0.62s` — tam URL'ler, API anahtarları veya kimlik bilgileri asla loglanmaz.
- **Tumblr oluşturma istekleri bilinçli olarak retry EDİLMEZ:** `create_photo` idempotent değildir; şeffaf retry, istek ilk denemede işlendiyse double-post üretir. Bu çağrılar paylaşılan politikanın dışındadır; belirsiz sonuçlar yayın journal'ındaki `uncertain` yoluyla ele alınır (yukarıdaki bölüme bakın).
- **Görsel güvenliği değişmez:** MIME beyaz listesi, indirme boyutu/piksel/boyut sınırları, Pillow doğrulama ve decompression-bomb koruması retry politikasından bağımsız olarak aynen uygulanır. AIC IIIF 403'ünde status-retry yapılmadan tek seferlik standart-boyut (843px) fallback'e geçilir.

---

## 🧪 Development & CI

Yerel kalite kontrolleri:

```bash
python3 -m unittest discover -v   # birim testleri (yetkili süite)
ruff check .                      # statik analiz (pyproject.toml: E4/E7/E9/F, E701 hariç)
python3 -m compileall -q .        # sözdizimi kontrolü
```

CI araçları `requirements-dev.lock` içindedir (`pip install -r requirements-dev.lock`).

### Bağımlılık kilitleme (reproducible installs)

| Dosya | Rol |
|---|---|
| `requirements.in` / `requirements-dev.in` | **Yetkili kaynak** — insan tarafından düzenlenir (doğrudan bağımlılıklar + aralık kısıtları) |
| `requirements.lock` / `requirements-dev.lock` | **Üretilmiş** — pip-compile ile çözülmüş tam sürümler; elle düzenlenmez |

Kurulum:

```bash
# Üretim (yalnızca çalışma zamanı grafiği)
python3 -m pip install -r requirements.lock

# Geliştirme / CI (çalışma zamanı + ruff + pip-tools)
python3 -m pip install -r requirements-dev.lock
```

Kilitleri yeniden üretme (projenin tam komutu, Python 3.11 ile):

```bash
pip-compile requirements.in --output-file requirements.lock
pip-compile requirements-dev.in --output-file requirements-dev.lock
```

Kural: sürüm değişikliği **her zaman `.in` dosyasından** yapılır, sonra kilitler yeniden üretilir. Her iki GitHub Actions workflow'u da kilit dosyalarından kurulum yapar; üretim (`tumblr.yml`) yalnızca çalışma zamanı grafiğini kurar, CI (`ci.yml`) dev grafiğini kurar. Hash kilitleme bilinçli olarak kullanılmıyor: sdist-only paketler (`future`, `pytumblr`) hash'li kurulumu araç zinciri değişimlerinde kırılgan yapar; `ubuntu-latest` runner sabitliği + tam sürüm pinleri yeterli tekrarlanabilirlik sağlar. Not: pip-tools 7.6.1, kilit başlığına hiç geçirilmese bile `--no-index` bayrağını yazar (araç quirk'i); yukarıdaki komut gerçekten çalıştırılan komuttur.

GitHub Actions (`ci.yml` — **CI Quality Gate**) bu kontrolleri `main`'e yapılan her push ve PR üzerinde çalıştırır:

- **CI'nın Tumblr yayınlama kimlik bilgisi YOKTUR** — iş akışında hiçbir `TUMBLR_*` secret'ına referans verilmez.
- **CI Tumblr gönderisi yayınlamaz** — yalnızca sözdizimi, lint ve birim testleri çalıştırır; tüm testler mock/ticari dosyalarla izoledir ve üretim state dosyalarına dokunmaz.
- Yetki en az seviyededir (`contents: read`); aynı branch/PR'daki eski CI çalıştırmaları otomatik iptal edilir.

Üretim yayını yalnızca zamanlanmış `tumblr.yml` workflow'unda, gerçek kimlik bilgileriyle ve kendi `contents: write` yetkisiyle gerçekleşir.

---

## 🩺 Source Health & Circuit Breaker

Her müze kaynağının kendi **işlem-başı (per-run)** sağlık durumu vardır (`artfolio/curation/source_health.py`):

- **Operasyonel arıza tanımı:** Timeout / ConnectionError (paylaşılan HTTP retry politikası tükendikten sonra, tek mantıksal arıza olarak sayılır), HTTP 5xx/429 kalıcılığı, bozuk JSON, beklenmeyen yanıt şekli.
- **İçerik sonuçları arıza değildir:** Boş arama, tüm adayların duplicate olması, kalite eşiği altı skor, hak reddi veya hedef tür uyuşmazlığı kaynağın sağlığını bozmaz.
- **Durum makinesi:** `CLOSED` (normal) → ardışık `SOURCE_CIRCUIT_FAILURE_THRESHOLD` (varsayılan 3) arıza → `OPEN` → `SOURCE_CIRCUIT_COOLDOWN_SECONDS` (varsayılan 300s) sonra tek kontrollü `HALF_OPEN` denemesi → başarıda `CLOSED`, arızada tekrar `OPEN`.
- **OPEN kaynak atlanır:** Seçim sırasında `source_circuit_skipped` log'u ile geçilir ve `circuit_skips` istatistiği artar; kalite/hak reddi sayılmaz.
- **AIC devre dışı durumu değişmedi:** Yayın bayrağı kapalıyken AIC hiç denenmez, sağlık state'i oluşmaz. Harvard'ın hak fail-closed davranışı operasyonel arıza değildir; eksik API anahtarı da arıza sayılmaz.
- **State kalıcı değildir:** Sağlık durumu yalnızca process/koşu içindedir; kaynaklar-arası taşınmaz, yeni `MuseumAPIClient` taze state ile başlar. Koşu sonunda `source_health_summary` satırları loglanır; `client.get_source_health()` programatik anlık görüntü verir.

---

## 📁 Proje Dosya Yapısı

```
.
├── .github/
│   └── workflows/
│       ├── tumblr.yml       # Zamanlanmış üretim yayın workflow'u (cron)
│       └── ci.yml           # CI Quality Gate: push/PR'da test + lint + sözdizimi
├── .env.example             # Yerel testler için örnek ortam değişkenleri
├── artfolio/                # Çekirdek paket
│   ├── domain/              # Saf alan modeli (Artwork + yayın hakları alanları)
│   ├── scoring/             # ArtworkScorer + telemetri (ScoringTelemetry, pool)
│   ├── sources/             # Müze kaynak adaptörleri (met, aic, cma, smk, harvard)
│   └── curation/            # MuseumAPIClient orkestratörü (rotasyon, seçim, istatistik)
├── config.py                # Konfigürasyon ve sabitler
├── museum_api.py            # Uyumluluk cephesi: artfolio.* sembollerini yeniden dışa aktarır
├── http_requests.py         # Paylaşılan sınırlı retry ve güvenli HTTP log politikası
├── publication_state.py     # Yetkili rezervasyon/yayın transaction journal'ı
├── tumblr_poster.py         # Tumblr API paylaşım ve dinamik 5 SEO etiket motoru
├── main.py                  # Ana orkestratör
├── posted_ids.json          # Paylaşılan eser ID kayıtları
├── requirements.in          # Çalışma zamanı bağımlılıkları (yetkili kaynak)
├── requirements.lock        # Üretilmiş tam sürüm kilidi (elle düzenlenmez)
├── requirements-dev.in      # Dev/CI bağımlılıkları (yetkili kaynak)
├── requirements-dev.lock    # Üretilmiş tam sürüm kilidi (elle düzenlenmez)
├── test_bot.py              # Birim ve entegrasyon testleri
└── README.md                # Kurulum ve kullanım kılavuzu
```

## 🏛️ Mimari

```text
main.py
  ├── MuseumAPIClient (artfolio/curation)   ← kaynak rotasyonu, seçim, istatistikler
  │      ├── MetSource  ├── AICSource       ← sağlayıcı sorgu/ayrıştırma/hak eşleme
  │      ├── CMASource  ├── SMKSource       (artfolio/sources)
  │      └── HarvardSource
  ├── PublicationStateStore (publication_state.py)
  └── TumblrPoster (tumblr_poster.py)
```

- **domain:** `Artwork` alan modeli; sağlayıcı ayrıştırması içermez.
- **scoring:** `ArtworkScorer` ve salt-gözlem telemetrisi (seçime katılmaz).
- **sources:** Her müze için adaptör — uçlar, sorgu kurulumu, HTTP çağrıları, hak eşlemesi ve `Artwork` normalizasyonu o modüle aittir. Harvard bilinçli olarak fail-closed'tur; AIC üretimde devre dışıdır (`AIC_PUBLISHING_ENABLED`, `artfolio/sources/aic.py`).
- **curation:** `MuseumAPIClient` yalnızca orkestrasyon yapar; sağlayıcı JSON ayrıştırması içermez.
- **`museum_api.py`** geçici bir uyumluluk cephesidir — tüm müze mantığı artık `artfolio` paketindendir; yeni kod doğrudan `artfolio.*` import etmelidir.

---
