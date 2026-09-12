from app.api.graphql.helpers import (
    ALLOWED_VIEW_TYPES,
    DEFAULT_TOOLBAR_ITEMS,
    DEFAULT_VIEW_NAMES,
    _build_default_view_config,
)


def test_calendar_view_is_supported():
    assert "calendar" in ALLOWED_VIEW_TYPES
    assert DEFAULT_VIEW_NAMES["calendar"] == "Calendar"


def test_calendar_view_default_config():
    config = _build_default_view_config("calendar")

    assert config["calendarConfig"] == {
        "startFieldId": None,
        "endFieldId": None,
        "weekStartsOn": 1,
    }
    assert config["groupConfig"] == {"fieldId": None, "order": "asc"}
    assert config["filters"] == []
    assert config["sorts"] == []
    assert config["hiddenFieldIds"] == []
    assert config["toolbar"]["items"] == DEFAULT_TOOLBAR_ITEMS["calendar"]
    assert "viewSettings" in config["toolbar"]["items"]
    assert "filter" in config["toolbar"]["items"]
    assert "sort" in config["toolbar"]["items"]


def test_calendar_default_config_is_isolated_between_calls():
    first = _build_default_view_config("calendar")
    second = _build_default_view_config("calendar")

    first["calendarConfig"]["startFieldId"] = "start"
    first["toolbar"]["items"].append("unexpected")

    assert second["calendarConfig"]["startFieldId"] is None
    assert "unexpected" not in second["toolbar"]["items"]
