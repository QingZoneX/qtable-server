from app.api.graphql.helpers import (
    ALLOWED_VIEW_TYPES,
    DEFAULT_TOOLBAR_ITEMS,
    DEFAULT_VIEW_NAMES,
    _build_default_view_config,
)


def test_gallery_view_type_is_supported():
    assert "gallery" in ALLOWED_VIEW_TYPES
    assert DEFAULT_VIEW_NAMES["gallery"] == "Gallery"


def test_gallery_default_config_is_complete():
    config = _build_default_view_config("gallery")

    assert config["toolbar"]["items"] == DEFAULT_TOOLBAR_ITEMS["gallery"]
    assert config["filters"] == []
    assert config["sorts"] == []
    assert config["groupConfig"] == {"fieldId": None, "order": "asc"}
    assert config["hiddenFieldIds"] == []
    assert config["galleryConfig"] == {
        "coverFieldId": None,
        "titleFieldId": None,
        "cardSize": "medium",
        "imageFit": "cover",
        "showFieldNames": True,
    }


def test_gallery_default_config_is_not_shared_between_calls():
    first = _build_default_view_config("gallery")
    second = _build_default_view_config("gallery")

    first["galleryConfig"]["cardSize"] = "large"
    first["toolbar"]["items"].append("unexpected")

    assert second["galleryConfig"]["cardSize"] == "medium"
    assert "unexpected" not in second["toolbar"]["items"]
