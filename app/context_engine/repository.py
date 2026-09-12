from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import redis.asyncio as redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context_engine.models import AgentContext, ContextBuildInput
from app.core.config import settings
from app.models.context_session import ContextSession
from app.models.context_snapshot import ContextSnapshot


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _stable_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class ContextRepository:
    def __init__(self) -> None:
        self._redis_client: redis.Redis | None = None

    async def _get_redis(self) -> redis.Redis | None:
        if self._redis_client is not None:
            return self._redis_client
        try:
            client = redis.Redis(
                host=settings.REDIS_HOST,
                port=settings.REDIS_PORT,
                db=settings.REDIS_DB,
                decode_responses=True,
                socket_connect_timeout=0.3,
                socket_timeout=0.5,
            )
            await client.ping()
            self._redis_client = client
            return client
        except Exception:
            return None

    def build_scope_key(self, build_input: ContextBuildInput) -> str:
        session_part = build_input.session_id or build_input.conversation_id or "anonymous"
        user_part = build_input.user_id or "guest"
        workspace_part = build_input.workspace_id or "default"
        return f"user:{user_part}:workspace:{workspace_part}:session:{session_part}"

    def build_cache_key(self, build_input: ContextBuildInput) -> str:
        payload = {
            "scope": self.build_scope_key(build_input),
            "tableIds": build_input.table_ids,
            "viewId": build_input.view_id,
            "projectId": build_input.project_id,
            "taskId": build_input.task_id,
            "teamId": build_input.team_id,
            "organizationId": build_input.organization_id,
            "workflowId": build_input.workflow_id,
            "agentId": build_input.agent_id,
            "message": (build_input.message or "")[:500],
        }
        digest = _stable_hash(payload)
        return f"{settings.CONTEXT_CACHE_PREFIX}:snapshot:{digest}"

    def build_session_cache_key(self, session_key: str) -> str:
        return f"{settings.CONTEXT_CACHE_PREFIX}:session:{session_key}"

    async def get_cached_context(self, cache_key: str) -> AgentContext | None:
        client = await self._get_redis()
        if client is None:
            return None
        try:
            payload = await client.get(cache_key)
            if not payload:
                return None
            return AgentContext.model_validate_json(payload)
        except Exception:
            return None

    async def set_cached_context(self, cache_key: str, context: AgentContext) -> None:
        client = await self._get_redis()
        if client is None:
            return
        try:
            await client.setex(
                cache_key,
                settings.CONTEXT_CACHE_TTL_SECONDS,
                context.model_dump_json(by_alias=True),
            )
        except Exception:
            return

    async def get_cached_session(self, session_key: str) -> dict[str, Any] | None:
        client = await self._get_redis()
        if client is None:
            return None
        try:
            payload = await client.get(self.build_session_cache_key(session_key))
            return json.loads(payload) if payload else None
        except Exception:
            return None

    async def set_cached_session(self, session_key: str, payload: dict[str, Any]) -> None:
        client = await self._get_redis()
        if client is None:
            return
        try:
            await client.setex(
                self.build_session_cache_key(session_key),
                settings.CONTEXT_SESSION_TTL_SECONDS,
                json.dumps(payload, ensure_ascii=True, default=str),
            )
        except Exception:
            return

    async def load_or_create_session(
        self,
        db: AsyncSession,
        build_input: ContextBuildInput,
        *,
        scope_key: str,
    ) -> tuple[ContextSession, bool]:
        cached_session = await self.get_cached_session(scope_key)
        if cached_session:
            result = await db.execute(
                select(ContextSession).where(ContextSession.session_key == scope_key).limit(1)
            )
            record = result.scalars().first()
            if record:
                return record, True

        result = await db.execute(
            select(ContextSession).where(ContextSession.session_key == scope_key).limit(1)
        )
        record = result.scalars().first()
        if record:
            return record, False

        expires_at = _now_utc() + timedelta(seconds=settings.CONTEXT_SESSION_TTL_SECONDS)
        record = ContextSession(
            id=str(uuid.uuid4()),
            session_key=scope_key,
            user_id=str(build_input.user_id) if build_input.user_id is not None else None,
            workspace_id=build_input.workspace_id,
            conversation_id=build_input.conversation_id,
            project_id=build_input.project_id,
            table_id=(build_input.table_ids or [None])[0],
            view_id=build_input.view_id,
            task_id=build_input.task_id,
            team_id=build_input.team_id,
            organization_id=build_input.organization_id,
            workflow_id=build_input.workflow_id,
            agent_id=build_input.agent_id,
            session_state={},
            memory_summary=None,
            expires_at=expires_at,
        )
        db.add(record)
        await db.flush()
        return record, False

    async def persist_session(
        self,
        db: AsyncSession,
        record: ContextSession,
        *,
        context: AgentContext,
        cache_key: str,
    ) -> None:
        expires_at = _now_utc() + timedelta(seconds=settings.CONTEXT_SESSION_TTL_SECONDS)
        record.workspace_id = context.workspace.id
        record.conversation_id = context.conversation.id
        record.project_id = context.project.id
        record.table_id = context.table.id
        record.view_id = context.view.id
        record.task_id = context.task.id
        record.team_id = context.team.id
        record.organization_id = context.organization.id
        record.workflow_id = context.workflow.id
        record.agent_id = context.agents[0].id if context.agents else record.agent_id
        record.session_state = {
            "summary": context.summary.model_dump(mode="json", by_alias=True),
            "window": context.window.model_dump(mode="json", by_alias=True),
            "conversation": {
                "messageCount": context.conversation.message_count,
                "compression": context.conversation.compression.model_dump(mode="json", by_alias=True),
            },
        }
        record.memory_summary = context.conversation.summary or context.summary.text
        record.last_context_hash = cache_key.rsplit(":", 1)[-1]
        record.expires_at = expires_at
        await db.flush()
        await self.set_cached_session(
            record.session_key,
            {
                "id": record.id,
                "sessionKey": record.session_key,
                "lastContextHash": record.last_context_hash,
                "expiresAt": expires_at.isoformat(),
            },
        )

    async def persist_snapshot(
        self,
        db: AsyncSession,
        *,
        context: AgentContext,
        session_record: ContextSession,
        trace_id: str | None,
        source: str,
    ) -> None:
        snapshot = ContextSnapshot(
            id=str(uuid.uuid4()),
            session_id=session_record.id,
            scope_key=context.scope_key,
            trace_id=trace_id,
            user_id=str(context.user.id) if context.user.id is not None else None,
            workspace_id=context.workspace.id,
            conversation_id=context.conversation.id,
            summary=context.summary.model_dump(mode="json", by_alias=True),
            snapshot=context.model_dump(mode="json", by_alias=True),
            compression_meta=context.conversation.compression.model_dump(mode="json", by_alias=True),
            source=source,
        )
        db.add(snapshot)
        await db.flush()


context_repository = ContextRepository()
