from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent, RunContext
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.openai import OpenAIProvider
from sqlalchemy.ext.asyncio import AsyncSession

from app.context_engine.builder import context_builder
from app.context_engine.models import AgentContext, ContextBuildInput
from app.core.config import settings
from app.services.deepseek_helper import (
    DEEPSEEK_BASE_URL,
    build_deepseek_model,
)
from app.models.ai_config import AiConfig
from app.models.task_split import (
    TaskSplitDependency as TaskSplitDependencyModel,
    TaskSplitNode as TaskSplitNodeModel,
    TaskSplitPlan as TaskSplitPlanModel,
)
from app.schemas.task_split import (
    TaskSplitDependency,
    TaskSplitEstimate,
    TaskSplitNode,
    TaskSplitPlan,
    TaskSplitRequest,
    TaskSplitResponse,
    TaskSplitRisk,
)
from app.services.encryption import decrypt_api_key
from app.services.smart_table_store import get_full_store, get_full_store_for_table
from app.services.row_permissions import filter_store_for_user
from app.services.workspace import (
    get_effective_permission_for_item,
    permission_allows,
)

logger = logging.getLogger(__name__)


class TableContextArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_ids: list[str] = Field(default_factory=list, alias="tableIds")
    sample_limit: int = Field(default=5, ge=1, le=12, alias="sampleLimit")


class TableFieldSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    type: str


class TableContextSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table_id: str = Field(alias="tableId")
    field_count: int = Field(alias="fieldCount")
    record_count: int = Field(alias="recordCount")
    fields: list[TableFieldSummary] = Field(default_factory=list)
    sample_rows: list[dict[str, Any]] = Field(default_factory=list, alias="sampleRows")


class TableContextResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tables: list[TableContextSummary] = Field(default_factory=list)


class SkillCatalogArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit: int = Field(default=12, ge=1, le=30)


class SkillCatalogItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    title: str
    description: str
    tags: list[str] = Field(default_factory=list)
    side_effect: str = Field(alias="sideEffect")


class SkillCatalogResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skills: list[SkillCatalogItem] = Field(default_factory=list)


@dataclass
class SplitTaskDeps:
    db: AsyncSession
    user_id: int
    request: TaskSplitRequest
    agent_context: AgentContext
    tool_log: list[dict[str, Any]] = field(default_factory=list)


class TaskSplitService:
    async def _load_user_ai_config(self, db: AsyncSession, user_id: int) -> AiConfig:
        from sqlalchemy import select

        result = await db.execute(select(AiConfig).where(AiConfig.user_id == str(user_id)))
        config = result.scalars().first()
        if not config:
            raise ValueError("AI configuration not found")
        return config

    def _build_provider_model(
        self,
        config: AiConfig,
        *,
        model_override: str | None = None,
    ) -> tuple[OpenAIChatModel, str, str]:
        provider = (config.provider or "deepseek").strip().lower()
        api_key = decrypt_api_key(config.api_key_encrypted)
        if provider == "openai":
            base_url = (settings.OPENAI_BASE_URL or "https://api.openai.com/v1").rstrip("/")
            model_name = model_override or config.model or settings.OPENAI_MODEL or "gpt-4o-mini"
            provider_name = "openai"
            client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0)
            model = OpenAIChatModel(
                model_name,
                provider=OpenAIProvider(openai_client=client),
            )
        elif provider in {"deepseek", "deepseek-chat", "deepseek-reasoner"}:
            model_name = model_override or config.model or settings.DEEPSEEK_MODEL or "deepseek-chat"
            base_url = (settings.DEEPSEEK_BASE_URL or DEEPSEEK_BASE_URL).rstrip("/")
            provider_name = "deepseek"
            model = build_deepseek_model(api_key, model_name, base_url)
        else:
            base_url = (
                settings.OPENAI_BASE_URL
                or settings.DEEPSEEK_BASE_URL
                or "https://api.openai.com/v1"
            ).rstrip("/")
            model_name = (
                model_override
                or config.model
                or settings.OPENAI_MODEL
                or settings.DEEPSEEK_MODEL
                or "gpt-4o-mini"
            )
            provider_name = "openai-compatible"
            client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0)
            model = OpenAIChatModel(
                model_name,
                provider=OpenAIProvider(openai_client=client),
            )

        return (model, provider_name, model_name)

    def _build_system_prompt(
        self, request: TaskSplitRequest, agent_context: AgentContext, table_schemas: str = ""
    ) -> str:
        prompt = (
            "你是 QTable 的 split_task Agent，负责把复杂目标拆解成可执行任务树，并输出稳定的结构化结果。\n\n"
            "能力边界与要求：\n"
            "1. 必须进行多层任务拆分，形成父子任务树。\n"
            "2. 每个任务必须给出明确目标、交付物、验收标准、工时预估和风险。\n"
            "3. 任务依赖只保留关键依赖，避免无意义的全连接。\n"
            "4. 输出必须适配软件项目管理，优先按照业务域、平台能力、交付阶段进行拆解。\n"
            "5. 结果必须结合当前上下文，尤其是 workspace / project / table / task / conversation 信息。\n"
            "6. 如信息不足，可以给出合理假设，但要写入 assumptions 或 warnings，不得伪造现有系统事实。\n"
            "7. Mermaid 只输出 `graph TD`，节点名称简洁，不要包含 markdown 代码块。\n"
            "8. 根任务建议控制在 4-8 个一级子任务；每层子任务数量不要超过配置上限。\n"
            "9. 工时以小时为单位，需包含 optimistic / likely / pessimistic / buffered 四个值。\n"
            "10. 如果当前系统上下文中已经存在表、任务、工作流信息，应优先复用这些语义。\n"
            "11. 每个任务还必须给出 priority、milestone、suggestedRole；叶子任务必须有交付物和验收标准。\n"
            "12. dependsOn 和 dependencies 必须使用输出节点自己的 key，且严禁形成循环依赖。\n\n"
            "当前上下文摘要：\n"
            f"- scopeKey: {agent_context.scope_key}\n"
            f"- workspace: {agent_context.workspace.name or agent_context.workspace.id or 'unknown'}\n"
            f"- projectId: {request.project_id or agent_context.project.id or 'unknown'}\n"
            f"- taskId: {request.task_id or agent_context.task.id or 'unknown'}\n"
            f"- tableIds: {request.table_ids or agent_context.table.metadata.get('tableIds') or []}\n"
            f"- summary: {agent_context.summary.text}\n"
        )
        if table_schemas:
            prompt += (
                "\n🎯 目标任务表的字段结构（任务落库时将按下列字段进行映射）：\n"
                f"{table_schemas}\n"
                "重要提示：生成的每个 task 都应该包含能映射到上述字段的数据。\n"
                "请在 task.title 中生成任务名称（映射到标题/名称字段），"
                "在 task.description 中生成详细描述（映射到描述/说明字段），"
                "评估合理的 priority（映射到优先级字段，注意 rating 类型通常为 1-5 评分），"
                "给出建议的 owner/assignee（映射到负责人/成员字段）。\n"
            )
        return prompt

    def _build_user_prompt(self, request: TaskSplitRequest, agent_context: AgentContext) -> str:
        highlights = "\n".join(f"- {item}" for item in agent_context.summary.highlights[:8]) or "- 无"
        return (
            "请输出一个可直接落库的任务拆解方案。\n\n"
            f"用户目标：{request.prompt}\n\n"
            "约束：\n"
            f"- maxDepth: {request.max_depth}\n"
            f"- maxChildrenPerNode: {request.max_children_per_node}\n"
            f"- autoCreateRecords: {request.auto_create_records}\n"
            f"- dryRun: {request.dry_run}\n"
            f"- locale: {request.locale}\n"
            f"- timezone: {request.timezone}\n\n"
            "当前上下文亮点：\n"
            f"{highlights}\n\n"
            "请确保输出包含：根任务树、任务依赖、工时预估、风险分析、Mermaid。"
        )

    async def _build_agent_context(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        request: TaskSplitRequest,
        trace_id: str,
    ) -> AgentContext:
        return await context_builder.build(
            db,
            ContextBuildInput(
                source="task_split_service",
                userId=user_id,
                sessionId=request.session_id,
                conversationId=request.conversation_id,
                workspaceId=request.workspace_id,
                projectId=request.project_id,
                tableIds=request.table_ids,
                viewId=request.view_id,
                taskId=request.task_id,
                teamId=request.team_id,
                organizationId=request.organization_id,
                workflowId=request.workflow_id,
                agentId=request.agent_id,
                message=request.prompt,
                locale=request.locale,
                timezone=request.timezone,
            ),
            trace_id=trace_id,
        )

    async def _load_visible_table_store(
        self,
        db: AsyncSession,
        *,
        table_id: str,
        user_id: int,
    ) -> dict[str, Any]:
        backend = (settings.DATA_BACKEND or "").lower()
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
            return store

        return await get_full_store_for_table(
            table_id if table_id != "dstDefault" else None
        )

    async def _load_table_context(
        self,
        db: AsyncSession,
        *,
        table_ids: list[str],
        sample_limit: int,
        user_id: int,
    ) -> TableContextResult:
        summaries: list[TableContextSummary] = []
        for table_id in table_ids[:8]:
            store = await self._load_visible_table_store(
                db,
                table_id=table_id,
                user_id=user_id,
            )
            summaries.append(
                TableContextSummary(
                    tableId=table_id,
                    fieldCount=len(store.get("fields", [])),
                    recordCount=len(store.get("records", [])),
                    fields=[
                        TableFieldSummary(
                            id=str(field.get("id", "")),
                            name=str(field.get("name", field.get("id", ""))),
                            type=str(field.get("type", "text")),
                        )
                        for field in store.get("fields", [])
                        if field.get("id")
                    ],
                    sampleRows=list(store.get("records", []))[:sample_limit],
                )
            )
        return TableContextResult(tables=summaries)

    async def _load_skill_catalog(
        self,
        db: AsyncSession,
        *,
        workspace_id: str | None,
        limit: int,
    ) -> SkillCatalogResult:
        from app.services.skill_registry import skill_registry_service

        manifest = await skill_registry_service.list_manifest(
            db,
            workspace_id=workspace_id,
            include_disabled=False,
        )
        items = []
        for entry in manifest[:limit]:
            metadata = entry.get("metadata") or {}
            items.append(
                SkillCatalogItem(
                    name=metadata.get("name", ""),
                    title=metadata.get("title", ""),
                    description=metadata.get("description", ""),
                    tags=list(metadata.get("tags", [])),
                    sideEffect=metadata.get("side_effect", "none"),
                )
            )
        return SkillCatalogResult(skills=items)

    def _count_nodes(self, node: TaskSplitNode) -> int:
        return 1 + sum(self._count_nodes(child) for child in node.children)

    def _collect_keys(self, node: TaskSplitNode) -> set[str]:
        keys = {node.key}
        for child in node.children:
            keys.update(self._collect_keys(child))
        return keys

    def _normalize_node(
        self,
        node: TaskSplitNode,
        *,
        depth: int,
        key: str,
        request: TaskSplitRequest,
        key_map: dict[str, str],
    ) -> TaskSplitNode:
        raw_key = str(node.key or "").strip()
        if raw_key:
            key_map[raw_key] = key
        normalized_children = [
            self._normalize_node(
                child,
                depth=depth + 1,
                key=f"{key}.{index}",
                request=request,
                key_map=key_map,
            )
            for index, child in enumerate(node.children[: request.max_children_per_node], start=1)
            if depth + 1 < request.max_depth
        ]
        risks = node.risks[:5]
        if not risks:
            risks = [
                TaskSplitRisk(
                    title="需求边界待确认",
                    level="medium",
                    impact="可能导致任务返工或拆解粒度不一致",
                    mitigation="在执行前补充范围评审和验收标准",
                )
            ]
        estimate = node.estimate or TaskSplitEstimate()
        likely = max(estimate.likely_hours, estimate.optimistic_hours, 1.0)
        pessimistic = max(estimate.pessimistic_hours, likely)
        buffered = max(estimate.buffered_hours, likely * 1.15)
        return TaskSplitNode(
            key=key,
            title=node.title.strip(),
            description=node.description.strip(),
            objective=node.objective.strip(),
            depth=depth,
            estimate=TaskSplitEstimate(
                optimisticHours=max(estimate.optimistic_hours, 0.5),
                likelyHours=likely,
                pessimisticHours=pessimistic,
                bufferedHours=round(buffered, 2),
            ),
            risks=risks,
            priority=node.priority,
            milestone=node.milestone.strip(),
            suggestedRole=node.suggested_role.strip(),
            sourceReference=node.source_reference,
            deliverables=(
                [item.strip() for item in node.deliverables if str(item).strip()][:8]
                or [node.objective.strip() or f"{node.title.strip()} 的可验收交付物"]
            ),
            acceptanceCriteria=(
                [
                    item.strip()
                    for item in node.acceptance_criteria
                    if str(item).strip()
                ][:8]
                or [
                    f"{node.objective.strip() or node.title.strip()} 已完成，并可由负责人复核验收"
                ]
            ),
            requiredSkills=[item.strip() for item in node.required_skills if str(item).strip()][:8],
            tags=[item.strip() for item in node.tags if str(item).strip()][:8],
            dependsOn=[item.strip() for item in node.depends_on if str(item).strip()][:12],
            children=normalized_children,
        )

    def _remap_node_dependencies(
        self,
        node: TaskSplitNode,
        key_map: dict[str, str],
        valid_keys: set[str],
    ) -> None:
        remapped: list[str] = []
        for raw in node.depends_on:
            candidate = key_map.get(raw, raw)
            if candidate in valid_keys and candidate != node.key and candidate not in remapped:
                remapped.append(candidate)
        node.depends_on = remapped
        for child in node.children:
            self._remap_node_dependencies(child, key_map, valid_keys)

    def _assert_dependency_acyclic(self, plan: TaskSplitPlan) -> None:
        graph: dict[str, list[str]] = {}
        for dep in plan.dependencies:
            if dep.dependency_type != "blocks":
                continue
            graph.setdefault(dep.predecessor_key, []).append(dep.successor_key)

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_key: str) -> None:
            if node_key in visiting:
                raise ValueError("Task dependency cycle detected")
            if node_key in visited:
                return
            visiting.add(node_key)
            for successor in graph.get(node_key, []):
                visit(successor)
            visiting.remove(node_key)
            visited.add(node_key)

        for node_key in list(graph):
            visit(node_key)

    def _derive_dependency_specs(self, plan: TaskSplitPlan) -> list[TaskSplitDependency]:
        discovered: list[TaskSplitDependency] = list(plan.dependencies)

        def walk(node: TaskSplitNode) -> None:
            for predecessor in node.depends_on:
                discovered.append(
                    TaskSplitDependency(
                        predecessorKey=predecessor,
                        successorKey=node.key,
                        dependencyType="blocks",
                        rationale=f"{predecessor} 完成后才能推进 {node.key}",
                    )
                )
            for child in node.children:
                walk(child)

        walk(plan.root)
        dedup: dict[tuple[str, str, str], TaskSplitDependency] = {}
        valid_keys = self._collect_keys(plan.root)
        for item in discovered:
            if item.predecessor_key == item.successor_key:
                continue
            if item.predecessor_key not in valid_keys or item.successor_key not in valid_keys:
                continue
            dedup[(item.predecessor_key, item.successor_key, item.dependency_type)] = item
        return list(dedup.values())

    def _escape_mermaid(self, value: str) -> str:
        return value.replace('"', "'").replace("\n", " ").strip()

    def _highest_risk_level(self, risks: list[TaskSplitRisk]) -> str:
        weights = {"low": 1, "medium": 2, "high": 3, "critical": 4}
        if not risks:
            return "medium"
        return max(risks, key=lambda item: weights.get(item.level, 2)).level

    def _generate_mermaid(self, plan: TaskSplitPlan) -> str:
        lines = ["graph TD"]

        def walk(node: TaskSplitNode) -> None:
            node_id = f"N{node.key.replace('.', '_')}"
            label = self._escape_mermaid(f"{node.key} {node.title}")
            lines.append(f'    {node_id}["{label}"]')
            for child in node.children:
                child_id = f"N{child.key.replace('.', '_')}"
                lines.append(f"    {node_id} --> {child_id}")
                walk(child)

        walk(plan.root)
        for dependency in plan.dependencies:
            from_id = f"N{dependency.predecessor_key.replace('.', '_')}"
            to_id = f"N{dependency.successor_key.replace('.', '_')}"
            if dependency.dependency_type == "parallelizable":
                lines.append(f"    {from_id} -. parallel .-> {to_id}")
            else:
                lines.append(f"    {from_id} -. blocks .-> {to_id}")
        return "\n".join(lines)

    def _normalize_plan(self, raw: TaskSplitPlan, request: TaskSplitRequest) -> TaskSplitPlan:
        key_map: dict[str, str] = {}
        root = self._normalize_node(
            raw.root,
            depth=0,
            key="1",
            request=request,
            key_map=key_map,
        )
        valid_keys = self._collect_keys(root)
        self._remap_node_dependencies(root, key_map, valid_keys)

        normalized_dependencies: list[TaskSplitDependency] = []
        for dependency in raw.dependencies:
            predecessor = key_map.get(dependency.predecessor_key, dependency.predecessor_key)
            successor = key_map.get(dependency.successor_key, dependency.successor_key)
            if predecessor not in valid_keys or successor not in valid_keys:
                continue
            normalized_dependencies.append(
                TaskSplitDependency(
                    predecessorKey=predecessor,
                    successorKey=successor,
                    dependencyType=dependency.dependency_type,
                    rationale=dependency.rationale,
                )
            )

        plan = TaskSplitPlan(
            goal=raw.goal.strip(),
            summary=raw.summary.strip(),
            assumptions=[item.strip() for item in raw.assumptions if str(item).strip()][:10],
            risks=raw.risks[:8],
            root=root,
            dependencies=normalized_dependencies,
            mermaid=raw.mermaid.strip(),
            warnings=[item.strip() for item in raw.warnings if str(item).strip()][:10],
        )
        plan.dependencies = self._derive_dependency_specs(plan)
        self._assert_dependency_acyclic(plan)
        plan.mermaid = self._generate_mermaid(plan)
        return plan

    async def _persist_plan(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        request: TaskSplitRequest,
        trace_id: str,
        provider: str,
        model: str,
        agent_context: AgentContext,
        plan: TaskSplitPlan,
    ) -> str:
        plan_id = str(uuid.uuid4())
        db.add(
            TaskSplitPlanModel(
                id=plan_id,
                workspace_id=request.workspace_id,
                project_id=request.project_id,
                task_id=request.task_id,
                source_prompt=request.prompt,
                normalized_goal=plan.goal[:255],
                summary=plan.summary,
                mermaid=plan.mermaid,
                context_summary=agent_context.summary.model_dump(mode="json", by_alias=True),
                structured_output=plan.model_dump(mode="json", by_alias=True),
                provider=provider,
                model=model,
                trace_id=trace_id,
                created_by=str(user_id),
                auto_created=bool(request.auto_create_records and not request.dry_run),
            )
        )
        node_id_by_key: dict[str, str] = {}

        def add_node(node: TaskSplitNode, parent_id: str | None, sort_order: int) -> None:
            node_id = str(uuid.uuid4())
            node_id_by_key[node.key] = node_id
            risk_summary = "；".join(f"{risk.level}:{risk.title}" for risk in node.risks[:3])
            db.add(
                TaskSplitNodeModel(
                    id=node_id,
                    plan_id=plan_id,
                    parent_id=parent_id,
                    node_key=node.key,
                    title=node.title[:255],
                    description=node.description,
                    objective=node.objective,
                    depth=node.depth,
                    sort_order=sort_order,
                    estimate_optimistic_hours=node.estimate.optimistic_hours,
                    estimate_likely_hours=node.estimate.likely_hours,
                    estimate_pessimistic_hours=node.estimate.pessimistic_hours,
                    estimate_buffered_hours=node.estimate.buffered_hours,
                    risk_level=self._highest_risk_level(node.risks),
                    risk_summary=risk_summary,
                    priority=node.priority,
                    milestone=node.milestone,
                    suggested_role=node.suggested_role,
                    source_reference=node.source_reference,
                    deliverables=node.deliverables,
                    acceptance_criteria=node.acceptance_criteria,
                    required_skills=node.required_skills,
                    tags=node.tags,
                    depends_on_keys=node.depends_on,
                    raw_payload=node.model_dump(mode="json", by_alias=True),
                )
            )
            for index, child in enumerate(node.children):
                add_node(child, node_id, index)

        add_node(plan.root, None, 0)
        for dependency in plan.dependencies:
            db.add(
                TaskSplitDependencyModel(
                    id=str(uuid.uuid4()),
                    plan_id=plan_id,
                    predecessor_node_id=node_id_by_key.get(dependency.predecessor_key),
                    successor_node_id=node_id_by_key.get(dependency.successor_key),
                    predecessor_key=dependency.predecessor_key,
                    successor_key=dependency.successor_key,
                    dependency_type=dependency.dependency_type,
                    rationale=dependency.rationale,
                )
            )
        await db.commit()
        return plan_id

    def _build_agent(
        self,
        *,
        provider_model,
        request: TaskSplitRequest,
        agent_context: AgentContext,
        table_schemas: str = "",
    ) -> Agent[SplitTaskDeps, TaskSplitPlan]:
        agent = Agent(
            model=provider_model,
            deps_type=SplitTaskDeps,
            output_type=TaskSplitPlan,
            system_prompt=self._build_system_prompt(request, agent_context, table_schemas),
            model_settings=OpenAIChatModelSettings(temperature=0.2),
            output_retries=request.retry.max_output_retries,
            tool_retries=request.retry.max_tool_retries,
        )

        @agent.instructions
        def runtime_instructions(_ctx: RunContext[SplitTaskDeps]) -> str:
            return (
                "补充要求：\n"
                "- 一级任务优先按业务域或平台能力拆分。\n"
                "- 子任务必须能交付、可验证，不要写成空泛口号。\n"
                "- 依赖只标注关键路径。\n"
                "- 风险与工时必须体现工程现实，不要过度乐观。\n"
            )

        @agent.output_validator
        def validate_output(
            _ctx: RunContext[SplitTaskDeps],
            output: TaskSplitPlan,
        ) -> TaskSplitPlan:
            if not output.root.children:
                raise ModelRetry("根任务至少需要包含一个子任务")
            if len(output.root.children) > request.max_children_per_node:
                raise ModelRetry("一级任务数量超过上限，请收敛任务分组")
            return output

        @agent.tool(
            name="load_table_context",
            retries=request.retry.max_tool_retries,
            include_return_schema=True,
        )
        async def load_table_context(
            ctx: RunContext[SplitTaskDeps],
            payload: TableContextArgs,
        ) -> TableContextResult:
            started = time.perf_counter()
            result = await self._load_table_context(
                ctx.deps.db,
                table_ids=payload.table_ids or ctx.deps.request.table_ids,
                sample_limit=payload.sample_limit,
                user_id=ctx.deps.user_id,
            )
            ctx.deps.tool_log.append(
                {
                    "tool": "load_table_context",
                    "latencyMs": round((time.perf_counter() - started) * 1000, 3),
                    "arguments": payload.model_dump(mode="json", by_alias=True),
                }
            )
            return result

        @agent.tool(
            name="list_enabled_skills",
            retries=request.retry.max_tool_retries,
            include_return_schema=True,
        )
        async def list_enabled_skills(
            ctx: RunContext[SplitTaskDeps],
            payload: SkillCatalogArgs,
        ) -> SkillCatalogResult:
            started = time.perf_counter()
            result = await self._load_skill_catalog(
                ctx.deps.db,
                workspace_id=ctx.deps.request.workspace_id,
                limit=payload.limit,
            )
            ctx.deps.tool_log.append(
                {
                    "tool": "list_enabled_skills",
                    "latencyMs": round((time.perf_counter() - started) * 1000, 3),
                    "arguments": payload.model_dump(mode="json", by_alias=True),
                }
            )
            return result

        return agent

    async def _build_table_schemas_prompt(
        self,
        db: AsyncSession,
        request: TaskSplitRequest,
        user_id: int,
    ) -> str:
        """Preload target table schemas and format them as a prompt string with options."""
        table_ids = request.table_ids or []
        if not table_ids:
            return ""
        lines: list[str] = []
        for table_id in table_ids[:8]:
            store = await self._load_visible_table_store(
                db,
                table_id=table_id,
                user_id=user_id,
            )
            raw_fields = list(store.get("fields") or [])
            if not raw_fields:
                continue
            lines.append(f"\n📋 表 {table_id}（{len(raw_fields)} 个字段）：")
            for f in raw_fields:
                fid = str(f.get("id", ""))
                fname = str(f.get("name", fid))
                ftype = str(f.get("type", "text"))
                options = f.get("options") or []
                # Format options for select/member fields
                opt_str = ""
                if options and isinstance(options[0], dict):
                    labels = [str(opt.get("label", opt.get("id", ""))) for opt in options[:6]]
                    opt_str = f" → 可选值: [{', '.join(labels)}]"
                # Format rating max
                max_str = ""
                if ftype == "rating":
                    prop = f.get("property") or {}
                    max_val = prop.get("max", 5) if isinstance(prop, dict) else 5
                    max_str = f" → 评分范围: 1-{max_val}"
                lines.append(f"  - [{fid}] {fname} ({ftype}){opt_str}{max_str}")
        return "\n".join(lines) if lines else ""

    async def run(
        self,
        *,
        db: AsyncSession,
        user_id: int,
        request: TaskSplitRequest,
    ) -> TaskSplitResponse:
        config = await self._load_user_ai_config(db, user_id)
        provider_model, provider_name, model_name = self._build_provider_model(
            config,
            model_override=request.model,
        )
        trace_id = str(uuid.uuid4())
        agent_context = await self._build_agent_context(
            db,
            user_id=user_id,
            request=request,
            trace_id=trace_id,
        )
        # Preload target table schemas so the AI knows available fields
        table_schemas = await self._build_table_schemas_prompt(
            db,
            request,
            user_id,
        )
        last_error: Exception | None = None

        for attempt in range(1, request.retry.max_attempts + 1):
            deps = SplitTaskDeps(
                db=db,
                user_id=user_id,
                request=request,
                agent_context=agent_context,
            )
            try:
                agent = self._build_agent(
                    provider_model=provider_model,
                    request=request,
                    agent_context=agent_context,
                    table_schemas=table_schemas,
                )
                result = await agent.run(
                    self._build_user_prompt(request, agent_context),
                    deps=deps,
                    output_retries=request.retry.max_output_retries,
                )
                plan = self._normalize_plan(result.output, request)
                persisted = bool(request.auto_create_records and not request.dry_run)
                plan_id = None
                if persisted:
                    plan_id = await self._persist_plan(
                        db,
                        user_id=user_id,
                        request=request,
                        trace_id=trace_id,
                        provider=provider_name,
                        model=model_name,
                        agent_context=agent_context,
                        plan=plan,
                    )
                return TaskSplitResponse(
                    traceId=trace_id,
                    provider=provider_name,
                    model=model_name,
                    attempts=attempt,
                    persisted=persisted,
                    planId=plan_id,
                    result=plan,
                )
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "task_split attempt failed trace_id=%s attempt=%s error=%s",
                    trace_id,
                    attempt,
                    str(exc),
                )
                await db.rollback()
                if attempt < request.retry.max_attempts and request.retry.backoff_ms:
                    await asyncio.sleep(request.retry.backoff_ms / 1000)

        return TaskSplitResponse(
            traceId=trace_id,
            provider=provider_name,
            model=model_name,
            attempts=request.retry.max_attempts,
            persisted=False,
            planId=None,
            result=TaskSplitPlan(
                goal=request.prompt,
                summary="任务拆解失败",
                assumptions=[],
                risks=[],
                root=TaskSplitNode(
                    key="1",
                    title=request.prompt,
                    description="任务拆解失败，未生成有效结果",
                    objective="请检查模型配置、上下文或缩小任务范围后重试",
                    depth=0,
                ),
                dependencies=[],
                mermaid='graph TD\n    N1["1 failed"]',
                warnings=[str(last_error) or "unknown error"],
            ),
            error={
                "code": "TASK_SPLIT_FAILED",
                "message": str(last_error) or "Task split failed",
            },
        )


task_split_service = TaskSplitService()
