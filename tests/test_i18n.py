from app.core.i18n import (
    DEFAULT_LOCALE,
    catalog_keys_match,
    get_locale,
    locale_from_accept_language,
    locale_from_connection_params,
    normalize_locale,
    reset_locale,
    set_locale,
    translate,
)


def test_normalize_locale_supports_zh_and_en_variants():
    assert normalize_locale("zh-CN") == "zh-CN"
    assert normalize_locale("zh-TW") == "zh-CN"
    assert normalize_locale("en") == "en-US"
    assert normalize_locale("en-GB") == "en-US"
    assert normalize_locale("ja-JP") == DEFAULT_LOCALE


def test_accept_language_honors_quality_and_supported_languages():
    assert locale_from_accept_language("en-US,en;q=0.9,zh-CN;q=0.8") == "en-US"
    assert locale_from_accept_language("ja-JP,zh-CN;q=0.9,en-US;q=0.8") == "zh-CN"
    assert locale_from_accept_language("en-US;q=0.3,zh-CN;q=0.9") == "zh-CN"
    assert locale_from_accept_language(None) == DEFAULT_LOCALE


def test_websocket_connection_params_support_locale_aliases():
    assert locale_from_connection_params({"Accept-Language": "en-US"}) == "en-US"
    assert locale_from_connection_params({"locale": "zh-CN"}) == "zh-CN"
    assert locale_from_connection_params({}) is None


def test_request_scoped_translation_uses_context_locale_and_interpolation():
    token = set_locale("en-US")
    try:
        assert get_locale() == "en-US"
        assert translate("notification.defaultTitle") == "Notification"
        assert translate("notification.taskAssigned", actor="Alice") == "Alice assigned a task to you"
    finally:
        reset_locale(token)
    assert get_locale() == DEFAULT_LOCALE


def test_translation_catalogs_have_identical_keys():
    assert catalog_keys_match()
