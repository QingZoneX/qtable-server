from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.estimate_workload import (
    get_estimate_workload,
    submit_estimate_feedback,
)
from app.api.graphql.queries.task_split import TaskSplitQueries
from app.api.task_split import get_task_split_plan
from app.db.base import Base
from app.models.estimate_workload import WorkloadEstimateRun
from app.models.task_split import TaskSplitPlan
from app.models.user import User
from app.schemas.estimate_workload import (
    EstimateFeedbackRequest,
    EstimateWorkloadRequest,
)
from app.services.estimate_workload import EstimateWorkloadService


@pytest.fixture
async def ai_result_db(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'ai-result-permissions.db'}"
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        alice = User(
            id=601,
            email="alice-ai-result@example.test",
            name="Alice",
            password_hash="!test",
        )
        bob = User(
            id=602,
            email="bob-ai-result@example.test",
            name="Bob",
            password_hash="!test",
        )
        session.add_all(
            [
                alice,
                bob,
                TaskSplitPlan(
                    id="plan-alice",
                    workspace_id="ws-result",
                    project_id="project-result",
                    source_prompt="Alice private source",
                    normalized_goal="Alice goal",
                    summary="Alice private summary",
                    mermaid="graph TD; A-->B",
                    structured_output={"summary": "Alice private result"},
                    provider="test",
                    model="test",
                    trace_id="trace-plan-alice",
                    created_by=str(alice.id),
                ),
                TaskSplitPlan(
                    id="plan-bob",
                    workspace_id="ws-result",
                    project_id="project-result",
                    source_prompt="Bob private source",
                    normalized_goal="Bob goal",
                    summary="Bob private summary",
                    mermaid="graph TD; B-->C",
                    structured_output={"summary": "Bob private result"},
                    provider="test",
                    model="test",
                    trace_id="trace-plan-bob",
                    created_by=str(bob.id),
                ),
                WorkloadEstimateRun(
                    id="estimate-alice",
                    workspace_id="ws-result",
                    project_id="project-result",
                    source_prompt="secure table permission",
                    normalized_scope="secure table permission",
                    request_payload={},
                    structured_output={"summary": "Alice private workload"},
                    historical_summary={},
                    confidence_score=0.8,
                    base_story_points=5.0,
                    adjusted_story_points=5.0,
                    p50_hours=20.0,
                    p90_hours=30.0,
                    created_by=str(alice.id),
                ),
                WorkloadEstimateRun(
                    id="estimate-bob",
                    workspace_id="ws-result",
                    project_id="project-result",
                    source_prompt="secure table permission",
                    normalized_scope="secure table permission",
                    request_payload={},
                    structured_output={"summary": "Bob private workload"},
                    historical_summary={},
                    confidence_score=0.8,
                    base_story_points=8.0,
                    adjusted_story_points=8.0,
                    p50_hours=32.0,
                    p90_hours=48.0,
                    created_by=str(bob.id),
                ),
            ]
        )
        await session.commit()
        yield session, alice, bob

    await engine.dispose()


@pytest.mark.asyncio
async def test_task_split_rest_result_is_scoped_to_creator(ai_result_db):
    session, alice, _ = ai_result_db

    own = await get_task_split_plan(
        "plan-alice",
        user_id=alice.id,
        db=session,
    )
    assert own["id"] == "plan-alice"
    assert own["result"]["summary"] == "Alice private result"

    with pytest.raises(HTTPException) as exc:
        await get_task_split_plan(
            "plan-bob",
            user_id=alice.id,
            db=session,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_task_split_graphql_result_is_scoped_to_creator(ai_result_db):
    session, alice, _ = ai_result_db
    info = SimpleNamespace(context={"db": session, "user": alice})

    own = await TaskSplitQueries().task_split_plan(info, planId="plan-alice")
    assert own is not None
    assert own.planId == "plan-alice"

    hidden = await TaskSplitQueries().task_split_plan(info, planId="plan-bob")
    assert hidden is None


@pytest.mark.asyncio
async def test_workload_result_and_feedback_are_scoped_to_creator(ai_result_db):
    session, alice, _ = ai_result_db

    own = await get_estimate_workload(
        "estimate-alice",
        user_id=alice.id,
        db=session,
    )
    assert own["id"] == "estimate-alice"
    assert own["result"]["summary"] == "Alice private workload"

    with pytest.raises(HTTPException) as get_exc:
        await get_estimate_workload(
            "estimate-bob",
            user_id=alice.id,
            db=session,
        )
    assert get_exc.value.status_code == 404

    feedback = EstimateFeedbackRequest(
        actualHours=24.0,
        actualStoryPoints=5.0,
        accuracyRating=4,
        notes="owner feedback",
    )
    updated = await submit_estimate_feedback(
        "estimate-alice",
        feedback,
        user_id=alice.id,
        db=session,
    )
    assert updated["actualHours"] == 24.0
    assert updated["actualStoryPoints"] == 5.0

    with pytest.raises(HTTPException) as feedback_exc:
        await submit_estimate_feedback(
            "estimate-bob",
            feedback,
            user_id=alice.id,
            db=session,
        )
    assert feedback_exc.value.status_code == 404


@pytest.mark.asyncio
async def test_workload_history_and_cache_are_isolated_per_user(
    ai_result_db,
    monkeypatch,
):
    session, alice, bob = ai_result_db
    service = EstimateWorkloadService()

    async def no_cache(_key):
        return None

    async def ignore_cache_write(_key, _payload, *, ttl_seconds):
        assert ttl_seconds == 300

    monkeypatch.setattr(service, "_get_cached_json", no_cache)
    monkeypatch.setattr(service, "_set_cached_json", ignore_cache_write)

    request = EstimateWorkloadRequest.model_validate(
        {
            "prompt": "secure table permission",
            "workspaceId": "ws-result",
            "projectId": "project-result",
            "historicalLearning": {
                "enabled": True,
                "limit": 10,
                "lookbackDays": 180,
                "requireFeedback": False,
            },
        }
    )
    agent_context = SimpleNamespace(scope_key="scope:result")

    alice_history = await service._load_historical_learning(
        session,
        request=request,
        agent_context=agent_context,
        user_id=alice.id,
    )
    bob_history = await service._load_historical_learning(
        session,
        request=request,
        agent_context=agent_context,
        user_id=bob.id,
    )

    assert alice_history.sample_count == 1
    assert [item.estimate_id for item in alice_history.examples] == [
        "estimate-alice"
    ]
    assert all("Bob" not in item.summary for item in alice_history.examples)

    assert bob_history.sample_count == 1
    assert [item.estimate_id for item in bob_history.examples] == [
        "estimate-bob"
    ]

    alice_key = service._history_cache_key(
        request,
        agent_context,
        user_id=alice.id,
    )
    bob_key = service._history_cache_key(
        request,
        agent_context,
        user_id=bob.id,
    )
    assert alice_key != bob_key
