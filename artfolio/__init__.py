"""artfolio - Tumblr sanat kürasyon botunun çekirdek paketi.

Katmanlar (bağımlılık yönü aşağı doğrudur):
    domain    -> saf veri modeli (bağımlılıksız)
    scoring   -> puanlama + telemetri (config'e okur)
    sources   -> müze kaynak adaptörleri (domain, scoring, http_requests, config)
    curation  -> kaynak seçim/orkestrasyon (sources, scoring, domain)

Kök `museum_api.py` bu paketin uyumluluk cephesidir (compatibility facade).
"""
