from __future__ import annotations

import asyncio
from types import SimpleNamespace
import pytest

from app.api.graphql import helpers, subscriptions
from app.api.graphql.helpers import publish_table_update, table_broker
from app.api.graphql.subscriptions import Subscription
from app.core.config import settings


@pytest.mark.asyncio
async def test_db_publish_does_not_materialize_full_store(monkeypatch):
    monkeypatch.setattr(settings, "DATA_BACKEND", "db")
    async def fail(*_args, **_kwargs):
        raise AssertionError("full store must not be loaded while publishing")
    captured = []
    async def capture(payload):
        captured.append(payload)
    monkeypatch.setattr(helpers, "get_full_store", fail)
    monkeypatch.setattr(helpers.table_broker, "publish", capture)
    event = await publish_table_update(SimpleNamespace(), "large-table")
    assert event["data"] is None
    assert event["snapshotIncluded"] is False
    assert captured == [event]


@pytest.mark.asyncio
async def test_table_updates_support_lightweight_invalidation(monkeypatch):
    monkeypatch.setattr(settings, "DATA_BACKEND", "db")
    async def allow(*_args, **_kwargs):
        return "read"
    async def row_context(*_args, **_kwargs):
        return SimpleNamespace(id=77), "read", {"mode": "all"}
    monkeypatch.setattr(subscriptions, "_require_item_permission", allow)
    monkeypatch.setattr(subscriptions, "_row_permission_context", row_context)
    info = SimpleNamespace(context={"db": SimpleNamespace()})
    gen = Subscription().tableUpdates(info, table_id="large-table", include_snapshot=False)
    pending = asyncio.create_task(anext(gen))
    await asyncio.sleep(0)
    await table_broker.publish({"tableId":"large-table","updatedAt":"2026-08-29T00:00:00","data":None})
    payload = await asyncio.wait_for(pending, timeout=1)
    await gen.aclose()
    assert payload["data"] is None
    assert payload["snapshotIncluded"] is False


@pytest.mark.asyncio
async def test_table_updates_keep_snapshot_backward_compatibility(monkeypatch):
    monkeypatch.setattr(settings, "DATA_BACKEND", "db")
    async def allow(*_args, **_kwargs):
        return "read"
    async def row_context(*_args, **_kwargs):
        return SimpleNamespace(id=77), "read", {"mode": "all"}
    async def load_store(*_args, **_kwargs):
        return {"fields":[],"records":[{"id":"r1","title":"visible"}],"views":[],"hiddenFieldIds":[],"filters":[],"sorts":[],"groupConfig":{"fieldId":None,"order":"asc"}}
    async def passthrough(_db, _table_id, store, **_kwargs):
        return store, {"mode":"all"}
    monkeypatch.setattr(subscriptions, "_require_item_permission", allow)
    monkeypatch.setattr(subscriptions, "_row_permission_context", row_context)
    monkeypatch.setattr(subscriptions, "get_full_store", load_store)
    monkeypatch.setattr(subscriptions, "filter_store_for_user", passthrough)
    info = SimpleNamespace(context={"db": SimpleNamespace()})
    gen = Subscription().tableUpdates(info, table_id="large-table", include_snapshot=True)
    pending = asyncio.create_task(anext(gen))
    await asyncio.sleep(0)
    await table_broker.publish({"tableId":"large-table","updatedAt":"2026-08-29T00:00:01","data":None})
    payload = await asyncio.wait_for(pending, timeout=1)
    await gen.aclose()
    assert payload["snapshotIncluded"] is True
    assert payload["data"]["records"] == [{"id":"r1","title":"visible"}]
