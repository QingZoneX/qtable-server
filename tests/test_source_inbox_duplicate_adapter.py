from __future__ import annotations

import pytest

from app.models.source_inbox import SourceInboxItem
from app.services.source_inbox_unified import (
    UnifiedSourceInboxService,
    source_inbox_service,
)
from app.services.task_duplicates import task_duplicate_service


@pytest.mark.asyncio
async def test_source_inbox_duplicate_context_uses_shared_detector(monkeypatch):
    calls: list[dict[str, object]] = []

    async def fake_visible_store(
        db,
        *,
        user_id: int,
        table_id: str,
        required_permission: str,
    ):
        calls.append(
            {
                "db": db,
                "user_id": user_id,
                "table_id": table_id,
                "required_permission": required_permission,
            }
        )
        return {
            "defaultViewId": "v1",
            "fields": [
                {"id": "title", "name": "任务名称", "type": "text"},
                {"id": "description", "name": "任务描述", "type": "text"},
                {
                    "id": "status",
                    "name": "状态",
                    "type": "select",
                    "options": [{"id": "doing", "label": "进行中"}],
                },
            ],
            "records": [
                {
                    "id": "r1",
                    "title": "优化登录首页首次加载速度",
                    "description": "减少用户登录后首屏等待耗时",
                    "status": "doing",
                },
                {
                    "id": "r2",
                    "title": "整理财务月报",
                    "description": "汇总本月财务数据",
                    "status": "doing",
                },
            ],
        }

    monkeypatch.setattr(task_duplicate_service, "_visible_store", fake_visible_store)
    item = SourceInboxItem(
        id="inbox-1",
        user_id=7,
        workspace_id="w1",
        source_id="qnote-1",
        source_type="qnote",
        url="https://example.com/note",
        canonical_url="https://example.com/note",
        page_title="提升登录主页首屏性能",
        quote="降低首次加载延迟",
        annotation="优化登录体验",
        tags=["登录", "性能"],
        content_hash="hash",
        request_fingerprint="fingerprint",
        status="pending",
    )

    fields, candidates, truncated = await source_inbox_service._visible_task_context(
        object(),
        user_id=7,
        table_id="tasks",
        permission="edit",
        item=item,
    )

    assert isinstance(source_inbox_service, UnifiedSourceInboxService)
    assert len(calls) == 1
    assert calls[0]["user_id"] == 7
    assert calls[0]["table_id"] == "tasks"
    assert calls[0]["required_permission"] == "edit"
    assert len(fields) == 3
    assert truncated is False
    assert [candidate["recordId"] for candidate in candidates] == ["r1"]
    candidate = candidates[0]
    assert candidate["thresholdBand"] in {"high", "medium"}
    assert candidate["reason"]
    assert candidate["blocksCreation"] is False
    assert candidate["deepLink"].endswith("?recordId=r1")


@pytest.mark.asyncio
async def test_source_inbox_duplicate_context_accepts_url_only_capture(monkeypatch):
    async def fake_visible_store(
        db,
        *,
        user_id: int,
        table_id: str,
        required_permission: str,
    ):
        return {
            "defaultViewId": "v1",
            "fields": [
                {"id": "title", "name": "任务名称", "type": "text"},
                {"id": "source_url", "name": "来源链接", "type": "url"},
            ],
            "records": [
                {
                    "id": "same-source",
                    "title": "跟进来源事项",
                    "source_url": "https://example.com/sparse",
                }
            ],
        }

    monkeypatch.setattr(task_duplicate_service, "_visible_store", fake_visible_store)
    item = SourceInboxItem(
        id="inbox-sparse",
        user_id=7,
        workspace_id="w1",
        source_id="qnote-sparse",
        source_type="qnote",
        url="https://example.com/sparse",
        canonical_url="https://example.com/sparse",
        page_title=None,
        quote=None,
        annotation=None,
        tags=[],
        content_hash="hash-sparse",
        request_fingerprint="fingerprint-sparse",
        status="pending",
    )

    _, candidates, truncated = await source_inbox_service._visible_task_context(
        object(),
        user_id=7,
        table_id="tasks",
        permission="edit",
        item=item,
    )

    assert truncated is False
    assert len(candidates) == 1
    assert candidates[0]["recordId"] == "same-source"
    assert candidates[0]["matchType"] == "exact_source"
    assert candidates[0]["blocksCreation"] is False
