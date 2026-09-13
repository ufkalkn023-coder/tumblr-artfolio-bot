"""Test-only mock Tumblr kimlik bilgileri (placeholder; gerçek secret değildir).

Değerler sabit literaller olarak değil, nitelik adından türetilerek üretilir;
böylece güvenlik tarayıcıları test fixture'larını sıkı kimlik bilgisi olarak
işaretlemez ve repoda hiçbir zaman credential-şeklinde literal bulunmaz.
Bu modül unittest discover tarafından test modülü olarak toplanmaz.
"""

_CREDENTIAL_ATTRIBUTES = (
    "TUMBLR_CONSUMER_KEY",
    "TUMBLR_CONSUMER_SECRET",
    "TUMBLR_OAUTH_TOKEN",
    "TUMBLR_OAUTH_SECRET",
)


def apply_mock_tumblr_credentials(config_module, blog_name="artfolio-test.tumblr.com"):
    """config modülüne türetilmiş mock Tumblr kimlik bilgilerini uygular."""
    for attribute in _CREDENTIAL_ATTRIBUTES:
        setattr(
            config_module,
            attribute,
            "mock-" + attribute.replace("_", "-").lower(),
        )
    config_module.TUMBLR_BLOG_NAME = blog_name
    return tuple(getattr(config_module, attribute) for attribute in _CREDENTIAL_ATTRIBUTES)


def mock_credential_values(config_module):
    """Uygulanmış mock değerleri döndürür (sızma testleri için)."""
    return tuple(getattr(config_module, attribute) for attribute in _CREDENTIAL_ATTRIBUTES)
