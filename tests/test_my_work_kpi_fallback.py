from __future__ import annotations

from types import SimpleNamespace

import pytest

import app.services.my_work_kpi_fallback as fallback_module
from app.services.my_work_kpi_fallback import build_my_work_kpi_fallback


class _ScalarResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class _PairResult:
    def __init__(self, values):
        self._values = values

    def all(self):
        return list(self._values)


class _Db:
    def __init__(self, records, history):
        self._responses = [
            _ScalarResult(records),
            _PairResult(history),
        ]

    async def execute(self, _statement):
        return self._responses.pop(0)


@pytest.mark.asyncio
async def test_kpi_fallback_ignores_malformed_values_and_respects_visibility(monkeypatch):
    user_id = 7
    ctx = SimpleNamespace(
        table_id="table-1",
        config={
            "statusFieldId": "status",
            "assigneeFieldId": "owner",
            "dueDateFieldId": "due",
            "progressFieldId": "progress",
            "completedStatusValues": ["done"],
            "blockedStatusValues": ["blocked"],
        },
        row_policy={"mode": "creator", "memberFieldId": None, "enabled": True},
        permission="edit",
    )

    async def fake_contexts(_db, *, user_id):
        assert user_id == 7
        return [ctx]

    monkeypatch.setattr(fallback_module, "_task_table_contexts", fake_contexts)

    records = [
        SimpleNamespace(
            id="bad-legacy",
            created_by_user_id=user_id,
            data={
                "status": "doing",
                "owner": ["7"],
                "due": "not-a-valid-timestamp-long",
                "progress": "50%",
            },
        ),
        SimpleNamespace(
            id="blocked",
            created_by_user_id=user_id,
            data={
                "status": "blocked",
                "owner": ["7"],
                "due": None,
                "progress": 20,
            },
        ),
        SimpleNamespace(
            id="done",
            created_by_user_id=user_id,
            data={
                "status": "done",
                "owner": ["7"],
                "due": None,
                "progress": 100,
            },
        ),
        # Assigned to the user but hidden by the creator row policy. It must
        # not influence any fallback KPI value or history visibility.
        SimpleNamespace(
            id="hidden",
            created_by_user_id=99,
            data={
                "status": "blocked",
                "owner": ["7"],
                "due": "2000-01-01",
                "progress": 0,
            },
        ),
    ]

    done_change = SimpleNamespace(
        entity_id="done",
        changed_fields=["status"],
        after_data={"status": "done", "owner": ["7"]},
    )
    hidden_change = SimpleNamespace(
        entity_id="hidden",
        changed_fields=["status"],
        after_data={"status": "done", "owner": ["7"]},
    )
    change_set = SimpleNamespace()

    result = await build_my_work_kpi_fallback(
        _Db(records, [(done_change, change_set), (hidden_change, change_set)]),
        user_id=user_id,
        timezone_name="UTC",
    )

    assert result == {
        "myIncompleteCount": 2,
        "completedThisWeekCount": 1,
        "activeProjectCount": 1,
        "overdueOrRiskCount": 1,
    }


def test_member_assignment_matches_scalar_and_list_contract():
    assert fallback_module._member_contains_user("7", 7) is True
    assert fallback_module._member_contains_user(["6", "7"], 7) is True
    assert fallback_module._member_contains_user(["6"], 7) is False
    assert fallback_module._member_contains_user(None, 7) is False
