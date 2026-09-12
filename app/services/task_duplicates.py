"""Shared, permission-safe duplicate detection for QTable task creation flows.

The service is intentionally deterministic and network-free.  It aligns QTable's
threshold semantics with QNoteServer so Source Inbox, AI task planning and manual
creation can present the same explainable candidate model without silently
merging or blocking user writes.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Iterable, Mapping, Optional, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.source_inbox import SourceInboxItem
from app.services.row_permissions import filter_store_for_user
from app.services.smart_table_store import get_full_store
from app.services.workspace import get_effective_permission_for_item, permission_allows


HIGH_THRESHOLD = 0.85
MEDIUM_THRESHOLD = 0.65
MINIMUM_CANDIDATE_SCORE = 0.45
MAX_SCAN_RECORDS = 500
DEFAULT_LIMIT = 5
MAX_LIMIT = 10

_TRACKING_QUERY_KEYS = {"fbclid", "gclid", "mc_cid", "mc_eid"}

# Keep these concepts conservative: they improve recall for common task wording
# without pretending to be a general-purpose embedding model.  The service
# contract leaves room for a future embedding provider behind the same scorer.
_SEMANTIC_GROUPS: dict[str, tuple[str, ...]] = {
    "home": ("首页", "主页", "home", "landing page"),
    "load": ("加载", "首屏", "首次加载", "first load", "initial load", "startup"),
    "performance": ("性能", "速度", "耗时", "延迟", "latency", "performance", "speed"),
    "optimize": ("优化", "提升", "降低", "减少", "改进", "改善", "optimize", "improve", "reduce"),
    "task": ("任务", "事项", "工作项", "task", "work item"),
    "duplicate": ("重复", "重复项", "duplicate", "same work"),
    "login": ("登录", "登陆", "signin", "sign in", "log in", "login"),
    "auth": ("鉴权", "认证", "身份验证", "authentication", "authorization", "auth"),
    "fix": ("修复", "解决", "处理", "修正", "fix", "repair", "resolve"),
    "error": ("错误", "异常", "报错", "故障", "error", "failure", "fault"),
    "notification": ("通知", "提醒", "消息提示", "notification", "reminder", "alert"),
}

_TITLE_NAMES = ("任务名称", "任务标题", "标题", "名称", "task title", "title", "name")
_DESCRIPTION_NAMES = ("任务描述", "描述", "说明", "description", "details")
_STATUS_NAMES = ("状态", "任务状态", "status", "state")
_OWNER_NAMES = ("负责人", "执行人", "责任人", "成员", "人员", "owner", "assignee")
_SOURCE_URL_NAMES = ("来源链接", "原文链接", "source url", "source link", "url")
_SOURCE_ID_NAMES = ("来源id", "来源 id", "source id", "sourceid")
_SOURCE_QUOTE_NAMES = ("来源摘录", "原文摘录", "原文", "source quote", "quote")
_TAG_NAMES = ("标签", "tags", "tag")
_COMPLETED_STATUS_TOKENS = {
    "completed",
    "complete",
    "done",
    "closed",
    "resolved",
    "已完成",
    "完成",
    "已关闭",
    "关闭",
    "已解决",
}


class TaskDuplicateError(ValueError):
    """Raised when a duplicate scan cannot be performed safely."""


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower().strip()
    for canonical, aliases in _SEMANTIC_GROUPS.items():
        for alias in sorted(aliases, key=len, reverse=True):
            text = text.replace(alias, f" {canonical} ")
    text = re.sub(r"[^\w\u3400-\u9fff]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _compact(value: Any) -> str:
    return normalize_text(value).replace(" ", "")


def canonicalize_url(value: Optional[str]) -> str:
    if not value:
        return ""
    raw = str(value).strip()
    if not raw:
        return ""
    try:
        parts = urlsplit(raw)
        query = [
            (key, val)
            for key, val in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_")
            and key.lower() not in _TRACKING_QUERY_KEYS
        ]
        path = parts.path.rstrip("/") or "/"
        return urlunsplit(
            (
                parts.scheme.lower(),
                parts.netloc.lower(),
                path,
                urlencode(sorted(query)),
                "",
            )
        )
    except Exception:  # malformed URLs still receive deterministic normalization
        return raw.casefold().split("#", 1)[0].rstrip("/")


def content_hash(*parts: Any) -> str:
    normalized = "\n".join(normalize_text(part) for part in parts if part not in (None, ""))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _ngrams(text: str, size: int = 2) -> set[str]:
    compact = text.replace(" ", "")
    if len(compact) < size:
        return {compact} if compact else set()
    return {compact[index : index + size] for index in range(len(compact) - size + 1)}


def _dice(left: Iterable[str], right: Iterable[str]) -> float:
    a, b = set(left), set(right)
    if not a or not b:
        return 0.0
    return 2.0 * len(a & b) / (len(a) + len(b))


def lexical_similarity(left: Any, right: Any) -> float:
    a, b = normalize_text(left), normalize_text(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    sequence = SequenceMatcher(None, a, b).ratio()
    grams = _dice(_ngrams(a), _ngrams(b))
    tokens = _dice(a.split(), b.split())
    return round(max(sequence * 0.45 + grams * 0.35 + tokens * 0.20, tokens, grams), 6)


def threshold_band(score: float) -> str:
    if score >= HIGH_THRESHOLD:
        return "high"
    if score >= MEDIUM_THRESHOLD:
        return "medium"
    return "low"


def _field_matches(field: Mapping[str, Any], names: Sequence[str], types: set[str]) -> bool:
    if str(field.get("type") or "") not in types:
        return False
    normalized_name = _compact(field.get("name"))
    return any(
        normalized_name == _compact(name) or _compact(name) in normalized_name
        for name in names
    )


def _field_by_names(
    fields: Sequence[Mapping[str, Any]],
    names: Sequence[str],
    types: set[str],
) -> Optional[Mapping[str, Any]]:
    return next((field for field in fields if _field_matches(field, names, types)), None)


def _label(field: Optional[Mapping[str, Any]], value: Any) -> str:
    if value in (None, "", []):
        return ""
    options = {
        str(item.get("id")): str(item.get("label") or item.get("name") or item.get("id"))
        for item in list((field or {}).get("options") or [])
        if isinstance(item, dict) and item.get("id") is not None
    }
    if isinstance(value, list):
        return ", ".join(part for part in (_label(field, item) for item in value) if part)
    if isinstance(value, dict):
        candidate = (
            value.get("name")
            or value.get("label")
            or value.get("id")
            or value.get("userId")
            or value.get("value")
        )
        return options.get(str(candidate), str(candidate or ""))
    return options.get(str(value), str(value))


def _string_value(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, list):
        return " ".join(_string_value(item) for item in value if item not in (None, ""))
    if isinstance(value, dict):
        return " ".join(
            str(part)
            for key, part in value.items()
            if key in {"id", "label", "name", "value"} and part not in (None, "")
        )
    return str(value)


def _is_completed(status: str) -> bool:
    normalized = normalize_text(status)
    compact = normalized.replace(" ", "")
    return any(
        normalize_text(token) == normalized or normalize_text(token).replace(" ", "") == compact
        for token in _COMPLETED_STATUS_TOKENS
    )


@dataclass(frozen=True)
class DuplicateSubject:
    title: str
    content: str = ""
    source_id: str = ""
    source_url: str = ""
    tags: str = ""

    @property
    def normalized_content_hash(self) -> str:
        return content_hash(self.title, self.content, self.tags)

    @property
    def canonical_url(self) -> str:
        return canonicalize_url(self.source_url)


@dataclass(frozen=True)
class ScoreResult:
    score: float
    match_type: str
    reason: str
    matched_on: list[str]


class TaskDuplicateScorer:
    """Explainable scorer shared by every task-creation entry point."""

    def compare(self, subject: DuplicateSubject, candidate: DuplicateSubject) -> ScoreResult:
        subject_text = normalize_text(subject.title + " " + subject.content + " " + subject.tags)
        if (
            subject_text
            and subject.normalized_content_hash == candidate.normalized_content_hash
        ):
            return ScoreResult(1.0, "exact_content", "任务标题和内容指纹完全一致", ["content"])

        title_score = lexical_similarity(subject.title, candidate.title)
        content_score = lexical_similarity(subject.content, candidate.content)
        tag_score = lexical_similarity(subject.tags, candidate.tags)
        cross_score = lexical_similarity(
            f"{subject.title} {subject.content}",
            f"{candidate.title} {candidate.content}",
        )
        semantic_score = max(
            title_score,
            cross_score,
            0.56 * title_score + 0.36 * content_score + 0.08 * tag_score,
        )

        same_source_id = bool(subject.source_id and subject.source_id == candidate.source_id)
        same_url = bool(subject.canonical_url and subject.canonical_url == candidate.canonical_url)
        if same_source_id or same_url:
            score = min(0.99, 0.72 + 0.28 * semantic_score)
            matched = ["source"]
            if title_score >= MEDIUM_THRESHOLD:
                matched.append("title")
            if content_score >= MEDIUM_THRESHOLD:
                matched.append("description")
            return ScoreResult(
                round(score, 6),
                "exact_source",
                "来自同一来源，并结合任务内容相似度评分",
                matched,
            )

        score = round(semantic_score, 6)
        matched_on: list[str] = []
        if title_score >= MINIMUM_CANDIDATE_SCORE:
            matched_on.append("title")
        if content_score >= MINIMUM_CANDIDATE_SCORE:
            matched_on.append("description")
        if tag_score >= MINIMUM_CANDIDATE_SCORE:
            matched_on.append("tags")
        if score >= MEDIUM_THRESHOLD:
            return ScoreResult(score, "semantic", "标题与正文语义高度相近", matched_on or ["content"])
        return ScoreResult(score, "lexical", "存在可解释的关键词或文本重合", matched_on or ["content"])


@dataclass(frozen=True)
class _PreparedStore:
    fields: list[dict[str, Any]]
    records: list[dict[str, Any]]
    default_view_id: str
    field_ids: dict[str, str]
    field_models: dict[str, Optional[dict[str, Any]]]
    truncated: bool


class TaskDuplicateService:
    def __init__(self) -> None:
        self.scorer = TaskDuplicateScorer()

    def _prepare_store(
        self,
        store: Mapping[str, Any],
        *,
        field_mapping: Optional[Mapping[str, str]] = None,
    ) -> _PreparedStore:
        fields = [dict(field) for field in store.get("fields", []) if isinstance(field, dict)]
        raw_records = [dict(record) for record in store.get("records", []) if isinstance(record, dict)]
        truncated = len(raw_records) > MAX_SCAN_RECORDS
        records = raw_records[:MAX_SCAN_RECORDS]

        by_id = {str(field.get("id") or ""): field for field in fields if field.get("id")}
        mapping = dict(field_mapping or {})

        def resolve(
            semantic: str,
            names: Sequence[str],
            types: set[str],
            *,
            fallback_text: bool = False,
        ) -> Optional[dict[str, Any]]:
            mapped = by_id.get(str(mapping.get(semantic) or ""))
            if mapped and str(mapped.get("type") or "") in types:
                return mapped
            matched = _field_by_names(fields, names, types)
            if matched:
                return dict(matched)
            if fallback_text:
                fallback = next((field for field in fields if field.get("type") == "text"), None)
                return dict(fallback) if fallback else None
            return None

        title_field = resolve("title", _TITLE_NAMES, {"text"}, fallback_text=True)
        description_field = resolve("description", _DESCRIPTION_NAMES, {"text"})
        status_field = resolve("status", _STATUS_NAMES, {"select", "text"})
        owner_field = resolve("owner", _OWNER_NAMES, {"member", "text"})
        source_url_field = resolve("sourceUrl", _SOURCE_URL_NAMES, {"url", "text"})
        source_id_field = resolve("sourceId", _SOURCE_ID_NAMES, {"text"})
        source_quote_field = resolve("sourceQuote", _SOURCE_QUOTE_NAMES, {"text"})
        tags_field = resolve("tags", _TAG_NAMES, {"multiSelect", "select", "text"})

        field_models = {
            "title": title_field,
            "description": description_field,
            "status": status_field,
            "owner": owner_field,
            "sourceUrl": source_url_field,
            "sourceId": source_id_field,
            "sourceQuote": source_quote_field,
            "tags": tags_field,
        }
        field_ids = {
            semantic: str(field.get("id") or "")
            for semantic, field in field_models.items()
            if field and field.get("id")
        }
        return _PreparedStore(
            fields=fields,
            records=records,
            default_view_id=str(store.get("defaultViewId") or ""),
            field_ids=field_ids,
            field_models=field_models,
            truncated=truncated,
        )

    def _scan_prepared(
        self,
        prepared: _PreparedStore,
        *,
        table_id: str,
        title: str,
        description: str = "",
        source_quote: str = "",
        source_url: str = "",
        source_id: str = "",
        tags: Optional[Sequence[str]] = None,
        limit: int = DEFAULT_LIMIT,
        exclude_record_ids: Optional[set[str]] = None,
    ) -> dict[str, Any]:
        safe_title = str(title or "").strip()
        safe_description = str(description or "").strip()
        safe_quote = str(source_quote or "").strip()
        safe_tags = [str(tag).strip() for tag in (tags or []) if str(tag).strip()]
        if not any((safe_title, safe_description, safe_quote, safe_tags)):
            raise TaskDuplicateError("Duplicate check requires task content")
        if "title" not in prepared.field_ids:
            return {
                "candidates": [],
                "scanTruncated": prepared.truncated,
                "thresholds": self.thresholds(),
                "permissionScope": {"rowVisibilityApplied": True},
                "warning": "Target table has no readable text field for task title",
            }

        subject = DuplicateSubject(
            title=safe_title,
            content="\n".join(part for part in (safe_description, safe_quote) if part),
            source_id=str(source_id or "").strip(),
            source_url=str(source_url or "").strip(),
            tags=" ".join(safe_tags),
        )
        excluded = {str(value) for value in (exclude_record_ids or set())}
        candidates: list[dict[str, Any]] = []
        title_id = prepared.field_ids["title"]
        desc_id = prepared.field_ids.get("description", "")
        status_id = prepared.field_ids.get("status", "")
        owner_id = prepared.field_ids.get("owner", "")
        url_id = prepared.field_ids.get("sourceUrl", "")
        source_id_field = prepared.field_ids.get("sourceId", "")
        quote_id = prepared.field_ids.get("sourceQuote", "")
        tags_id = prepared.field_ids.get("tags", "")

        for record in prepared.records:
            record_id = str(record.get("id") or "")
            candidate_title = str(record.get(title_id) or "").strip()
            if not record_id or record_id in excluded or not candidate_title:
                continue
            candidate_description = str(record.get(desc_id) or "").strip() if desc_id else ""
            candidate_quote = str(record.get(quote_id) or "").strip() if quote_id else ""
            candidate = DuplicateSubject(
                title=candidate_title,
                content="\n".join(part for part in (candidate_description, candidate_quote) if part),
                source_id=str(record.get(source_id_field) or "").strip() if source_id_field else "",
                source_url=str(record.get(url_id) or "").strip() if url_id else "",
                tags=_string_value(record.get(tags_id)) if tags_id else "",
            )
            score = self.scorer.compare(subject, candidate)
            if score.score < MINIMUM_CANDIDATE_SCORE:
                continue

            status = _label(prepared.field_models.get("status"), record.get(status_id)) if status_id else ""
            owner = _label(prepared.field_models.get("owner"), record.get(owner_id)) if owner_id else ""
            completed = _is_completed(status)
            deep_link = (
                f"/workbench/{table_id}/{prepared.default_view_id}?recordId={record_id}"
                if prepared.default_view_id
                else f"/workbench/{table_id}?recordId={record_id}"
            )
            band = threshold_band(score.score)
            candidates.append(
                {
                    "recordId": record_id,
                    "title": candidate_title,
                    "similarity": score.score,
                    # Backward-compatible alias used by the existing TaskPlanning UI.
                    "score": score.score,
                    "thresholdBand": band,
                    "matchType": score.match_type,
                    "reason": score.reason,
                    "similarFragment": candidate_description[:240] or candidate_title[:240],
                    "matchedOn": score.matched_on,
                    "status": status,
                    "owner": owner,
                    "assignee": owner,
                    "completed": completed,
                    "requiresReview": bool(band == "high" and not completed),
                    # Duplicate detection is advisory. It never prevents a user
                    # from explicitly choosing "still create".
                    "blocksCreation": False,
                    "recommendedAction": (
                        "reuse" if band == "high" and not completed else "review"
                    ),
                    "deepLink": deep_link,
                }
            )

        safe_limit = max(1, min(MAX_LIMIT, int(limit or DEFAULT_LIMIT)))
        candidates.sort(
            key=lambda item: (
                -float(item["similarity"]),
                bool(item["completed"]),
                str(item["title"]),
                str(item["recordId"]),
            )
        )
        return {
            "candidates": candidates[:safe_limit],
            "scanTruncated": prepared.truncated,
            "thresholds": self.thresholds(),
            "permissionScope": {
                "rowVisibilityApplied": True,
                "hiddenRecordsExcluded": True,
                "advisoryOnly": True,
            },
        }

    @staticmethod
    def thresholds() -> dict[str, float]:
        return {
            "high": HIGH_THRESHOLD,
            "medium": MEDIUM_THRESHOLD,
            "minimumCandidate": MINIMUM_CANDIDATE_SCORE,
        }

    async def _visible_store(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        table_id: str,
        required_permission: str,
    ) -> dict[str, Any]:
        permission = await get_effective_permission_for_item(db, user_id, table_id)
        if not permission_allows(permission, required_permission):
            raise PermissionError("Target task table not found or no access")
        store = await get_full_store(db, table_id)
        visible, _ = await filter_store_for_user(
            db,
            table_id,
            store,
            user_id=user_id,
            table_permission=permission,
        )
        return visible

    async def scan_table(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        table_id: str,
        title: str,
        description: str = "",
        source_quote: str = "",
        source_url: str = "",
        source_id: str = "",
        tags: Optional[Sequence[str]] = None,
        limit: int = DEFAULT_LIMIT,
        exclude_record_ids: Optional[set[str]] = None,
        field_mapping: Optional[Mapping[str, str]] = None,
        required_permission: str = "update",
    ) -> dict[str, Any]:
        store = await self._visible_store(
            db,
            user_id=user_id,
            table_id=table_id,
            required_permission=required_permission,
        )
        prepared = self._prepare_store(store, field_mapping=field_mapping)
        result = self._scan_prepared(
            prepared,
            table_id=table_id,
            title=title,
            description=description,
            source_quote=source_quote,
            source_url=source_url,
            source_id=source_id,
            tags=tags,
            limit=limit,
            exclude_record_ids=exclude_record_ids,
        )
        result.update({"tableId": table_id, "origin": "manual"})
        return result

    async def scan_source_inbox_item(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        item_id: str,
        table_id: str,
        limit: int = DEFAULT_LIMIT,
    ) -> dict[str, Any]:
        result = await db.execute(
            select(SourceInboxItem).where(
                SourceInboxItem.id == item_id,
                SourceInboxItem.user_id == user_id,
            )
        )
        item = result.scalars().first()
        if item is None:
            raise TaskDuplicateError("Inbox item not found or no access")
        output = await self.scan_table(
            db,
            user_id=user_id,
            table_id=table_id,
            title=str(item.page_title or item.annotation or item.quote or ""),
            description=str(item.annotation or ""),
            source_quote=str(item.quote or ""),
            source_url=str(item.canonical_url or item.url or ""),
            source_id=str(item.source_id or ""),
            tags=list(item.tags or []),
            limit=limit,
            required_permission="edit",
        )
        output["origin"] = "source_inbox"
        output["itemId"] = item_id
        return output

    async def scan_plan(
        self,
        db: AsyncSession,
        *,
        user_id: int,
        table_id: str,
        plan: Mapping[str, Any],
        field_mapping: Optional[Mapping[str, str]] = None,
        limit_per_node: int = 3,
    ) -> dict[str, list[dict[str, Any]]]:
        store = await self._visible_store(
            db,
            user_id=user_id,
            table_id=table_id,
            required_permission="edit",
        )
        prepared = self._prepare_store(store, field_mapping=field_mapping)
        root = plan.get("root") if isinstance(plan, Mapping) else None
        if not isinstance(root, Mapping):
            return {}

        nodes: list[Mapping[str, Any]] = []

        def walk(node: Mapping[str, Any], *, is_root: bool) -> None:
            if not is_root:
                nodes.append(node)
            children = node.get("children") or []
            if isinstance(children, list):
                for child in children:
                    if isinstance(child, Mapping):
                        walk(child, is_root=False)

        walk(root, is_root=True)
        output: dict[str, list[dict[str, Any]]] = {}
        for node in nodes:
            node_key = str(node.get("key") or "").strip()
            title = str(node.get("title") or "").strip()
            if not node_key or not title:
                continue
            description_parts = [
                str(node.get("description") or "").strip(),
                str(node.get("objective") or "").strip(),
            ]
            result = self._scan_prepared(
                prepared,
                table_id=table_id,
                title=title,
                description="\n".join(part for part in description_parts if part),
                tags=[str(tag) for tag in (node.get("tags") or []) if str(tag).strip()],
                limit=limit_per_node,
            )
            if result["candidates"]:
                output[node_key] = result["candidates"]
        return output


task_duplicate_service = TaskDuplicateService()
