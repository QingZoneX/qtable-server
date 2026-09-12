"""
AI 对话日志记录模块。

将每一次 AI 对话（包括 /chat 和 /agent）的完整记录写入 `.log/ai-chat/` 目录。
每次对话生成一个独立的 JSON 日志文件。
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# 日志目录（相对于项目根目录）
_LOG_DIR = Path(__file__).resolve().parent.parent.parent / ".log" / "ai-chat"


def _ensure_log_dir() -> Path:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    return _LOG_DIR


def _make_safe_filename(conversation_id: str, timestamp: str) -> str:
    """生成安全的日志文件名：conversationId_YYYYMMDD_HHMMSS.json"""
    safe_id = "".join(c for c in conversation_id if c.isalnum() or c in "-_")[:64]
    return f"{safe_id}_{timestamp}.json"


def log_chat_conversation(
    *,
    conversation_id: str,
    user_id: int,
    mode: str,  # "chat" or "agent"
    user_question: str,
    table_ids: list[str],
    model: str,
    provider: str,
    workspace_id: Optional[str],
    answer: str,
    status: str,
    trace_id: str,
    tool_calls: list[dict[str, Any]],
    error: Optional[dict[str, Any]] = None,
    extra: Optional[dict[str, Any]] = None,
) -> str:
    """
    记录一次完整的 AI 对话到日志文件。

    返回写入的日志文件路径。
    """
    log_dir = _ensure_log_dir()
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%d_%H%M%S")
    filename = _make_safe_filename(conversation_id, timestamp)
    filepath = log_dir / filename

    log_entry: dict[str, Any] = {
        "conversationId": conversation_id,
        "userId": user_id,
        "mode": mode,
        "model": model,
        "provider": provider,
        "workspaceId": workspace_id,
        "traceId": trace_id,
        "status": status,
        "timestamp": now.isoformat(),
        "request": {
            "question": user_question,
            "tableIds": table_ids,
        },
        "response": {
            "answer": answer,
            "status": status,
        },
        "toolCalls": tool_calls,
    }

    if error:
        log_entry["error"] = error

    if extra:
        log_entry["extra"] = extra

    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(log_entry, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    return str(filepath)
