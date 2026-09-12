from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import redis.asyncio as aioredis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.agent_workflow import (
    AgentWorkflow,
    AgentWorkflowStep,
    AgentWorkflowHumanApproval,
)

logger = logging.getLogger(__name__)


class RedisSessionManager:
    def __init__(self) -> None:
        self._redis: Optional[aioredis.Redis] = None

    async def connect(self) -> None:
        if self._redis is not None:
            return
        self._redis = aioredis.Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            db=settings.REDIS_DB,
            decode_responses=True,
        )
        await self._redis.ping()
        logger.info("RedisSessionManager connected to %s:%s", settings.REDIS_HOST, settings.REDIS_PORT)

    async def disconnect(self) -> None:
        if self._redis is not None:
            await self._redis.close()
            self._redis = None

    @property
    def client(self) -> aioredis.Redis:
        if self._redis is None:
            raise RuntimeError("RedisSessionManager not connected")
        return self._redis

    def _workflow_key(self, workflow_id: str) -> str:
        return f"agent-workflow:{workflow_id}"

    def _session_key(self, session_id: str) -> str:
        return f"agent-workflow-session:{session_id}"

    def _resume_token_key(self, workflow_id: str) -> str:
        return f"agent-workflow-resume:{workflow_id}"

    async def set_workflow_state(
        self,
        workflow_id: str,
        state: dict[str, Any],
        ttl_seconds: int = 86400,
    ) -> None:
        key = self._workflow_key(workflow_id)
        await self.client.setex(key, ttl_seconds, json.dumps(state, ensure_ascii=False, default=str))

    async def get_workflow_state(self, workflow_id: str) -> Optional[dict[str, Any]]:
        key = self._workflow_key(workflow_id)
        data = await self.client.get(key)
        if data:
            return json.loads(data)
        return None

    async def delete_workflow_state(self, workflow_id: str) -> None:
        key = self._workflow_key(workflow_id)
        await self.client.delete(key)

    async def set_session_workflows(
        self,
        session_id: str,
        workflow_ids: list[str],
        ttl_seconds: int = 86400,
    ) -> None:
        key = self._session_key(session_id)
        await self.client.setex(key, ttl_seconds, json.dumps(workflow_ids))

    async def get_session_workflows(self, session_id: str) -> list[str]:
        key = self._session_key(session_id)
        data = await self.client.get(key)
        if data:
            return json.loads(data)
        return []

    async def set_resume_token(
        self,
        workflow_id: str,
        checkpoint_id: str,
        ttl_seconds: int = 604800,
    ) -> None:
        key = self._resume_token_key(workflow_id)
        payload = {
            "workflowId": workflow_id,
            "checkpointId": checkpoint_id,
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
        await self.client.setex(key, ttl_seconds, json.dumps(payload))

    async def get_resume_token(self, workflow_id: str) -> Optional[dict[str, Any]]:
        key = self._resume_token_key(workflow_id)
        data = await self.client.get(key)
        if data:
            return json.loads(data)
        return None

    async def delete_resume_token(self, workflow_id: str) -> None:
        key = self._resume_token_key(workflow_id)
        await self.client.delete(key)

    async def publish_workflow_event(
        self,
        workflow_id: str,
        event: dict[str, Any],
    ) -> None:
        channel = f"workflow-events:{workflow_id}"
        await self.client.publish(channel, json.dumps(event, ensure_ascii=False, default=str))

    async def subscribe_workflow_events(self, workflow_id: str):
        channel = f"workflow-events:{workflow_id}"
        pubsub = self.client.pubsub()
        await pubsub.subscribe(channel)
        return pubsub


redis_session_manager = RedisSessionManager()


class PostgresCheckpointSaver:
    async def save_checkpoint(
        self,
        db: AsyncSession,
        workflow_id: str,
        checkpoint_data: dict[str, Any],
    ) -> None:
        result = await db.execute(
            select(AgentWorkflow).where(AgentWorkflow.id == workflow_id)
        )
        workflow = result.scalars().first()
        if workflow:
            workflow.langgraph_checkpoint_id = checkpoint_data.get("checkpoint_id")
            workflow.plan_snapshot = checkpoint_data.get("plan")
            workflow.tool_calls_snapshot = checkpoint_data.get("tool_calls")
            workflow.observations_snapshot = checkpoint_data.get("observations")
            workflow.current_step_index = checkpoint_data.get("current_step_index", -1)
            workflow.status = checkpoint_data.get("status", workflow.status)
            workflow.pending_approvals_snapshot = checkpoint_data.get("pending_approvals")
            workflow.approval_history_snapshot = checkpoint_data.get("approval_history")
            workflow.final_response = checkpoint_data.get("final_response")
            workflow.updated_at = datetime.now(timezone.utc)
        await db.commit()

    async def load_checkpoint(
        self,
        db: AsyncSession,
        workflow_id: str,
    ) -> Optional[dict[str, Any]]:
        result = await db.execute(
            select(AgentWorkflow).where(AgentWorkflow.id == workflow_id)
        )
        workflow = result.scalars().first()
        if not workflow:
            return None
        return {
            "checkpoint_id": workflow.langgraph_checkpoint_id,
            "workflow_id": workflow.id,
            "status": workflow.status,
            "plan": workflow.plan_snapshot,
            "tool_calls": workflow.tool_calls_snapshot,
            "observations": workflow.observations_snapshot,
            "current_step_index": workflow.current_step_index,
            "pending_approvals": workflow.pending_approvals_snapshot,
            "approval_history": workflow.approval_history_snapshot,
            "final_response": workflow.final_response,
            "context_snapshot": workflow.context_snapshot,
            "config_snapshot": workflow.config_snapshot,
            "user_message": workflow.user_message,
            "table_ids": json.loads(workflow.table_ids) if workflow.table_ids else [],
            "workspace_id": workflow.workspace_id,
            "session_id": workflow.session_id,
            "conversation_id": workflow.conversation_id,
            "user_id": workflow.user_id,
            "error": workflow.error_snapshot,
            "retry_policy": workflow.retry_policy,
            "metadata": workflow.metadata_snapshot,
            "created_at": workflow.created_at.isoformat() if workflow.created_at else None,
            "updated_at": workflow.updated_at.isoformat() if workflow.updated_at else None,
        }


class WorkflowPersistenceManager:
    def __init__(self) -> None:
        self.checkpoint_saver = PostgresCheckpointSaver()
        self.redis = redis_session_manager

    async def create_workflow(
        self,
        db: AsyncSession,
        *,
        workflow_id: str,
        user_id: int,
        session_id: Optional[str],
        conversation_id: Optional[str],
        workspace_id: Optional[str],
        table_ids: list[str],
        user_message: str,
        config_snapshot: dict[str, Any],
        context_snapshot: dict[str, Any],
        retry_policy: dict[str, Any],
        agent_context: Optional[dict[str, Any]] = None,
        project_id: Optional[str] = None,
        view_id: Optional[str] = None,
        task_id: Optional[str] = None,
        team_id: Optional[str] = None,
        organization_id: Optional[str] = None,
        workflow_def_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> AgentWorkflow:
        workflow = AgentWorkflow(
            id=workflow_id,
            session_id=session_id,
            user_id=str(user_id),
            conversation_id=conversation_id,
            workspace_id=workspace_id,
            table_ids=json.dumps(table_ids) if table_ids else None,
            user_message=user_message,
            status="initializing",
            config_snapshot=config_snapshot,
            context_snapshot=context_snapshot,
            retry_policy=retry_policy,
            metadata_snapshot={
                "projectId": project_id,
                "viewId": view_id,
                "taskId": task_id,
                "teamId": team_id,
                "organizationId": organization_id,
                "workflowDefId": workflow_def_id,
                "agentId": agent_id,
                "agentContext": agent_context,
            },
        )
        db.add(workflow)
        await db.commit()
        await db.refresh(workflow)

        if session_id:
            active = await self.redis.get_session_workflows(session_id)
            if workflow_id not in active:
                active.append(workflow_id)
                await self.redis.set_session_workflows(session_id, active)

        logger.info(
            "WorkflowPersistenceManager created workflow_id=%s user_id=%s",
            workflow_id,
            user_id,
        )
        return workflow

    async def update_workflow_status(
        self,
        db: AsyncSession,
        workflow_id: str,
        status: str,
        final_response: Optional[str] = None,
        error: Optional[dict[str, Any]] = None,
    ) -> None:
        values: dict[str, Any] = {
            "status": status,
            "updated_at": datetime.now(timezone.utc),
        }
        if final_response is not None:
            values["final_response"] = final_response
        if error is not None:
            values["error_snapshot"] = error
        await db.execute(
            update(AgentWorkflow)
            .where(AgentWorkflow.id == workflow_id)
            .values(**values)
        )
        await db.commit()

    async def save_step(
        self,
        db: AsyncSession,
        step_data: dict[str, Any],
    ) -> AgentWorkflowStep:
        step = AgentWorkflowStep(
            id=step_data["id"],
            workflow_id=step_data["workflow_id"],
            step_index=step_data["step_index"],
            description=step_data.get("description", ""),
            skill_name=step_data.get("skill_name"),
            arguments=step_data.get("arguments"),
            status=step_data.get("status", "pending"),
            depends_on=step_data.get("depends_on"),
            max_retries=step_data.get("max_retries", 3),
            retry_count=step_data.get("retry_count", 0),
            require_approval=step_data.get("require_approval", False),
            result=step_data.get("result"),
            error=step_data.get("error"),
            started_at=step_data.get("started_at"),
            completed_at=step_data.get("completed_at"),
        )
        db.add(step)
        await db.commit()
        return step

    async def save_approval(
        self,
        db: AsyncSession,
        approval_data: dict[str, Any],
    ) -> AgentWorkflowHumanApproval:
        approval = AgentWorkflowHumanApproval(
            id=approval_data["id"],
            workflow_id=approval_data["workflow_id"],
            call_id=approval_data["call_id"],
            skill_name=approval_data["skill_name"],
            function_name=approval_data["function_name"],
            arguments=approval_data.get("arguments"),
            reason=approval_data.get("reason", ""),
            preview=approval_data.get("preview"),
            status=approval_data.get("status", "pending"),
        )
        db.add(approval)
        await db.commit()
        return approval

    async def resolve_approval(
        self,
        db: AsyncSession,
        approval_id: str,
        status: str,
    ) -> None:
        await db.execute(
            update(AgentWorkflowHumanApproval)
            .where(AgentWorkflowHumanApproval.id == approval_id)
            .values(
                status=status,
                resolved_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()

    async def get_workflow(self, db: AsyncSession, workflow_id: str) -> Optional[AgentWorkflow]:
        result = await db.execute(
            select(AgentWorkflow).where(AgentWorkflow.id == workflow_id)
        )
        return result.scalars().first()

    async def get_pending_approvals(
        self,
        db: AsyncSession,
        workflow_id: str,
    ) -> list[AgentWorkflowHumanApproval]:
        result = await db.execute(
            select(AgentWorkflowHumanApproval)
            .where(
                AgentWorkflowHumanApproval.workflow_id == workflow_id,
                AgentWorkflowHumanApproval.status == "pending",
            )
        )
        return list(result.scalars().all())


workflow_persistence_manager = WorkflowPersistenceManager()
