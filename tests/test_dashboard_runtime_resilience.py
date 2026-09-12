from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.services.dashboard_runtime_resilience as resilience
from app.services.dashboard_runtime import DashboardValidationError


class _Savepoint:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _Db:
    def begin_nested(self):
        return _Savepoint()


def _widget():
    return SimpleNamespace(
        id="widget-1",
        dashboard_id="dash-1",
        config={"tableId": "table-1"},
    )


@pytest.mark.asyncio
async def test_conversion_failure_uses_authorized_record_fallback(monkeypatch):
    captured = {}

    async def fail_sql(_db, _widget, *, user_id):
        assert user_id == 7
        raise ValueError("could not convert legacy numeric value")

    async def fallback(_db, widget, *, user_id):
        captured["widget"] = widget
        captured["user_id"] = user_id
        return {"rows": [{"dimension": "legacy", "value": 3.0}]}

    monkeypatch.setattr(resilience, "compute_widget_data_for_user", fail_sql)
    monkeypatch.setattr(resilience, "_compute_from_authorized_records", fallback)

    result = await resilience.compute_widget_data_resilient(
        _Db(), _widget(), user_id=7
    )

    assert result == {"rows": [{"dimension": "legacy", "value": 3.0}]}
    assert captured == {"widget": captured["widget"], "user_id": 7}


@pytest.mark.asyncio
async def test_validation_failure_is_not_hidden_by_fallback(monkeypatch):
    fallback_called = False

    async def fail_validation(_db, _widget, *, user_id):
        raise DashboardValidationError("Source table not found")

    async def fallback(*args, **kwargs):
        nonlocal fallback_called
        fallback_called = True
        return {"rows": []}

    monkeypatch.setattr(resilience, "compute_widget_data_for_user", fail_validation)
    monkeypatch.setattr(resilience, "_compute_from_authorized_records", fallback)

    with pytest.raises(DashboardValidationError, match="Source table not found"):
        await resilience.compute_widget_data_resilient(
            _Db(), _widget(), user_id=7
        )

    assert fallback_called is False


@pytest.mark.asyncio
async def test_permission_failure_is_not_hidden_by_fallback(monkeypatch):
    fallback_called = False

    async def fail_permission(_db, _widget, *, user_id):
        raise PermissionError("Dashboard source table is not available")

    async def fallback(*args, **kwargs):
        nonlocal fallback_called
        fallback_called = True
        return {"rows": []}

    monkeypatch.setattr(resilience, "compute_widget_data_for_user", fail_permission)
    monkeypatch.setattr(resilience, "_compute_from_authorized_records", fallback)

    with pytest.raises(PermissionError, match="source table"):
        await resilience.compute_widget_data_resilient(
            _Db(), _widget(), user_id=7
        )

    assert fallback_called is False
