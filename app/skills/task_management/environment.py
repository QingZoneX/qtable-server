"""
Layer 1: Environment Tools - 环境感知

提供 AI Agent 对运行环境的基础感知能力:
- get_current_datetime: 获取当前时间 (含时区感知)
- get_current_user: 获取当前用户信息
- get_current_workspace: 获取当前工作空间信息

这些是 AI 进行任务分析的前提条件。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.models.workspace_member import Workspace, WorkspaceMember
from app.skills.contracts import (
    SkillErrorCode,
    SkillLifecycleState,
    SkillMetadata,
    SkillPermissionRequirement,
    SkillSideEffect,
)
from app.skills.runtime import SkillDefinition, SkillExecutionContext, SkillRuntimeError
from app.skills.task_management.contracts import (
    ToolDomain,
    TaskManagementToolLayer,
    TaskManagementToolMetadata,
    ToolIntelligenceLevel,
    TaskManagementToolOperateMode,
    MCPCompatibilityInfo,
    LangGraphCompatibilityInfo,
    MultiAgentInfo,
    ToolObservability,
    ToolResult,
    ToolExecutionMetrics,
)


# ============ Tool 1: get_current_datetime ============

class GetCurrentDatetimeInput(BaseModel):
    """get_current_datetime 输入 - 无需参数，从上下文获取时区"""
    model_config = ConfigDict(extra="forbid")


class GetCurrentDatetimeOutput(BaseModel):
    """get_current_datetime 输出 - 多格式时间信息"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # ISO 8601 标准格式
    iso8601: str = Field(alias="iso8601")
    # Unix 时间戳
    unix_timestamp: float = Field(alias="unixTimestamp")
    # 可读格式
    human_readable: str = Field(alias="humanReadable")
    # 日期部分
    date: str
    # 时间部分
    time: str
    # 星期几 (中英文)
    day_of_week: str = Field(alias="dayOfWeek")
    day_of_week_en: str = Field(alias="dayOfWeekEn")
    # 是否为周末
    is_weekend: bool = Field(alias="isWeekend")
    # 周数
    week_number: int = Field(alias="weekNumber")
    # 时区信息
    timezone: str
    # 时区偏移
    utc_offset: str = Field(alias="utcOffset")


async def handle_get_current_datetime(
    context: SkillExecutionContext,
    data: GetCurrentDatetimeInput,
) -> dict[str, Any]:
    """获取当前日期时间，含时区感知"""
    tz_str = context.context.timezone or "Asia/Shanghai"
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_str)
    except Exception:
        tz = None

    now = datetime.now(tz or timezone.utc)
    utc_now = datetime.now(timezone.utc)
    offset_hours = (now.utcoffset().total_seconds() / 3600) if now.utcoffset() else 0

    weekdays_cn = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekdays_en = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    dow = now.weekday()

    return {
        "iso8601": now.isoformat(),
        "unixTimestamp": now.timestamp(),
        "humanReadable": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "dayOfWeek": weekdays_cn[dow],
        "dayOfWeekEn": weekdays_en[dow],
        "isWeekend": dow >= 5,
        "weekNumber": now.isocalendar()[1],
        "timezone": tz_str,
        "utcOffset": f"UTC{offset_hours:+g}",
    }


# ============ Tool 2: get_current_user ============

class GetCurrentUserInput(BaseModel):
    """get_current_user 输入"""
    model_config = ConfigDict(extra="forbid")


class GetCurrentUserOutput(BaseModel):
    """get_current_user 输出"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    user_id: int = Field(alias="userId")
    username: str = ""
    email: str = ""
    display_name: str = Field(default="", alias="displayName")
    avatar_url: Optional[str] = Field(default=None, alias="avatarUrl")
    is_authenticated: bool = Field(alias="isAuthenticated")
    workspace_role: Optional[str] = Field(default=None, alias="workspaceRole")


async def handle_get_current_user(
    context: SkillExecutionContext,
    data: GetCurrentUserInput,
) -> dict[str, Any]:
    """获取当前用户信息"""
    user_id = context.context.user_id
    workspace_id = context.context.workspace_id

    if not user_id:
        raise SkillRuntimeError(
            SkillErrorCode.UNAUTHORIZED,
            "User not authenticated",
        )

    db: AsyncSession | None = context.db
    result: dict[str, Any] = {
        "userId": user_id,
        "username": "",
        "email": "",
        "displayName": "",
        "avatarUrl": None,
        "isAuthenticated": bool(user_id),
        "workspaceRole": None,
    }

    if db is not None:
        user_row = await db.get(User, user_id)
        if user_row:
            result["username"] = user_row.name or ""
            result["email"] = user_row.email or ""
            result["displayName"] = user_row.name or str(user_id)
            result["avatarUrl"] = getattr(user_row, "avatar_url", None)

        if workspace_id and db is not None:
            member_stmt = select(WorkspaceMember).where(
                WorkspaceMember.user_id == user_id,
                WorkspaceMember.workspace_id == workspace_id,
            ).limit(1)
            member_result = await db.execute(member_stmt)
            member = member_result.scalars().first()
            if member:
                result["workspaceRole"] = member.role.value if hasattr(member.role, "value") else str(member.role)

    return result


# ============ Tool 3: get_current_workspace ============

class GetCurrentWorkspaceInput(BaseModel):
    """get_current_workspace 输入"""
    model_config = ConfigDict(extra="forbid")


class WorkspaceMemberInfo(BaseModel):
    """工作空间成员简要信息"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    user_id: int = Field(alias="userId")
    username: str = ""
    role: str = ""


class GetCurrentWorkspaceOutput(BaseModel):
    """get_current_workspace 输出"""
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    workspace_id: str = Field(alias="workspaceId")
    workspace_name: str = Field(default="", alias="workspaceName")
    member_count: int = Field(default=0, alias="memberCount")
    members: list[dict[str, Any]] = Field(default_factory=list)
    is_owner: bool = Field(default=False, alias="isOwner")
    role: Optional[str] = None


async def handle_get_current_workspace(
    context: SkillExecutionContext,
    data: GetCurrentWorkspaceInput,
) -> dict[str, Any]:
    """获取当前工作空间信息"""
    workspace_id = context.context.workspace_id
    user_id = context.context.user_id
    db = context.db

    result: dict[str, Any] = {
        "workspaceId": workspace_id or "",
        "workspaceName": "",
        "memberCount": 0,
        "members": [],
        "isOwner": False,
        "role": None,
    }

    if not workspace_id:
        return result

    if db is not None:
        workspace = await db.get(Workspace, workspace_id)
        if workspace:
            result["workspaceName"] = workspace.name or ""

        members_stmt = select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
        )
        members_result = await db.execute(members_stmt)
        members = members_result.scalars().all()

        member_list: list[dict[str, Any]] = []
        for member in members:
            member_data = {
                "userId": member.user_id,
                "username": "",
                "role": member.role.value if hasattr(member.role, "value") else str(member.role),
            }
            if db is not None:
                user = await db.get(User, member.user_id)
                if user:
                    member_data["username"] = user.name or ""
            member_list.append(member_data)

        result["members"] = member_list
        result["memberCount"] = len(member_list)

        if user_id:
            for member in members:
                if member.user_id == user_id:
                    role = member.role.value if hasattr(member.role, "value") else str(member.role)
                    result["role"] = role
                    result["isOwner"] = role == "owner"
                    break

    return result


# ============ Skill Definitions ============

def _build_env_tool_metadata(
    name: str,
    title: str,
    description: str,
    tags: list[str],
    domain: ToolDomain,
    cache_ttl: int = 60,
) -> TaskManagementToolMetadata:
    return TaskManagementToolMetadata(
        name=name,
        title=title,
        description=description,
        tags=tags,
        side_effect="none",
        confirmation_required=False,
        idempotent=True,
        supports_dry_run=True,
        visibility="internal",
        layer=TaskManagementToolLayer.ENVIRONMENT,
        domain=domain,
        intelligence_level=ToolIntelligenceLevel.PASSIVE,
        operate_mode=TaskManagementToolOperateMode.CACHED,
        supports_streaming=False,
        supports_batch=False,
        supports_pagination=False,
        cache_ttl_seconds=cache_ttl,
        cache_strategy="simple",
        timeout_seconds=10,
        max_retries=1,
        retry_backoff_ms=200,
        mcp=MCPCompatibilityInfo(
            mcp_version="1.0",
            transport="sse",
            capabilities=["tools/list", "tools/call"],
        ),
        langgraph=LangGraphCompatibilityInfo(
            can_be_node=True,
            node_type="tool",
            supports_human_in_the_loop=False,
        ),
        multi_agent=MultiAgentInfo(
            agent_scoped=False,
            sharing_mode="shared_read",
        ),
        observability=ToolObservability(
            log_level="debug",
            metrics_enabled=True,
            tracing_enabled=True,
        ),
    )


def build_get_current_datetime_definition() -> SkillDefinition:
    return SkillDefinition(
        metadata=SkillMetadata(
            name="qtable.env.datetime.get",
            version="1.0.0",
            title="Get Current Datetime",
            description=(
                "获取当前日期和时间（含时区感知）。"
                "用于 AI 计算截止日期、判断任务是否延期、计算时间差等场景。"
                "返回 ISO 8601 格式、Unix 时间戳、可读格式、星期、周数、时区等信息。"
            ),
            tags=["environment", "datetime", "timezone"],
            side_effect=SkillSideEffect.NONE,
            confirmation_required=False,
            idempotent=True,
            supports_dry_run=True,
            visibility="internal",
            permissions=[],
        ),
        input_model=GetCurrentDatetimeInput,
        output_model=GetCurrentDatetimeOutput,
        handler=handle_get_current_datetime,
    )


def build_get_current_user_definition() -> SkillDefinition:
    return SkillDefinition(
        metadata=SkillMetadata(
            name="qtable.env.user.get",
            version="1.0.0",
            title="Get Current User",
            description=(
                "获取当前登录用户信息。返回用户 ID、用户名、邮箱、头像、"
                "在当前工作空间中的角色等信息。用于 AI 进行权限判断和个性化分析。"
            ),
            tags=["environment", "user", "auth"],
            side_effect=SkillSideEffect.NONE,
            confirmation_required=False,
            idempotent=True,
            supports_dry_run=True,
            visibility="internal",
            permissions=[],
        ),
        input_model=GetCurrentUserInput,
        output_model=GetCurrentUserOutput,
        handler=handle_get_current_user,
    )


def build_get_current_workspace_definition() -> SkillDefinition:
    return SkillDefinition(
        metadata=SkillMetadata(
            name="qtable.env.workspace.get",
            version="1.0.0",
            title="Get Current Workspace",
            description=(
                "获取当前工作空间信息。返回工作空间名称、成员列表、成员角色、"
                "当前用户权限等信息。用于 AI 进行协作分析和权限感知。"
            ),
            tags=["environment", "workspace", "collaboration"],
            side_effect=SkillSideEffect.NONE,
            confirmation_required=False,
            idempotent=True,
            supports_dry_run=True,
            visibility="internal",
            permissions=[],
        ),
        input_model=GetCurrentWorkspaceInput,
        output_model=GetCurrentWorkspaceOutput,
        handler=handle_get_current_workspace,
    )
