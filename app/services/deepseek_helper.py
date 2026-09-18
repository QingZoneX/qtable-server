"""
DeepSeek V4 / V4.1 模型兼容性辅助模块。

处理 DeepSeek V4 系列（包括当前 ``deepseek-flash``）在不同模式下的工具调用兼容性问题：

- 非思考模式（默认，reasoning_effort 未设置或为 'none'）：
  标准 OpenAI 工具调用格式，tool_choice='auto' 原生支持。
- 思考模式（reasoning_effort 被设置为有效值，如 'medium'/'high'）：
  需要移除 tool_choice 参数，并完整回传 reasoning_content 与 tool_calls，
  且确保 content 字段非空。当前通过 DeepSeekProvider 的 profile 配置
  （openai_supports_tool_choice_required=False）兼容处理。

核心策略：
1. 对 V4 系列模型使用 DeepSeekProvider（而非 OpenAIProvider），确保
   reasoning_content 的正确读取和回传（pydantic_ai 原生支持）
2. 对 2026-09-10 新增的 ``deepseek-flash`` 别名补齐 V4 profile；固定在
   pydantic-ai 1.x 时，上游 profile 仍只识别 ``deepseek-v4-*``
3. 非思考模式下 tool_choice='auto' 由 pydantic_ai 正常发送，DeepSeek V4 系列原生支持
4. 统一使用 https://api.deepseek.com/v1 作为基础 URL
"""

from __future__ import annotations

import logging
from typing import Any

from openai import AsyncOpenAI
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.deepseek import DeepSeekProvider

logger = logging.getLogger(__name__)

# 当前 DeepSeek V4.1 Flash 模型名，以及仍需兼容的历史 V4 前缀。
DEEPSEEK_FLASH_MODEL = "deepseek-flash"
DEEPSEEK_V4_PREFIX = "deepseek-v4-"

# 正式的 base URL（含 /v1 路径）
DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"


def is_deepseek_v4_model(model_name: str | None) -> bool:
    """判断是否为 DeepSeek V4 系列（含当前 V4.1 Flash 与历史 V4 别名）。"""
    if not model_name:
        return False
    normalized = model_name.strip().lower()
    return normalized == DEEPSEEK_FLASH_MODEL or normalized.startswith(DEEPSEEK_V4_PREFIX)


def is_deepseek_model(model_name: str | None) -> bool:
    """判断是否为 DeepSeek 任一模型（含 v4 和旧版）。"""
    if not model_name:
        return False
    normalized = model_name.strip().lower()
    return normalized.startswith("deepseek-")


def create_deepseek_client(
    api_key: str,
    model_name: str | None = None,
    base_url: str | None = None,
) -> AsyncOpenAI:
    """创建适用于 DeepSeek API 的 AsyncOpenAI 客户端。

    使用标准客户端（非思考模式下 V4 系列原生支持 tool_choice='auto'），
    设置 max_retries=2 以避免网络瞬时故障导致的 "Connection error."。

    Args:
        api_key: API 密钥
        model_name: 模型名称（仅用于日志，不影响客户端配置）
        base_url: API 基础 URL，默认为 https://api.deepseek.com/v1

    Returns:
        配置好的 AsyncOpenAI 客户端
    """
    url = (base_url or DEEPSEEK_BASE_URL).rstrip("/")
    return AsyncOpenAI(
        api_key=api_key,
        base_url=url,
        max_retries=2,
    )


def _build_current_flash_profile(provider: DeepSeekProvider, model_name: str) -> dict[str, Any] | None:
    """补齐 pydantic-ai 1.x 对 ``deepseek-flash`` V4.1 别名的 profile 识别。"""
    profile = provider.model_profile(model_name)
    if profile is None:
        return None

    if model_name.strip().lower() != DEEPSEEK_FLASH_MODEL:
        return profile

    # pydantic-ai predates the V4.1 alias and only treats
    # ``deepseek-v4-*`` as reasoning-capable / incompatible with
    # tool_choice="required". Preserve every other DeepSeek profile field and
    # override only the two alias-sensitive capabilities.
    # Model profiles are TypedDict values in pydantic-ai 2.x. They cannot be
    # used with isinstance() or dataclasses.replace(); copy and override the
    # two alias-sensitive capabilities instead.
    return {
        **dict(profile),
        "supports_thinking": True,
        "openai_supports_tool_choice_required": False,
    }


def build_deepseek_model(
    api_key: str,
    model_name: str,
    base_url: str | None = None,
    *,
    openai_client: AsyncOpenAI | None = None,
) -> OpenAIChatModel:
    """构建适配 DeepSeek 的 OpenAIChatModel。

    对 V4 系列模型使用 DeepSeekProvider（以获得 reasoning_content 读写回传特性），
    DeepSeekProvider 的 model_profile 已正确配置历史 V4 模型；对于当前
    ``deepseek-flash`` 别名，本模块额外补齐：
    - supports_thinking=True
    - openai_chat_thinking_field='reasoning_content'
    - openai_chat_send_back_thinking_parts='field'
    - openai_supports_tool_choice_required=False

    非 V4 DeepSeek 模型使用 OpenAIProvider + 标准 OpenAIChatModel。

    Args:
        api_key: API 密钥
        model_name: 模型名称
        base_url: API 基础 URL
        openai_client: 可选, 预先创建的 AsyncOpenAI 客户端

    Returns:
        配置好的 OpenAIChatModel 实例
    """
    client = openai_client or create_deepseek_client(api_key, model_name, base_url)

    if is_deepseek_v4_model(model_name):
        # V4 系列模型使用 DeepSeekProvider 获得完整 profile
        provider = DeepSeekProvider(openai_client=client)
        profile = _build_current_flash_profile(provider, model_name)
        logger.info(
            "DeepSeek helper: using DeepSeekProvider for v4-family model=%s",
            model_name,
        )
        return OpenAIChatModel(model_name, provider=provider, profile=profile)

    # 非 V4 DeepSeek 模型使用 OpenAIProvider
    from pydantic_ai.providers.openai import OpenAIProvider

    provider = OpenAIProvider(openai_client=client)
    return OpenAIChatModel(model_name, provider=provider)
