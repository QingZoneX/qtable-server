import logging
import json
import uuid
from typing import Any
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.agent_runtime import (
    SSEEventEmitter,
    get_context_builder,
    get_runtime_executor,
)
from app.context_engine import ContextBuildInput, context_injector
from app.core.config import settings
from app.core.security import decode_access_token
from app.db.session import AsyncSessionLocal
from app.models.ai_config import AiConfig
from app.models.ai_conversation import AiConversation
from app.models.ai_message import AiMessage
from app.services.ai_service import analyze_table_data
from app.services.ai_tool_router import ToolContextState, ToolRouterRequest, ToolRouterResponse, tool_router_service
from app.services.smart_table_store import get_full_store, get_full_store_for_table
from app.services.row_permissions import filter_store_for_user
from app.services.workspace import (
    get_effective_permission_for_item,
    permission_allows,
)

router = APIRouter(prefix="/api/ai", tags=["ai"])
logger = logging.getLogger(__name__)


def _sse_event(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _iter_text_chunks(text: str, chunk_size: int = 1):
    if not text:
        return
    for index in range(0, len(text), chunk_size):
        yield text[index : index + chunk_size]


async def _get_current_user_id(request: Request) -> int:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.lower().startswith("bearer "):
        return None
    token = auth_header.split(" ", 1)[1]
    try:
        payload = decode_access_token(token)
        return payload.get("user_id")
    except Exception:
        return None


async def _get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def _load_ai_config(db: AsyncSession, user_id: int) -> AiConfig | None:
    config_result = await db.execute(
        select(AiConfig).where(AiConfig.user_id == str(user_id))
    )
    return config_result.scalars().first()


def _merge_table_ids(table_id: str | None, table_ids: list[str] | None) -> list[str]:
    merged = list(table_ids or [])
    if table_id and table_id not in merged:
        merged = [table_id, *merged]
    if not merged and table_id:
        merged = [table_id]
    return merged


def _get_context_headers(request: Request) -> dict[str, str | None]:
    return {
        "workspaceId": request.headers.get("X-Workspace-Id"),
        "sessionId": request.headers.get("X-Session-Id"),
        "projectId": request.headers.get("X-Project-Id"),
        "viewId": request.headers.get("X-View-Id"),
        "taskId": request.headers.get("X-Task-Id"),
        "teamId": request.headers.get("X-Team-Id"),
        "organizationId": request.headers.get("X-Organization-Id"),
        "workflowId": request.headers.get("X-Workflow-Id"),
        "agentId": request.headers.get("X-Agent-Id"),
    }


async def _load_table_snapshot(
    db: AsyncSession,
    table_ids: list[str],
    *,
    user_id: int,
):
    """Load only table rows the authenticated user may read.

    This snapshot is sent directly to the fallback AI analysis path, so it must
    enforce both table-level access and row-level visibility before applying
    the 100-record prompt limit.
    """
    backend = (settings.DATA_BACKEND or "").lower()
    all_records = []
    all_fields = []

    for table_id in table_ids:
        if not table_id:
            continue

        if backend in {"db", "database", "postgres", "sqlite"}:
            permission = await get_effective_permission_for_item(
                db,
                user_id,
                table_id,
            )
            if not permission_allows(permission, "read"):
                raise PermissionError("No access to target table")

            store = await get_full_store(db, table_id)
            store, _ = await filter_store_for_user(
                db,
                table_id,
                store,
                user_id=user_id,
                table_permission=permission,
            )
        else:
            # Row-level permissions are a database-backed feature. Preserve the
            # legacy file-backend behavior without pretending it has DB ACLs.
            store = await get_full_store_for_table(table_id)

        records = [
            {**record, "_table_id": table_id}
            for record in store.get("records", [])[:100]
        ]
        fields = [
            {**field, "_table_id": table_id}
            for field in store.get("fields", [])
        ]

        all_records.extend(records)
        all_fields.extend(fields)

    if all_records or all_fields or table_ids:
        return all_records, all_fields

    # Do not silently expose an arbitrary default DB table when no table scope
    # was supplied. File mode retains its historic local-development fallback.
    if backend in {"db", "database", "postgres", "sqlite"}:
        return [], []

    store = await get_full_store_for_table(None)
    return store.get("records", [])[:100], store.get("fields", [])


async def _persist_assistant_message(
    *,
    user_id: int,
    conversation_id: str,
    content: str,
    table_ids: list[str],
):
    if not content:
        return

    async with AsyncSessionLocal() as session:
        conv_result = await session.execute(
            select(AiConversation).where(
                AiConversation.id == conversation_id,
                AiConversation.user_id == str(user_id),
            )
        )
        persisted_conversation = conv_result.scalars().first()
        if not persisted_conversation:
            return

        assistant_message = AiMessage(
            id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            role="assistant",
            content=content,
            table_ids=json.dumps(table_ids) if table_ids else None,
            created_at=datetime.now(timezone.utc),
        )
        session.add(assistant_message)
        persisted_conversation.updated_at = datetime.now(timezone.utc)
        await session.commit()


def _is_tool_calling_unsupported_response(response) -> bool:
    if not response:
        return False
    if getattr(response, "status", None) != "failed":
        return False
    answer = getattr(response, "answer", None)
    error = getattr(response, "error", None) or {}
    error_message = error.get("message") if isinstance(error, dict) else None
    return tool_router_service.is_tool_calling_unsupported_error(answer) or tool_router_service.is_tool_calling_unsupported_error(error_message)


@router.post("/chat")
async def chat(
    request: Request,
    user_id: int = Depends(_get_current_user_id),
):
    """统一的AI聊天接口，支持CHAT和AGENT两种模式。"""
    if not user_id:
        return StreamingResponse(
            iter([_sse_event({"type": "error", "error": True, "message": "Unauthorized", "code": "UNAUTHORIZED"})]),
            media_type="text/event-stream",
        )

    body = await request.json()
    mode = body.get("mode", "chat")  # chat | agent, keep ask for compatibility

    if mode in {"chat", "ask"}:
        return await handle_chat_mode(request, body, user_id)
    elif mode == "agent":
        return await handle_agent_mode(request, body, user_id)
    else:
        return StreamingResponse(
            iter([_sse_event({"type": "error", "error": True, "message": "Invalid mode", "code": "INVALID_MODE"})]),
            media_type="text/event-stream",
        )


async def handle_chat_mode(request: Request, body: dict, user_id: int):
    """处理CHAT模式 - 基于 Agent Runtime 的聊天与工具调用。"""
    table_id = body.get("tableId")
    table_ids = _merge_table_ids(table_id, body.get("tableIds", []))
    model = body.get("model")
    question = body.get("question", "")
    conversation_id = body.get("conversationId")
    context_headers = _get_context_headers(request)
    workspace_id = context_headers["workspaceId"]
    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as db:
        config = await _load_ai_config(db, user_id)
        if not config:
            return StreamingResponse(
                iter([_sse_event({"type": "error", "error": True, "message": "AI configuration not found", "code": "AI_CONFIG_NOT_FOUND"})]),
                media_type="text/event-stream",
            )

        if conversation_id:
            conv_result = await db.execute(
                select(AiConversation).where(
                    AiConversation.id == conversation_id,
                    AiConversation.user_id == str(user_id),
                )
            )
            conversation = conv_result.scalars().first()
            if not conversation:
                conversation_id = None

        if not conversation_id:
            table_ids_json = json.dumps(table_ids) if table_ids else None
            conv = AiConversation(
                id=str(uuid.uuid4()),
                user_id=str(user_id),
                table_id=table_id,
                table_ids=table_ids_json,
                title=question[:50] or "New Conversation",
                created_at=now,
                updated_at=now,
            )
            db.add(conv)
            await db.commit()
            conversation = conv
            conversation_id = conv.id

        msgs_result = await db.execute(
            select(AiMessage)
            .where(AiMessage.conversation_id == conversation_id)
            .order_by(AiMessage.created_at.asc())
        )
        history = [
            {"role": m.role, "content": m.content}
            for m in msgs_result.scalars().all()
        ]

        user_message = AiMessage(
            id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            role="user",
            content=question,
            table_ids=json.dumps(table_ids) if table_ids else None,
            created_at=now,
        )
        db.add(user_message)
        conversation.updated_at = now
        await db.commit()

    requested_model = model or config.model
    prefer_plain_chat = not tool_router_service.model_supports_tool_calling(requested_model)

    async def generate():
        assistant_content = ""
        try:
            async with AsyncSessionLocal() as session:
                if prefer_plain_chat:
                    logger.info(
                        "AI chat falling back to plain analysis for unsupported tool-calling model user_id=%s conversation_id=%s model=%s",
                        user_id,
                        conversation_id,
                        requested_model,
                    )
                    all_records, all_fields = await _load_table_snapshot(session, table_ids, user_id=user_id)
                    async for chunk in analyze_table_data(
                        api_key_encrypted=config.api_key_encrypted,
                        model=requested_model,
                        table_records=all_records,
                        table_fields=all_fields,
                        question=question,
                        conversation_history=history,
                    ):
                        payload = json.loads(chunk.strip())
                        content = payload.get("content")
                        if content:
                            assistant_content += content
                            yield _sse_event({"type": "content", "content": content})
                else:
                    try:
                        router_request = ToolRouterRequest(
                            message=question,
                            model=model,
                            sessionId=context_headers["sessionId"],
                            conversationId=conversation_id,
                            workspaceId=workspace_id,
                            projectId=context_headers["projectId"],
                            tableIds=table_ids,
                            viewId=context_headers["viewId"],
                            taskId=context_headers["taskId"],
                            teamId=context_headers["teamId"],
                            organizationId=context_headers["organizationId"],
                            workflowId=context_headers["workflowId"],
                            agentId=context_headers["agentId"],
                            conversation=history,
                            toolContext=ToolContextState(
                                sessionId=context_headers["sessionId"],
                                conversationId=conversation_id,
                                workspaceId=workspace_id,
                                projectId=context_headers["projectId"],
                                tableIds=table_ids,
                                viewId=context_headers["viewId"],
                                taskId=context_headers["taskId"],
                                teamId=context_headers["teamId"],
                                organizationId=context_headers["organizationId"],
                                workflow={"id": context_headers["workflowId"]} if context_headers["workflowId"] else {},
                                toolResults=[],
                                variables={},
                                notes=[],
                                chainDepth=0,
                                retryPolicy={
                                    "maxAttempts": 2,
                                    "backoffMs": 350,
                                },
                            ),
                            confirmed=False,
                            locale="zh-CN",
                            timezone="Asia/Shanghai",
                        )
                        injected_request, agent_context = await context_injector.build_and_inject(
                            db=session,
                            request=router_request,
                            build_input=ContextBuildInput(
                                source="api.ai.chat",
                                userId=user_id,
                                sessionId=context_headers["sessionId"],
                                conversationId=conversation_id,
                                workspaceId=workspace_id,
                                projectId=context_headers["projectId"],
                                tableIds=table_ids,
                                viewId=context_headers["viewId"],
                                taskId=context_headers["taskId"],
                                teamId=context_headers["teamId"],
                                organizationId=context_headers["organizationId"],
                                workflowId=context_headers["workflowId"],
                                agentId=context_headers["agentId"],
                                message=question,
                                locale="zh-CN",
                                timezone="Asia/Shanghai",
                            ),
                        )
                        response = await tool_router_service.run(
                            db=session,
                            user_id=user_id,
                            request=injected_request,
                            agent_context=agent_context,
                        )
                    except Exception as exc:
                        if not tool_router_service.is_tool_calling_unsupported_error(str(exc)):
                            raise
                        logger.warning(
                            "AI chat detected unsupported tool-calling response and fell back to plain analysis user_id=%s conversation_id=%s model=%s error=%s",
                            user_id,
                            conversation_id,
                            requested_model,
                            str(exc),
                        )
                        all_records, all_fields = await _load_table_snapshot(session, table_ids, user_id=user_id)
                        async for chunk in analyze_table_data(
                            api_key_encrypted=config.api_key_encrypted,
                            model=requested_model,
                            table_records=all_records,
                            table_fields=all_fields,
                            question=question,
                            conversation_history=history,
                        ):
                            payload = json.loads(chunk.strip())
                            content = payload.get("content")
                            if content:
                                assistant_content += content
                                yield _sse_event({"type": "content", "content": content})
                    else:
                        if _is_tool_calling_unsupported_response(response):
                            logger.warning(
                                "AI chat detected unsupported tool-calling failed response and fell back to plain analysis user_id=%s conversation_id=%s model=%s answer=%s",
                                user_id,
                                conversation_id,
                                requested_model,
                                response.answer,
                            )
                            all_records, all_fields = await _load_table_snapshot(session, table_ids, user_id=user_id)
                            async for chunk in analyze_table_data(
                                api_key_encrypted=config.api_key_encrypted,
                                model=requested_model,
                                table_records=all_records,
                                table_fields=all_fields,
                                question=question,
                                conversation_history=history,
                            ):
                                payload = json.loads(chunk.strip())
                                content = payload.get("content")
                                if content:
                                    assistant_content += content
                                    yield _sse_event({"type": "content", "content": content})
                            await _persist_assistant_message(
                                user_id=user_id,
                                conversation_id=conversation_id,
                                content=assistant_content.strip(),
                                table_ids=table_ids,
                            )
                            yield _sse_event({"type": "done", "done": True})
                            return

                        assistant_content = (response.answer or "").strip()
                        for chunk in _iter_text_chunks(assistant_content):
                            yield _sse_event({"type": "content", "content": chunk})

                        yield _sse_event(
                            {
                                "type": "result",
                                "status": response.status,
                                "answer": response.answer,
                                "provider": response.provider,
                                "model": response.model,
                                "traceId": response.trace_id,
                                "steps": response.steps,
                                "toolCalls": [item.model_dump(mode="json", by_alias=True) for item in response.tool_calls],
                                "toolContext": response.tool_context.model_dump(mode="json", by_alias=True),
                                "pendingConfirmation": response.pending_confirmation,
                                "error": response.error,
                            }
                        )

            await _persist_assistant_message(
                user_id=user_id,
                conversation_id=conversation_id,
                content=assistant_content.strip(),
                table_ids=table_ids,
            )
            yield _sse_event({"type": "done", "done": True})
        except ValueError as exc:
            logger.warning(
                "AI chat configuration error user_id=%s conversation_id=%s message=%s",
                user_id,
                conversation_id,
                str(exc),
            )
            yield _sse_event(
                {
                    "type": "error",
                    "error": True,
                    "message": str(exc) or "AI configuration not found",
                    "code": "AI_CONFIG_NOT_FOUND",
                }
            )
        except Exception as exc:
            logger.exception(
                "AI chat runtime failed user_id=%s conversation_id=%s workspace_id=%s",
                user_id,
                conversation_id,
                workspace_id,
            )
            yield _sse_event(
                {
                    "type": "error",
                    "error": True,
                    "message": str(exc) or "AI 请求失败",
                    "code": "AI_REQUEST_FAILED",
                }
            )

    return StreamingResponse(generate(), media_type="text/event-stream")


async def _run_agent_core(
    db: AsyncSession,
    user_id: int,
    user_request: str,
    table_ids: list[str],
    model: str | None,
    context_headers: dict[str, str | None],
    conversation_id: str | None = None,
) -> tuple[ToolRouterResponse, dict[str, Any]]:
    """Agent 模式核心执行逻辑，通过 ToolRouter 通用路由到所有可用技能。"""
    workspace_id = context_headers["workspaceId"]

    router_request = ToolRouterRequest(
        message=user_request,
        model=model,
        sessionId=context_headers["sessionId"],
        conversationId=conversation_id,
        workspaceId=workspace_id,
        projectId=context_headers["projectId"],
        tableIds=table_ids,
        viewId=context_headers["viewId"],
        taskId=context_headers["taskId"],
        teamId=context_headers["teamId"],
        organizationId=context_headers["organizationId"],
        workflowId=context_headers["workflowId"],
        agentId=context_headers["agentId"],
        toolContext=ToolContextState(
            sessionId=context_headers["sessionId"],
            conversationId=conversation_id,
            workspaceId=workspace_id,
            projectId=context_headers["projectId"],
            tableIds=table_ids,
            viewId=context_headers["viewId"],
            taskId=context_headers["taskId"],
            teamId=context_headers["teamId"],
            organizationId=context_headers["organizationId"],
            workflow={"id": context_headers["workflowId"]} if context_headers["workflowId"] else {},
            notes=[
                "创建记录时，values 的 key 必须是字段的 id（从 qtable.table.describe 获取），不能使用字段 name 或自创字段名。",
                "date 类型字段的值必须是毫秒时间戳整数，不能使用 ISO 字符串（如 '2026-05-25'）。",
                "member 类型字段的值必须是数组格式（如 ['u1']），不能使用单字符串。",
                "select 类型字段的值必须使用选项的 id（如 'opt1'），不能使用 label。",
                "创建记录请使用 qtable.record.create 工具。",
            ],
        ),
        confirmed=False,
        locale="zh-CN",
        timezone="Asia/Shanghai",
    )

    injected_request, agent_context = await context_injector.build_and_inject(
        db=db,
        request=router_request,
        build_input=ContextBuildInput(
            source="api.ai.agent",
            userId=user_id,
            sessionId=context_headers["sessionId"],
            conversationId=conversation_id,
            workspaceId=workspace_id,
            projectId=context_headers["projectId"],
            tableIds=table_ids,
            viewId=context_headers["viewId"],
            taskId=context_headers["taskId"],
            teamId=context_headers["teamId"],
            organizationId=context_headers["organizationId"],
            workflowId=context_headers["workflowId"],
            agentId=context_headers["agentId"],
            message=user_request,
            locale="zh-CN",
            timezone="Asia/Shanghai",
        ),
    )

    response = await tool_router_service.run(
        db=db,
        user_id=user_id,
        request=injected_request,
        agent_context=agent_context,
    )

    return response, _build_agent_response(table_ids, response)


def _extract_record_data_from_pending(pending: dict[str, Any]) -> Any:
    """从 pending_confirmation 的 arguments 中智能提取记录数据。

    不同工具的参数结构不同，常见的数据承载键名包括：
    - values: 通用记录创建
    - taskPlan: 批量任务创建
    - tasks: 简化任务列表
    - nodes: 任务拆分节点
    - records: 通用记录批量
    """
    if not isinstance(pending, dict):
        return None
    arguments = pending.get("arguments")
    if not isinstance(arguments, dict):
        return None

    # 常见的数据键名，按优先级尝试
    data_keys = ("values", "taskPlan", "tasks", "nodes", "records")
    for key in data_keys:
        data = arguments.get(key)
        if data is not None:
            return data
    return None


def _build_agent_response(table_ids: list[str], response: ToolRouterResponse) -> dict[str, Any]:
    """将 ToolRouterResponse 转换为 Agent 接口通用的 JSON 响应。"""
    target_table_id = table_ids[0] if table_ids else None
    pending = response.pending_confirmation or {}
    record_data = _extract_record_data_from_pending(pending)

    return {
        "success": response.status == "completed" or response.status == "requires_confirmation",
        "data": record_data or None,
        "tableId": target_table_id,
        "message": response.answer or "Agent execution completed",
        "requiresConfirmation": bool(response.pending_confirmation),
        "toolTraceId": response.trace_id,
        "toolStatus": response.status,
        "toolCalls": [item.model_dump(mode="json", by_alias=True) for item in response.tool_calls],
        "toolContext": response.tool_context.model_dump(mode="json", by_alias=True),
        "pendingConfirmation": response.pending_confirmation,
        "provider": response.provider,
        "model": response.model,
        "error": response.error,
    }


async def handle_agent_mode(request: Request, body: dict, user_id: int):
    """处理AGENT模式 - 通过 Chat SSE 接口执行 Agent 操作."""
    table_id = body.get("tableId")
    table_ids = _merge_table_ids(table_id, body.get("tableIds", []))
    model = body.get("model")
    user_request = body.get("question", "")
    conversation_id = body.get("conversationId")
    context_headers = _get_context_headers(request)

    if not table_ids or not user_request:
        return JSONResponse(
            status_code=400,
            content={"error": True, "message": "Table ID and request are required", "code": "INVALID_REQUEST"}
        )

    try:
        async with AsyncSessionLocal() as db:
            config = await _load_ai_config(db, user_id)
            if not config:
                return JSONResponse(
                    status_code=400,
                    content={"error": True, "message": "AI configuration not found", "code": "AI_CONFIG_NOT_FOUND"}
                )
            requested_model = model or config.model
            if not tool_router_service.model_supports_tool_calling(requested_model):
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": True,
                        "message": f"当前模型 {requested_model} 不支持 Agent 工具调用，请切换到支持工具调用的模型，例如 deepseek-chat。",
                        "code": "MODEL_UNSUPPORTED_FOR_AGENT",
                    }
                )

            _, response_data = await _run_agent_core(
                db=db,
                user_id=user_id,
                user_request=user_request,
                table_ids=table_ids,
                model=model,
                context_headers=context_headers,
                conversation_id=conversation_id,
            )

        return JSONResponse(status_code=200, content=response_data)
    except Exception as exc:
        import traceback
        error_traceback = traceback.format_exc()
        logger.exception("AI agent error user_id=%s", user_id)

        return JSONResponse(
            status_code=500,
            content={
                "error": True,
                "message": str(exc) or "AI request failed",
                "code": "AI_REQUEST_FAILED",
                "details": error_traceback if settings.DEBUG else None
            }
        )


@router.post("/analyze")
async def analyze(
    request: Request,
    user_id: int = Depends(_get_current_user_id),
):
    if not user_id:
        error_data = json.dumps({"error": True, "message": "Unauthorized", "code": "UNAUTHORIZED"})
        return StreamingResponse(
            iter([f"data: {error_data}\n\n"]),
            media_type="text/event-stream",
        )

    body = await request.json()
    table_id = body.get("tableId")
    table_ids = body.get("tableIds", [])
    model = body.get("model")
    question = body.get("question", "")
    conversation_id = body.get("conversationId")
    now = datetime.now(timezone.utc)

    # 合并 table_id 和 table_ids
    if table_id and table_id not in table_ids:
        table_ids = [table_id] + table_ids
    if not table_ids:
        table_ids = [table_id] if table_id else []

    async with AsyncSessionLocal() as db:
        config_result = await db.execute(
            select(AiConfig).where(AiConfig.user_id == str(user_id))
        )
        config = config_result.scalars().first()
        if not config:
            error_data = json.dumps({"error": True, "message": "API Key not configured", "code": "API_KEY_MISSING"})
            return StreamingResponse(
                iter([f"data: {error_data}\n\n"]),
                media_type="text/event-stream",
            )

        if conversation_id:
            conv_result = await db.execute(
                select(AiConversation).where(
                    AiConversation.id == conversation_id,
                    AiConversation.user_id == str(user_id),
                )
            )
            conversation = conv_result.scalars().first()
            if not conversation:
                conversation_id = None

        if not conversation_id:
            import json
            # 创建新对话时保存所有关联的表 ID
            table_ids_json = json.dumps(table_ids) if table_ids else None
            conv = AiConversation(
                id=str(uuid.uuid4()),
                user_id=str(user_id),
                table_id=table_id,
                table_ids=table_ids_json,
                title=question[:50] or "New Conversation",
                created_at=now,
                updated_at=now,
            )
            db.add(conv)
            await db.commit()
            conversation = conv
            conversation_id = conv.id

        msgs_result = await db.execute(
            select(AiMessage)
            .where(AiMessage.conversation_id == conversation_id)
            .order_by(AiMessage.created_at.asc())
        )
        history = [
            {"role": m.role, "content": m.content}
            for m in msgs_result.scalars().all()
        ]

        # 保存用户消息，记录关联的数据表
        import json
        user_message = AiMessage(
            id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            role="user",
            content=question,
            table_ids=json.dumps(table_ids) if table_ids else None,
            created_at=now,
        )
        db.add(user_message)
        conversation.updated_at = now
        await db.commit()

        # 获取所有关联表的数据
        from app.core.config import settings
        backend = (settings.DATA_BACKEND or "").lower()
        
        all_records = []
        all_fields = []
        
        for tid in table_ids:
            if not tid:
                continue
            if backend in {"db", "database", "postgres", "sqlite"}:
                store = await get_full_store(db, tid)
            else:
                store = await get_full_store_for_table(tid)
            
            records = store.get("records", [])[:100]
            fields = store.get("fields", [])
            
            # 为记录添加表标识
            for record in records:
                record["_table_id"] = tid
            for field in fields:
                field["_table_id"] = tid
            
            all_records.extend(records)
            all_fields.extend(fields)

        # 如果没有指定表，使用默认表
        if not table_ids:
            if backend in {"db", "database", "postgres", "sqlite"}:
                store = await get_full_store(db, "dstDefault")
            else:
                store = await get_full_store_for_table(None)
            all_records = store.get("records", [])[:100]
            all_fields = store.get("fields", [])

    async def generate():
        assistant_chunks = []
        try:
            async for chunk in analyze_table_data(
                api_key_encrypted=config.api_key_encrypted,
                model=model or config.model,
                table_records=all_records,
                table_fields=all_fields,
                question=question,
                conversation_history=history,
            ):
                try:
                    payload = json.loads(chunk.strip())
                    content = payload.get("content")
                    if content:
                        assistant_chunks.append(content)
                except Exception:
                    pass
                yield f"data: {chunk}"

            assistant_content = "".join(assistant_chunks).strip()
            if assistant_content:
                async with AsyncSessionLocal() as session:
                    conv_result = await session.execute(
                        select(AiConversation).where(
                            AiConversation.id == conversation_id,
                            AiConversation.user_id == str(user_id),
                        )
                    )
                    persisted_conversation = conv_result.scalars().first()
                    if persisted_conversation:
                        # 保存助手消息，记录关联的数据表
                        assistant_message = AiMessage(
                            id=str(uuid.uuid4()),
                            conversation_id=conversation_id,
                            role="assistant",
                            content=assistant_content,
                            table_ids=json.dumps(table_ids) if table_ids else None,
                            created_at=datetime.now(timezone.utc),
                        )
                        session.add(assistant_message)
                        persisted_conversation.updated_at = datetime.now(timezone.utc)
                        await session.commit()
        except Exception as exc:
            error_data = json.dumps(
                {
                    "error": True,
                    "message": str(exc) or "AI 请求失败",
                    "code": "AI_REQUEST_FAILED",
                }
            )
            yield f"data: {error_data}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/agent")
async def agent_run(
    request: Request,
    user_id: int = Depends(_get_current_user_id),
):
    """Agent 模式入口 - 通过 ToolRouter 全技能路由执行操作（创建记录、修改状态、规划任务等）。"""
    if not user_id:
        return JSONResponse(
            status_code=401,
            content={"error": True, "message": "Unauthorized", "code": "UNAUTHORIZED"}
        )

    body = await request.json()
    table_id = body.get("tableId")
    table_ids = _merge_table_ids(table_id, body.get("tableIds", []))
    model = body.get("model")
    user_request = body.get("question", "")
    conversation_id = body.get("conversationId")
    auto_create = body.get("autoCreate", False)
    context_headers = _get_context_headers(request)

    if not table_ids or not user_request:
        return JSONResponse(
            status_code=400,
            content={"error": True, "message": "Table ID and request are required", "code": "INVALID_REQUEST"}
        )

    try:
        async with AsyncSessionLocal() as db:
            config = await _load_ai_config(db, user_id)
            if not config:
                return JSONResponse(
                    status_code=400,
                    content={"error": True, "message": "AI configuration not found", "code": "AI_CONFIG_NOT_FOUND"}
                )
            requested_model = model or config.model
            if not tool_router_service.model_supports_tool_calling(requested_model):
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": True,
                        "message": f"当前模型 {requested_model} 不支持 Agent 工具调用，请切换到支持工具调用的模型，例如 deepseek-chat。",
                        "code": "MODEL_UNSUPPORTED_FOR_AGENT",
                    }
                )

            _, response_data = await _run_agent_core(
                db=db,
                user_id=user_id,
                user_request=user_request,
                table_ids=table_ids,
                model=model,
                context_headers=context_headers,
                conversation_id=conversation_id,
            )

            # 将 AI 回答持久化到会话中，确保用户在对话列表中可以看到完整结果
            if conversation_id and response_data.get("message"):
                assistant_message_content = response_data["message"]
                conv_result = await db.execute(
                    select(AiConversation).where(
                        AiConversation.id == conversation_id,
                        AiConversation.user_id == str(user_id),
                    )
                )
                persisted_conv = conv_result.scalars().first()
                if persisted_conv:
                    assistant_message = AiMessage(
                        id=str(uuid.uuid4()),
                        conversation_id=conversation_id,
                        role="assistant",
                        content=assistant_message_content,
                        table_ids=json.dumps(table_ids) if table_ids else None,
                        created_at=datetime.now(timezone.utc),
                    )
                    db.add(assistant_message)
                    persisted_conv.updated_at = datetime.now(timezone.utc)
                    await db.commit()

        if auto_create:
            response_data["autoCreateIgnored"] = True
            response_data["autoCreateReason"] = "Agent 模式下需要先确认再执行，暂不支持自动创建。"

        return JSONResponse(status_code=200, content=response_data)
    except Exception as exc:
        import traceback
        error_traceback = traceback.format_exc()
        logger.exception("AI agent error user_id=%s", user_id)

        error_message = str(exc) or "AI request failed"
        error_code = "AI_REQUEST_FAILED"
        if "AI configuration not found" in error_message:
            error_code = "AI_CONFIG_NOT_FOUND"

        return JSONResponse(
            status_code=500,
            content={
                "error": True,
                "message": error_message,
                "code": error_code,
                "details": error_traceback if settings.DEBUG else None
            }
        )


@router.post("/runtime/chat")
async def runtime_chat(
    request: Request,
    user_id: int = Depends(_get_current_user_id),
):
    """统一 AI Runtime 入口 - 不再区分 chat/agent mode，由 Runtime 自动识别意图并编排执行。"""
    if not user_id:
        return StreamingResponse(
            iter([_sse_event({"type": "error", "error": True, "message": "Unauthorized", "code": "UNAUTHORIZED"})]),
            media_type="text/event-stream",
        )

    body = await request.json()
    message = body.get("message") or body.get("question", "")
    table_ids = _merge_table_ids(body.get("tableId"), body.get("tableIds", []))
    model = body.get("model")
    conversation_id = body.get("conversationId")
    context_headers = _get_context_headers(request)
    workspace_id = context_headers["workspaceId"]

    if not message:
        return StreamingResponse(
            iter([_sse_event({"type": "error", "error": True, "message": "Message is required", "code": "INVALID_REQUEST"})]),
            media_type="text/event-stream",
        )

    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as db:
        config = await _load_ai_config(db, user_id)
        if not config:
            return StreamingResponse(
                iter([_sse_event({"type": "error", "error": True, "message": "AI configuration not found", "code": "AI_CONFIG_NOT_FOUND"})]),
                media_type="text/event-stream",
            )

        if conversation_id:
            conv_result = await db.execute(
                select(AiConversation).where(
                    AiConversation.id == conversation_id,
                    AiConversation.user_id == str(user_id),
                )
            )
            conversation = conv_result.scalars().first()
            if not conversation:
                conversation_id = None

        if not conversation_id:
            table_ids_json = json.dumps(table_ids) if table_ids else None
            conv = AiConversation(
                id=str(uuid.uuid4()),
                user_id=str(user_id),
                table_id=table_ids[0] if table_ids else None,
                table_ids=table_ids_json,
                title=message[:50] or "New Conversation",
                created_at=now,
                updated_at=now,
            )
            db.add(conv)
            await db.commit()
            conversation = conv
            conversation_id = conv.id

        user_message = AiMessage(
            id=str(uuid.uuid4()),
            conversation_id=conversation_id,
            role="user",
            content=message,
            table_ids=json.dumps(table_ids) if table_ids else None,
            created_at=now,
        )
        db.add(user_message)
        db.commit()

    requested_model = model or config.model

    async def generate():
        assistant_content = ""

        try:
            async with AsyncSessionLocal() as session:
                from app.services.encryption import decrypt_api_key
                from pydantic_ai.models.openai import OpenAIChatModel as PydanticOpenAIChatModel
                from pydantic_ai.providers.openai import OpenAIProvider

                api_key = decrypt_api_key(config.api_key_encrypted)
                base_url = "https://api.deepseek.com/v1"
                if config.provider == "openai":
                    base_url = "https://api.openai.com/v1"

                pydantic_model = PydanticOpenAIChatModel(
                    model_name=requested_model,
                    provider=OpenAIProvider(api_key=api_key, base_url=base_url),
                )

                emitter = SSEEventEmitter()

                context_builder = get_context_builder()
                ctx = context_builder.build(
                    user_id=user_id,
                    user_message=message,
                    session_id=context_headers["sessionId"],
                    workspace_id=workspace_id,
                    conversation_id=conversation_id,
                    project_id=context_headers["projectId"],
                    table_ids=table_ids,
                    view_id=context_headers["viewId"],
                    task_id=context_headers["taskId"],
                    team_id=context_headers["teamId"],
                    organization_id=context_headers["organizationId"],
                    workflow_id=context_headers["workflowId"],
                    agent_id=context_headers["agentId"],
                )

                executor = get_runtime_executor()
                await executor.run(
                    ctx=ctx,
                    db=session,
                    user_id=user_id,
                    model=pydantic_model,
                    emitter=emitter,
                )

                for event_str in emitter.get_events():
                    try:
                        payload = json.loads(event_str.replace("data: ", "").strip())
                        if payload.get("type") == "text":
                            assistant_content += payload.get("content", "")
                    except Exception:
                        pass
                    yield event_str

            await _persist_assistant_message(
                user_id=user_id,
                conversation_id=conversation_id,
                content=assistant_content.strip(),
                table_ids=table_ids,
            )

        except Exception as exc:
            logger.exception(
                "AI Runtime failed user_id=%s conversation_id=%s",
                user_id,
                conversation_id,
            )
            yield _sse_event(
                {
                    "type": "error",
                    "error": True,
                    "message": str(exc) or "AI 请求失败",
                    "code": "AI_REQUEST_FAILED",
                }
            )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/runtime/confirm")
async def runtime_confirm(
    request: Request,
    user_id: int = Depends(_get_current_user_id),
):
    """确认/拒绝 Runtime 中的待确认操作，恢复执行。"""
    if not user_id:
        return StreamingResponse(
            iter([_sse_event({"type": "error", "error": True, "message": "Unauthorized", "code": "UNAUTHORIZED"})]),
            media_type="text/event-stream",
        )

    body = await request.json()
    confirm_id = body.get("confirmId")
    approved = body.get("approved", False)
    context_headers = _get_context_headers(request)
    workspace_id = context_headers["workspaceId"]
    table_ids = _merge_table_ids(body.get("tableId"), body.get("tableIds", []))

    if not confirm_id:
        return StreamingResponse(
            iter([_sse_event({"type": "error", "error": True, "message": "confirmId is required", "code": "INVALID_REQUEST"})]),
            media_type="text/event-stream",
        )

    async with AsyncSessionLocal() as db:
        config = await _load_ai_config(db, user_id)
        if not config:
            return StreamingResponse(
                iter([_sse_event({"type": "error", "error": True, "message": "AI configuration not found", "code": "AI_CONFIG_NOT_FOUND"})]),
                media_type="text/event-stream",
            )

    async def generate():
        try:
            async with AsyncSessionLocal() as session:
                from app.services.encryption import decrypt_api_key
                from pydantic_ai.models.openai import OpenAIChatModel as PydanticOpenAIChatModel
                from pydantic_ai.providers.openai import OpenAIProvider

                api_key = decrypt_api_key(config.api_key_encrypted)
                base_url = "https://api.deepseek.com/v1"
                if config.provider == "openai":
                    base_url = "https://api.openai.com/v1"

                requested_model = config.model

                emitter = SSEEventEmitter()

                context_builder = get_context_builder()
                ctx = context_builder.build(
                    user_id=user_id,
                    user_message="",
                    session_id=context_headers["sessionId"],
                    workspace_id=workspace_id,
                    conversation_id=body.get("conversationId"),
                    project_id=context_headers["projectId"],
                    table_ids=table_ids,
                    view_id=context_headers["viewId"],
                    task_id=context_headers["taskId"],
                    team_id=context_headers["teamId"],
                    organization_id=context_headers["organizationId"],
                    workflow_id=context_headers["workflowId"],
                    agent_id=context_headers["agentId"],
                )

                executor = get_runtime_executor()
                await executor.resume_after_confirm(
                    ctx=ctx,
                    db=session,
                    user_id=user_id,
                    confirm_id=confirm_id,
                    approved=approved,
                    emitter=emitter,
                )

                for event_str in emitter.get_events():
                    yield event_str

        except Exception as exc:
            logger.exception("AI Runtime confirm failed user_id=%s", user_id)
            yield _sse_event(
                {
                    "type": "error",
                    "error": True,
                    "message": str(exc) or "确认处理失败",
                    "code": "CONFIRM_FAILED",
                }
            )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
