from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context_engine.models import (
    AgentContext,
    ContextBuildInput,
    ContextSummary,
    ContextWindow,
    ConversationCompression,
    ConversationContext,
    ConversationTurn,
    EntityContext,
    SessionContext,
    TableContext,
    ViewContext,
    WorkflowContext,
)
from app.context_engine.repository import context_repository
from app.core.config import settings
from app.models.ai_conversation import AiConversation
from app.models.ai_message import AiMessage
from app.models.smart_table import TableField, TableRecord, TableView
from app.models.user import User
from app.models.workspace_member import Workspace, WorkspaceMember
from app.services.smart_table_store import get_full_store_for_table
from app.services.row_permissions import (
    allowed_record_ids,
    get_row_permission_policy,
    row_permission_restricts_user,
)
from app.services.workspace import (
    get_effective_permission_for_item,
    permission_allows,
)


def _truncate_text(value: str | None, limit: int = 240) -> str:
    text = (value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 15] + "...(truncated)"


def _safe_json_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item is not None]
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except Exception:
            return [value]
        if isinstance(parsed, list):
            return [str(item) for item in parsed if item is not None]
    return []


class ContextBuilder:
    async def build(
        self,
        db: AsyncSession,
        build_input: ContextBuildInput,
        *,
        trace_id: str | None = None,
    ) -> AgentContext:
        scope_key = context_repository.build_scope_key(build_input)
        cache_key = context_repository.build_cache_key(build_input)
        cached = await context_repository.get_cached_context(cache_key)
        if cached:
            cached.session.cache_hit = True
            return cached

        session_record, session_cache_hit = await context_repository.load_or_create_session(
            db,
            build_input,
            scope_key=scope_key,
        )

        user_context = await self._build_user_context(db, build_input.user_id)
        workspace_context = await self._build_workspace_context(
            db,
            build_input.workspace_id,
            build_input.user_id,
        )
        table_context = await self._build_table_context(
            db,
            build_input.table_ids,
            build_input.user_id,
        )
        view_context = await self._build_view_context(db, build_input.view_id, table_context.id)
        conversation_context = await self._build_conversation_context(
            db,
            build_input.conversation_id,
        )
        summary = self._build_summary(
            build_input,
            workspace_context=workspace_context,
            table_context=table_context,
            view_context=view_context,
            conversation_context=conversation_context,
        )
        window = self._build_window(cache_key, summary, conversation_context)

        context = AgentContext(
            scopeKey=scope_key,
            user=user_context,
            project=EntityContext(id=build_input.project_id, metadata=build_input.metadata.get("project") or {}),
            workspace=workspace_context,
            table=table_context,
            task=EntityContext(id=build_input.task_id, metadata=build_input.metadata.get("task") or {}),
            team=EntityContext(id=build_input.team_id, metadata=build_input.metadata.get("team") or {}),
            organization=EntityContext(
                id=build_input.organization_id,
                metadata=build_input.metadata.get("organization") or {},
            ),
            view=view_context,
            session=SessionContext(
                id=session_record.id,
                sessionKey=session_record.session_key,
                cacheHit=session_cache_hit,
                summary=session_record.memory_summary or summary.text,
                state=session_record.session_state or {},
                expiresAt=session_record.expires_at.isoformat() if session_record.expires_at else None,
            ),
            conversation=conversation_context,
            workflow=WorkflowContext(id=build_input.workflow_id),
            agents=build_input.agent_stack,
            summary=summary,
            window=window,
            generatedAt=datetime.now(timezone.utc),
        )

        await context_repository.persist_session(db, session_record, context=context, cache_key=cache_key)
        await context_repository.persist_snapshot(
            db,
            context=context,
            session_record=session_record,
            trace_id=trace_id,
            source=build_input.source,
        )
        await db.commit()
        await context_repository.set_cached_context(cache_key, context)
        return context

    async def _build_user_context(self, db: AsyncSession, user_id: int | None) -> EntityContext:
        if user_id is None:
            return EntityContext()
        result = await db.execute(select(User).where(User.id == user_id).limit(1))
        user = result.scalars().first()
        if not user:
            return EntityContext(id=str(user_id))
        return EntityContext(
            id=str(user.id),
            name=user.name,
            metadata={"email": user.email},
        )

    async def _build_workspace_context(
        self,
        db: AsyncSession,
        workspace_id: str | None,
        user_id: int | None,
    ) -> EntityContext:
        if not workspace_id:
            return EntityContext()

        result = await db.execute(select(Workspace).where(Workspace.id == workspace_id).limit(1))
        workspace = result.scalars().first()
        role = None
        member_count = None
        if user_id is not None:
            role_result = await db.execute(
                select(WorkspaceMember).where(
                    WorkspaceMember.workspace_id == workspace_id,
                    WorkspaceMember.user_id == user_id,
                )
            )
            membership = role_result.scalars().first()
            if membership and membership.role is not None:
                role = getattr(membership.role, "value", str(membership.role))
        count_result = await db.execute(
            select(func.count()).select_from(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id
            )
        )
        member_count = int(count_result.scalar() or 0)
        return EntityContext(
            id=workspace_id,
            name=workspace.name if workspace else None,
            role=role,
            metadata={"memberCount": member_count},
        )

    async def _build_table_context(
        self,
        db: AsyncSession,
        table_ids: list[str],
        user_id: int | None,
    ) -> TableContext:
        table_id = table_ids[0] if table_ids else None
        if not table_id:
            return TableContext()

        backend = (settings.DATA_BACKEND or "").lower()
        fields: list[Any] = []
        views: list[Any] = []
        record_count = 0
        if backend in {"db", "database", "postgres", "sqlite"}:
            if user_id is None:
                raise PermissionError("Authentication required for table context")

            table_permission = await get_effective_permission_for_item(
                db,
                user_id,
                table_id,
            )
            if not permission_allows(table_permission, "read"):
                raise PermissionError("No access to target table")

            fields_result = await db.execute(
                select(TableField).where(TableField.table_id == table_id).order_by(TableField.order_index.asc())
            )
            views_result = await db.execute(
                select(TableView).where(TableView.table_id == table_id).order_by(TableView.name.asc())
            )
            fields = fields_result.scalars().all()
            views = views_result.scalars().all()

            row_policy = await get_row_permission_policy(db, table_id)
            if row_permission_restricts_user(row_policy, table_permission):
                visible_ids = await allowed_record_ids(
                    db,
                    table_id,
                    user_id=user_id,
                    table_permission=table_permission,
                    policy=row_policy,
                )
                record_count = len(visible_ids)
            else:
                records_count_result = await db.execute(
                    select(func.count()).select_from(TableRecord).where(
                        TableRecord.table_id == table_id
                    )
                )
                record_count = int(records_count_result.scalar() or 0)
        else:
            store = await get_full_store_for_table(table_id)
            fields = list(store.get("fields", []))
            views = list(store.get("views", []))
            record_count = len(store.get("records", []))

        return TableContext(
            id=table_id,
            fieldCount=len(fields),
            recordCount=record_count,
            sampleFields=[
                {
                    "id": getattr(field, "id", None) or field.get("id"),
                    "name": getattr(field, "name", None) or field.get("name"),
                    "type": getattr(field, "type", None) or field.get("type"),
                }
                for field in fields[: min(len(fields), 12)]
            ],
            sampleViews=[
                {
                    "id": getattr(view, "id", None) or view.get("id"),
                    "name": getattr(view, "name", None) or view.get("name"),
                    "type": getattr(view, "type", None) or view.get("type"),
                }
                for view in views[:6]
            ],
            metadata={"tableIds": table_ids},
        )

    async def _build_view_context(
        self,
        db: AsyncSession,
        view_id: str | None,
        table_id: str | None,
    ) -> ViewContext:
        if not view_id:
            return ViewContext(tableId=table_id)

        stmt = select(TableView).where(TableView.id == view_id)
        if table_id:
            stmt = stmt.where(TableView.table_id == table_id)
        result = await db.execute(stmt.limit(1))
        view = result.scalars().first()
        if not view:
            return ViewContext(id=view_id, tableId=table_id)
        return ViewContext(
            id=view.id,
            name=view.name,
            type=view.type,
            tableId=view.table_id,
            config=view.config or {},
        )

    async def _build_conversation_context(
        self,
        db: AsyncSession,
        conversation_id: str | None,
    ) -> ConversationContext:
        if not conversation_id:
            return ConversationContext()

        conversation_result = await db.execute(
            select(AiConversation).where(AiConversation.id == conversation_id).limit(1)
        )
        conversation = conversation_result.scalars().first()

        messages_result = await db.execute(
            select(AiMessage)
            .where(AiMessage.conversation_id == conversation_id)
            .order_by(AiMessage.created_at.asc())
        )
        messages = messages_result.scalars().all()
        compression = self._compress_messages(messages)
        recent_turns = [
            ConversationTurn(
                role=item["role"],
                content=item["content"],
                createdAt=item["createdAt"],
                tableIds=item["tableIds"],
            )
            for item in compression["recentTurns"]
        ]
        return ConversationContext(
            id=conversation_id,
            title=conversation.title if conversation else None,
            recentTurns=recent_turns,
            summary=compression["summary"],
            messageCount=len(messages),
            compression=ConversationCompression.model_validate(compression["compression"]),
        )

    def _compress_messages(self, messages: list[AiMessage]) -> dict[str, Any]:
        total_messages = len(messages)
        serialized: list[dict[str, Any]] = []
        for message in messages:
            serialized.append(
                {
                    "role": message.role,
                    "content": (message.content or "").strip(),
                    "createdAt": message.created_at.isoformat() if message.created_at else None,
                    "tableIds": _safe_json_list(getattr(message, "table_ids", None)),
                }
            )

        original_chars = sum(len(item["content"]) for item in serialized)
        keep_count = min(settings.CONTEXT_MAX_CONVERSATION_MESSAGES, len(serialized))
        recent_turns = serialized[-keep_count:]
        compressed_turns = serialized[:-keep_count]

        summary_lines: list[str] = []
        for item in compressed_turns[-8:]:
            if not item["content"]:
                continue
            summary_lines.append(f"[{item['role']}] {_truncate_text(item['content'], 140)}")
        if not summary_lines and recent_turns:
            summary_lines.append(f"[最近用户消息] {_truncate_text(recent_turns[-1]['content'], 180)}")
        summary = "\n".join(summary_lines)
        compressed_chars = len(summary) + sum(len(item["content"]) for item in recent_turns)
        return {
            "recentTurns": recent_turns,
            "summary": summary,
            "compression": {
                "totalMessages": total_messages,
                "keptMessages": len(recent_turns),
                "compressedMessages": len(compressed_turns),
                "originalChars": original_chars,
                "compressedChars": compressed_chars,
                "strategy": "tail-window+heuristic-summary",
            },
        }

    def _build_summary(
        self,
        build_input: ContextBuildInput,
        *,
        workspace_context: EntityContext,
        table_context: TableContext,
        view_context: ViewContext,
        conversation_context: ConversationContext,
    ) -> ContextSummary:
        highlights = [
            f"user={build_input.user_id or 'guest'}",
            f"workspace={workspace_context.id or 'unknown'}",
            f"table={table_context.id or 'unknown'}",
            f"view={view_context.id or 'none'}",
            f"conversation={conversation_context.id or 'none'}",
        ]
        if build_input.task_id:
            highlights.append(f"task={build_input.task_id}")
        if build_input.workflow_id:
            highlights.append(f"workflow={build_input.workflow_id}")
        text = (
            f"当前用户是 {build_input.user_id or 'guest'}；"
            f"当前工作区是 {workspace_context.name or workspace_context.id or 'unknown'}；"
            f"当前表是 {table_context.id or 'unknown'}，字段数 {table_context.field_count}；"
            f"当前视图是 {view_context.name or view_context.id or '未指定'}；"
            f"当前会话是 {build_input.session_id or build_input.conversation_id or '临时会话'}。"
        )
        if conversation_context.summary:
            text += f"\n历史会话摘要:\n{conversation_context.summary}"
        return ContextSummary(
            text=text,
            highlights=highlights,
            sources=[
                "user",
                "workspace",
                "table",
                "view",
                "conversation",
                "session",
            ],
        )

    def _build_window(
        self,
        cache_key: str,
        summary: ContextSummary,
        conversation_context: ConversationContext,
    ) -> ContextWindow:
        used_chars = len(summary.text) + sum(len(item.content) for item in conversation_context.recent_turns)
        target_chars = settings.CONTEXT_COMPRESSION_CHAR_BUDGET
        remaining = max(0, target_chars - used_chars)
        return ContextWindow(
            targetChars=target_chars,
            usedChars=used_chars,
            remainingChars=remaining,
            cacheKey=cache_key,
        )


context_builder = ContextBuilder()
