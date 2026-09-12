"""Locale negotiation and product-owned backend translations.

QTable currently supports Simplified Chinese and English. Locale is selected
per request (or GraphQL websocket connection) and must never be inferred from
user-authored table/record content.
"""
from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any, Mapping, Optional

SUPPORTED_LOCALES = ("zh-CN", "en-US")
DEFAULT_LOCALE = "zh-CN"
_CURRENT_LOCALE: ContextVar[str] = ContextVar("qtable_locale", default=DEFAULT_LOCALE)

_MESSAGES: dict[str, dict[str, str]] = {
    "zh-CN": {
        "notification.defaultTitle": "通知",
        "notification.inaccessibleTitle": "内容不可访问",
        "notification.inaccessibleSummary": "该内容已删除或你已无权访问。",
        "notification.mentionDeleted": "{actor} 在一条已删除的评论中提到了你",
        "notification.mention": "{actor} 在评论中提到了你：{preview}",
        "notification.replyDeleted": "{actor} 的回复已被删除",
        "notification.reply": "{actor} 回复了你的评论：{preview}",
        "notification.taskAssigned": "{actor} 将任务分配给了你",
        "notification.due24h": "任务将在 24 小时内到期",
        "notification.due3d": "任务将在 3 天内到期",
        "notification.aiActionRequired": "有一项 AI 操作等待确认",
        "notification.automation": "自动化规则已触发",
        "notification.automationFailed": "一条自动化规则执行失败",
        "notification.collaboration": "有新的协作动态",
        "presence.guest": "访客",
        "error.unauthorized": "未登录或登录状态已失效",
        "error.noAccess": "没有访问权限",
        "error.recordNoAccess": "记录不存在或无权访问",
    },
    "en-US": {
        "notification.defaultTitle": "Notification",
        "notification.inaccessibleTitle": "Content unavailable",
        "notification.inaccessibleSummary": "This content was deleted or you no longer have access.",
        "notification.mentionDeleted": "{actor} mentioned you in a deleted comment",
        "notification.mention": "{actor} mentioned you in a comment: {preview}",
        "notification.replyDeleted": "{actor}'s reply was deleted",
        "notification.reply": "{actor} replied to your comment: {preview}",
        "notification.taskAssigned": "{actor} assigned a task to you",
        "notification.due24h": "A task is due within 24 hours",
        "notification.due3d": "A task is due within 3 days",
        "notification.aiActionRequired": "An AI action is waiting for confirmation",
        "notification.automation": "An automation rule was triggered",
        "notification.automationFailed": "An automation rule failed",
        "notification.collaboration": "There is new collaboration activity",
        "presence.guest": "Guest",
        "error.unauthorized": "You are not signed in or your session has expired",
        "error.noAccess": "You do not have access",
        "error.recordNoAccess": "Record not found or you do not have access",
    },
}


def normalize_locale(value: Optional[str]) -> str:
    """Normalize a locale/language tag to a supported QTable locale."""
    raw = str(value or "").strip().replace("_", "-").lower()
    if raw.startswith("en"):
        return "en-US"
    if raw.startswith("zh"):
        return "zh-CN"
    return DEFAULT_LOCALE


def locale_from_accept_language(value: Optional[str]) -> str:
    """Resolve the highest-priority supported Accept-Language value."""
    if not value:
        return DEFAULT_LOCALE
    candidates: list[tuple[float, int, str]] = []
    for index, part in enumerate(str(value).split(",")):
        section = part.strip()
        if not section:
            continue
        language, *parameters = [piece.strip() for piece in section.split(";")]
        quality = 1.0
        for parameter in parameters:
            if parameter.lower().startswith("q="):
                try:
                    quality = float(parameter[2:])
                except ValueError:
                    quality = 0.0
        if quality <= 0:
            continue
        lowered = language.replace("_", "-").lower()
        if lowered.startswith("en"):
            resolved = "en-US"
        elif lowered.startswith("zh"):
            resolved = "zh-CN"
        else:
            continue
        candidates.append((quality, -index, resolved))
    if not candidates:
        return DEFAULT_LOCALE
    candidates.sort(reverse=True)
    return candidates[0][2]


def locale_from_connection_params(params: Any) -> Optional[str]:
    """Read locale from GraphQL websocket connection parameters."""
    if not isinstance(params, Mapping):
        return None
    value = (
        params.get("Accept-Language")
        or params.get("accept-language")
        or params.get("locale")
        or params.get("language")
    )
    return normalize_locale(str(value)) if value else None


def get_locale() -> str:
    return _CURRENT_LOCALE.get()


def set_locale(locale: Optional[str]) -> Token[str]:
    return _CURRENT_LOCALE.set(normalize_locale(locale))


def reset_locale(token: Token[str]) -> None:
    _CURRENT_LOCALE.reset(token)


def translate(
    key: str,
    locale: Optional[str] = None,
    **variables: Any,
) -> str:
    resolved = normalize_locale(locale) if locale is not None else get_locale()
    catalog = _MESSAGES[resolved]
    fallback = _MESSAGES[DEFAULT_LOCALE]
    value = catalog.get(key) or fallback.get(key) or key
    if variables:
        try:
            return value.format(**variables)
        except (KeyError, ValueError):
            return value
    return value


def catalog_keys_match() -> bool:
    """Used by tests/quality gates to protect locale key parity."""
    baseline = set(_MESSAGES[DEFAULT_LOCALE])
    return all(set(messages) == baseline for messages in _MESSAGES.values())
