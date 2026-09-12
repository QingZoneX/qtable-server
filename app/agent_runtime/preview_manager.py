from __future__ import annotations

import logging
from typing import Any, Optional

from app.agent_runtime.types import ActionPreview, RecordChange, ToolCallRecord

logger = logging.getLogger(__name__)


class PreviewManager:
    def build_preview_from_tool_call(
        self,
        tool_call: ToolCallRecord,
        table_id: str = "",
        table_name: Optional[str] = None,
    ) -> Optional[ActionPreview]:
        output = tool_call.output or {}
        arguments = tool_call.arguments or {}

        action = self._infer_action(tool_call.skill_name, tool_call.function_name, arguments)

        record_changes = self._extract_record_changes(output, arguments, action)
        summary = self._build_summary(action, record_changes)
        affected_count = len(record_changes) if record_changes else 1

        is_dangerous = action in {"delete_record", "batch_delete"}

        return ActionPreview(
            step_id=tool_call.step_id,
            skill_name=tool_call.skill_name,
            action=action,
            summary=summary,
            affected_count=affected_count,
            record_changes=record_changes,
            table_id=table_id,
            table_name=table_name,
            is_dangerous=is_dangerous,
            danger_reason="此操作将删除数据，无法撤销" if is_dangerous else None,
            raw_arguments=arguments,
        )

    def _infer_action(
        self,
        skill_name: str,
        function_name: str,
        arguments: dict[str, Any],
    ) -> ActionPreview.model_fields["action"].annotation.__args__[0]:
        lower = f"{skill_name}.{function_name}".lower()
        if "delete" in lower or "remove" in lower:
            return "batch_delete" if "batch" in lower else "delete_record"
        if "create" in lower or "add" in lower or "insert" in lower:
            return "batch_create" if "batch" in lower else "create_record"
        if "update" in lower or "modify" in lower or "edit" in lower or "change" in lower:
            return "batch_update" if "batch" in lower else "update_record"
        if "batch" in lower:
            return "batch_update"
        return "update_record"

    def _extract_record_changes(
        self,
        output: dict[str, Any],
        arguments: dict[str, Any],
        action: str,
    ) -> list[RecordChange]:
        records = output.get("records") or output.get("recordChanges") or []
        if records:
            changes: list[RecordChange] = []
            for rec in records:
                if isinstance(rec, dict):
                    changes.append(RecordChange(
                        record_id=rec.get("recordId") or rec.get("id"),
                        record_title=rec.get("recordTitle") or rec.get("title"),
                        changes=rec.get("changes") or rec.get("values") or {},
                        original_values=rec.get("originalValues") or rec.get("originals") or {},
                    ))
            return changes

        values = arguments.get("values") or output.get("values") or {}
        record_id = arguments.get("recordId") or output.get("recordId")
        if values:
            return [RecordChange(
                record_id=record_id,
                changes=values,
                original_values=output.get("originalValues") or {},
            )]

        if arguments:
            return [RecordChange(
                record_id=record_id or arguments.get("id"),
                changes=arguments,
                original_values={},
            )]

        return []

    def _build_summary(self, action: str, changes: list[RecordChange]) -> str:
        count = len(changes) if changes else 1
        action_label = {
            "create_record": "创建",
            "update_record": "更新",
            "delete_record": "删除",
            "batch_create": "批量创建",
            "batch_update": "批量更新",
            "batch_delete": "批量删除",
        }.get(action, "操作")

        record_ids = [c.record_id for c in changes if c.record_id]
        if record_ids:
            ids_preview = ", ".join(record_ids[:3])
            if len(record_ids) > 3:
                ids_preview += f" 等{len(record_ids)}条"
            return f"将{action_label}记录: {ids_preview}"

        return f"将{action_label} {count} 条记录"


_preview_manager: PreviewManager | None = None


def get_preview_manager() -> PreviewManager:
    global _preview_manager
    if _preview_manager is None:
        _preview_manager = PreviewManager()
    return _preview_manager
