from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.services.dashboard_runtime_filters as runtime_filters_module
from app.services.dashboard_runtime import DashboardValidationError
from app.services.dashboard_runtime_filters import compute_widget_data_with_runtime_filters


def _widget(config):
    return SimpleNamespace(id="widget-1", dashboard_id="dash-1", config=config)


@pytest.mark.asyncio
async def test_runtime_filters_append_without_mutating_persisted_config(monkeypatch):
    widget = _widget(
        {
            "tableId": "table-1",
            "filters": [{"fieldId": "status", "operator": "eq", "value": "open"}],
        }
    )
    original_config = {
        "tableId": "table-1",
        "filters": [{"fieldId": "status", "operator": "eq", "value": "open"}],
    }
    captured = {}

    async def fake_field_map(_db, table_id):
        assert table_id == "table-1"
        return {"status": SimpleNamespace(type="select")}

    async def fake_compute(_db, runtime_widget, *, user_id):
        assert user_id == 7
        captured["config"] = runtime_widget.config
        return {"rows": [{"value": 1.0}]}

    monkeypatch.setattr(runtime_filters_module, "_field_map", fake_field_map)
    monkeypatch.setattr(runtime_filters_module, "compute_widget_data_resilient", fake_compute)

    result = await compute_widget_data_with_runtime_filters(
        object(),
        widget,
        user_id=7,
        runtime_filters=[{"fieldId": "status", "operator": "neq", "value": "done"}],
    )

    assert result == {"rows": [{"value": 1.0}]}
    assert widget.config == original_config
    assert captured["config"]["filters"] == [
        {"fieldId": "status", "operator": "eq", "value": "open"},
        {"fieldId": "status", "operator": "neq", "value": "done"},
    ]


@pytest.mark.asyncio
async def test_runtime_filters_reuse_field_type_validation(monkeypatch):
    async def fake_field_map(_db, _table_id):
        return {"title": SimpleNamespace(type="text")}

    monkeypatch.setattr(runtime_filters_module, "_field_map", fake_field_map)

    with pytest.raises(DashboardValidationError, match="requires a numeric field"):
        await compute_widget_data_with_runtime_filters(
            object(),
            _widget({"tableId": "table-1", "filters": []}),
            user_id=7,
            runtime_filters=[{"fieldId": "title", "operator": "gt", "value": 3}],
        )


@pytest.mark.asyncio
async def test_empty_runtime_filters_delegate_to_resilient_permission_runtime(monkeypatch):
    widget = _widget({"tableId": "table-1", "filters": []})
    captured = {}

    async def fake_compute(_db, passed_widget, *, user_id):
        captured["widget"] = passed_widget
        captured["user_id"] = user_id
        return {"rows": [{"value": 2.0}]}

    monkeypatch.setattr(runtime_filters_module, "compute_widget_data_resilient", fake_compute)

    result = await compute_widget_data_with_runtime_filters(
        object(), widget, user_id=9, runtime_filters=[]
    )

    assert result == {"rows": [{"value": 2.0}]}
    assert captured == {"widget": widget, "user_id": 9}
