from __future__ import annotations

import logging
from typing import Any

from pydantic_ai import Agent as PydanticAgent
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings

from app.agent_runtime.types import AgentIntent, IntentResult

logger = logging.getLogger(__name__)

INTENT_SYSTEM_PROMPT = """你是 QTable AI Runtime 的意图路由器。

分析用户消息，判断意图类型：

1. **chat** - 纯对话/问答，不需要调用任何工具
   - 例如："你好"、"什么是多维表格"、"介绍一下这个项目"
2. **analyze** - 数据分析/查询，需要调用读取类 Skill
   - 例如："统计每月的销售额"、"查询延期任务"、"按负责人汇总工作量"
3. **action** - 数据修改操作，需要调用写入类 Skill
   - 例如："把张三的任务改为已完成"、"创建一条新记录"、"删除重复数据"
4. **multi_step** - 多步骤复合任务，需要编排多条工具调用
   - 例如："分析延期任务并按优先级分配给对应的负责人"
   - 例如："先统计工作量，再生成甘特图，最后给一个项目报告"

输出规则：
- 如果不需要工具，intent=chat
- 如果只需要查询/分析工具，intent=analyze
- 如果涉及写入操作，intent=action，且 requires_confirmation=true
- 如果需要3个以上工具或有依赖链，intent=multi_step
- suggested_skills 列出建议调用的 Skill 列表
"""


class IntentRouter:
    def __init__(self) -> None:
        self._agent: PydanticAgent[dict[str, Any], IntentResult] | None = None

    async def route(
        self,
        message: str,
        model: OpenAIChatModel,
        context: dict[str, Any],
    ) -> IntentResult:
        agent = PydanticAgent(
            model=model,
            deps_type=dict,
            output_type=IntentResult,
            system_prompt=INTENT_SYSTEM_PROMPT,
            model_settings=OpenAIChatModelSettings(temperature=0.1),
        )
        prompt = self._build_prompt(message, context)
        result = await agent.run(prompt, deps={})
        output = result.output
        logger.info(
            "IntentRouter: intent=%s confidence=%.2f reasoning=%s",
            output.intent.value,
            output.confidence,
            output.reasoning[:100],
        )
        return output

    def _build_prompt(self, message: str, context: dict[str, Any]) -> str:
        parts = [
            f"用户消息: {message}",
            f"当前上下文: workspace={context.get('workspace_id')}, "
            f"tables={context.get('table_ids')}",
        ]
        available_skills = context.get("available_skills", [])
        if available_skills:
            parts.append(f"可用 Skills: {available_skills}")
        return "\n".join(parts)


_intent_router: IntentRouter | None = None


def get_intent_router() -> IntentRouter:
    global _intent_router
    if _intent_router is None:
        _intent_router = IntentRouter()
    return _intent_router
