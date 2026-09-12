#!/usr/bin/env python3
"""Safe, repeatable development/demo data seeder for QTable."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import random
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.core.config import settings
from app.core.security import hash_password
from app.db.session import AsyncSessionLocal
from app.models.automation import AutomationEvent, AutomationExecution, AutomationRule
from app.models.collaboration import RecordComment, RecordCommentMention, UserNotification
from app.models.dashboard import Dashboard, DashboardWidget
from app.models.smart_table import WorkspaceItem, WorkspaceItemPermission
from app.models.source_inbox import SourceInboxItem
from app.models.user import User
from app.models.workspace_member import Workspace, WorkspaceMember, WorkspaceRole
from app.services.smart_table_store import create_records_with_data
from app.services.smart_table_store.db_backend import delete_table_data_db, init_table_db
from app.services.table_templates import resolve_template_for_use, sync_system_templates

SEED_PREFIX = "wkb_seed_"
SEED_NAME_PREFIX = "[SEED]"
SEED_EMAIL_PREFIX = "seed+dev"
# Auth endpoints validate the email with pydantic EmailStr, which rejects
# special-use TLDs such as `.invalid` (422 before any password check). Seed
# accounts must use a domain EmailStr accepts or they can never sign in.
SEED_EMAIL_DOMAIN = "example.com"
# Matches every synthetic user regardless of the domain it was created with, so
# --reset also cleans up accounts made by older versions of this script.
SEED_EMAIL_LIKE = f"{SEED_EMAIL_PREFIX}%"
DEFAULT_SEED = 20260910


class SeedSafetyError(RuntimeError):
    pass


@dataclass(frozen=True)
class TablePlan:
    template_id: str
    name: str
    records: int


REALISTIC = (
    TablePlan("project_management", "产品研发项目", 300),
    TablePlan("sprint_planning", "Sprint Backlog", 500),
    TablePlan("bug_tracking", "Bug Tracking", 400),
    TablePlan("milestone_tracker", "Milestones", 40),
    TablePlan("sales_crm", "客户 CRM", 300),
    TablePlan("content_calendar", "内容日历", 200),
    TablePlan("recruiting_pipeline", "招聘 Pipeline", 150),
    TablePlan("asset_inventory", "资产管理", 300),
)
COVERAGE = (
    TablePlan("project_management", "[Coverage] 项目状态矩阵", 120),
    TablePlan("bug_tracking", "[Coverage] 缺陷状态矩阵", 100),
    TablePlan("sales_crm", "[Coverage] CRM 状态矩阵", 80),
    TablePlan("content_calendar", "[Coverage] 内容状态矩阵", 80),
    TablePlan("recruiting_pipeline", "[Coverage] 招聘状态矩阵", 80),
)
TITLE_WORDS = {
    "project_management": ("权限模型", "仪表盘", "批量编辑", "甘特排期", "移动端", "自动化", "协作评论", "AI 助手"),
    "sprint_planning": ("登录重构", "表格性能", "看板拖拽", "日历联动", "筛选器", "成员字段", "关系字段", "回收站"),
    "bug_tracking": ("看板拖拽后顺序异常", "日期筛选跨时区偏移", "成员字段偶发丢失", "CSV 导入提示不完整", "关系字段删除后残留"),
    "milestone_tracker": ("Alpha 内测", "Beta 发布", "数据迁移", "权限收敛", "性能基线", "正式发布"),
    "sales_crm": ("星河科技", "远景制造", "云帆数据", "北辰零售", "青禾教育", "凌云物流"),
    "content_calendar": ("项目管理指南", "自动化最佳实践", "客户案例访谈", "版本发布说明", "性能优化复盘"),
    "recruiting_pipeline": ("高级前端工程师", "Python 后端工程师", "产品经理", "测试工程师", "UX 设计师"),
    "asset_inventory": ("MacBook Pro", "ThinkPad X1", "Dell 显示器", "iPhone 测试机", "开发服务器"),
    "performance": ("Performance row",),
}


def truthy(value: Optional[str]) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def token(*parts: Any, length: int = 16) -> str:
    return hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()[:length]


def seed_member_password() -> str:
    """Deterministic dev-only password shared by every synthetic Seed member."""
    return token("qtable-seed-disabled-login", length=32)


def workspace_id(profile: str) -> str:
    return f"{SEED_PREFIX}{profile}"


def workspace_name(profile: str) -> str:
    label = {"realistic": "QTable Demo / QA", "coverage": "QTable Coverage QA", "performance": "QTable Performance QA"}[profile]
    return f"{SEED_NAME_PREFIX} {label}"


def seed_root_id(wid: str) -> str:
    return f"fld_seed_{token(wid, 'root', length=20)}"


def build_seed_root_item(wid: str, name: str) -> WorkspaceItem:
    return WorkspaceItem(id=seed_root_id(wid), workspace_id=wid, type="folder", name=name, parent_id=None, order_index=0, default_view_id=None)


def db_target() -> tuple[str, str, str]:
    try:
        url = make_url(str(settings.SQLALCHEMY_DATABASE_URI or ""))
        return url.drivername, str(url.host or "local"), str(url.database or settings.POSTGRES_DB)
    except Exception:
        return settings.DATABASE_MODE, settings.POSTGRES_SERVER, settings.POSTGRES_DB


def require_write_opt_in(*, dry_run: bool, profile: str) -> None:
    if dry_run:
        return
    if not truthy(os.getenv("QTABLE_ALLOW_DEV_SEED")):
        raise SeedSafetyError("Set QTABLE_ALLOW_DEV_SEED=true after confirming the printed database target.")
    driver, _, database = db_target()
    if profile == "performance" and "sqlite" not in driver.lower():
        safe = any(word in database.casefold() for word in ("dev", "qa", "test", "seed", "demo"))
        if not safe and not truthy(os.getenv("QTABLE_ALLOW_PERFORMANCE_SEED")):
            raise SeedSafetyError(
                "Performance seed requires an isolated DB name containing dev/qa/test/seed/demo; "
                "override only with QTABLE_ALLOW_PERFORMANCE_SEED=true."
            )


def plans_for(profile: str, records: Optional[int]) -> list[TablePlan]:
    if profile == "performance":
        return [TablePlan("performance", "Performance Records", records or 20_000)]
    base = REALISTIC if profile == "realistic" else COVERAGE
    if records is None:
        return list(base)
    baseline = 300 if profile == "realistic" else 120
    ratio = records / baseline
    return [TablePlan(p.template_id, p.name, max(1, round(p.records * ratio))) for p in base]


def performance_snapshot() -> dict[str, Any]:
    return {
        "fields": [
            {"id": "f1", "name": "名称", "type": "text"},
            {"id": "f2", "name": "状态", "type": "select", "options": [
                {"id": "todo", "label": "未开始"}, {"id": "doing", "label": "进行中"}, {"id": "done", "label": "已完成"}
            ]},
            {"id": "f3", "name": "负责人", "type": "member", "property": {"multiple": False}},
            {"id": "f4", "name": "数值", "type": "number"},
            {"id": "f5", "name": "日期", "type": "date", "property": {"format": "YYYY-MM-DD"}},
            {"id": "f6", "name": "分类", "type": "select", "options": [
                {"id": "a", "label": "A"}, {"id": "b", "label": "B"}, {"id": "c", "label": "C"}, {"id": "d", "label": "D"}
            ]},
        ],
        "records": [],
        "views": [{"id": "v1", "name": "表格", "type": "grid", "config": {
            "filters": [], "sorts": [], "groupConfig": {"fieldId": None, "order": "asc"}, "hiddenFieldIds": []
        }}],
        "filters": [], "sorts": [], "groupConfig": {"fieldId": None, "order": "asc"},
    }


def template_file(template_id: str) -> dict[str, Any]:
    if template_id == "performance":
        return performance_snapshot()
    root = ROOT / "app" / "data" / "templates"
    index = json.loads((root / "index.json").read_text(encoding="utf-8"))
    meta = next((x for x in index if x.get("id") == template_id), None)
    if not meta:
        raise RuntimeError(f"Unknown template: {template_id}")
    return json.loads((root / meta["filename"]).read_text(encoding="utf-8"))


def options(field: Mapping[str, Any]) -> list[str]:
    return [str(x["id"]) for x in field.get("options") or [] if isinstance(x, Mapping) and x.get("id")]


def option_label(field: Mapping[str, Any], value: Any) -> str:
    for item in field.get("options") or []:
        if isinstance(item, Mapping) and str(item.get("id")) == str(value):
            return str(item.get("label") or "")
    return ""


def choose_option(rng: random.Random, field: Mapping[str, Any], index: int, coverage: bool) -> Optional[str]:
    values = options(field)
    if not values:
        return None
    if coverage:
        return values[index % len(values)]
    weights = []
    for value in values:
        label = option_label(field, value).casefold()
        weights.append(4 if any(x in label for x in ("进行", "处理", "沟通", "面试", "active", "progress")) else 3 if any(x in label for x in ("完成", "关闭", "赢单", "发布", "done", "closed")) else 2)
    return rng.choices(values, weights=weights, k=1)[0]


def anchor_date(seed: int) -> date:
    text = str(abs(seed))
    try:
        return datetime.strptime(text[:8], "%Y%m%d").date() if len(text) >= 8 else date(2026, 1, 1) + timedelta(days=abs(seed) % 3650)
    except ValueError:
        return date(2026, 1, 1) + timedelta(days=abs(seed) % 3650)


def title_value(template_id: str, index: int) -> str:
    words = TITLE_WORDS.get(template_id, ("Demo record",))
    core = words[index % len(words)]
    if template_id in {"sales_crm", "asset_inventory"}:
        return f"{core}-{index + 1:03d}"
    if template_id == "recruiting_pipeline":
        return f"候选人 {index + 1:03d} · {core}"
    if template_id == "performance":
        return f"Performance row {index + 1:07d}"
    return f"{core} #{index + 1:03d}"


def is_status(field: Mapping[str, Any]) -> bool:
    name = str(field.get("name") or "").casefold()
    role = str((field.get("property") or {}).get("taskProfileRole") or "")
    return role == "status" or "状态" in name or "阶段" in name or name == "status"


def completed(label: str) -> bool:
    return any(x in label.casefold() for x in ("完成", "关闭", "赢单", "已发布", "入职", "done", "closed", "won"))


def not_started(label: str) -> bool:
    return any(x in label.casefold() for x in ("未开始", "待处理", "潜在", "待筛选", "backlog", "todo", "new"))


def text_value(name: str, template_id: str, index: int) -> str:
    lowered = name.casefold()
    if "模块" in lowered:
        return ("表格", "看板", "甘特", "日历", "自动化", "协作")[index % 6]
    if "联系人" in lowered or "姓名" in lowered:
        return ("陈晨", "林溪", "周然", "王璐", "李哲")[index % 5]
    if "邮箱" in lowered or "email" in lowered:
        return f"contact{index + 1:03d}@example.invalid"
    if "联系方式" in lowered or "电话" in lowered:
        return f"1380000{index % 10000:04d}"
    if "编号" in lowered or "sku" in lowered:
        return f"SEED-{template_id[:4].upper()}-{index + 1:05d}"
    if "客户" in lowered or "公司" in lowered:
        return title_value("sales_crm", index)
    return f"Seed demo {name} {index + 1:03d}"


def date_value(name: str, index: int, rng: random.Random, coverage: bool, anchor: date) -> str:
    offsets = (-30, -7, -1, 0, 1, 3, 7, 14, 30, 60)
    offset = offsets[index % len(offsets)] if coverage else rng.randint(-35, 75)
    lowered = name.casefold()
    if any(x in lowered for x in ("开始", "发现", "创建", "start")):
        offset -= 7
    if any(x in lowered for x in ("结束", "到期", "修复", "成交", "发布", "面试", "due", "end")):
        offset += 7
    return (anchor + timedelta(days=offset)).isoformat()


def generate_records(*, template_id: str, fields: Sequence[Mapping[str, Any]], count: int, member_ids: Sequence[int | str], seed: int, coverage: bool = False) -> list[dict[str, Any]]:
    rng = random.Random(f"{seed}:{template_id}:{count}:{int(coverage)}")
    members = [str(x) for x in member_ids]
    anchor = anchor_date(seed)
    title_field = next((x for x in fields if str(x.get("type")) == "text"), None)
    status_field = next((x for x in fields if is_status(x)), None)
    rows: list[dict[str, Any]] = []
    for index in range(count):
        row: dict[str, Any] = {}
        status_value = choose_option(rng, status_field, index, coverage) if status_field else None
        status_label = option_label(status_field, status_value) if status_field else ""
        for field in fields:
            field_id, field_type = str(field.get("id") or ""), str(field.get("type") or "text")
            if not field_id:
                continue
            name = str(field.get("name") or field_id)
            prop = field.get("property") or {}
            role = str(prop.get("taskProfileRole") or "")
            if field is title_field:
                value: Any = title_value(template_id, index)
            elif field is status_field:
                value = status_value
            elif field_type in {"text", "richText", "longText"}:
                value = text_value(name, template_id, index)
            elif field_type == "member":
                if not members or (coverage and index % 11 == 0):
                    value = [] if prop.get("multiple", True) else None
                else:
                    member = members[index % len(members)] if coverage else rng.choice(members)
                    value = [member] if prop.get("multiple", True) else member
            elif field_type in {"select", "singleSelect"}:
                value = choose_option(rng, field, index, coverage)
            elif field_type in {"multiSelect", "multipleSelect"}:
                choices = options(field)
                value = choices[: min(len(choices), 1 + index % 2)]
            elif field_type == "progress" or role == "progress":
                value = 100 if completed(status_label) else 0 if not_started(status_label) else (10 + (index * 17) % 81 if coverage else rng.randint(10, 90))
            elif field_type == "rating" or role == "priority":
                maximum = int(prop.get("max") or 5)
                value = 1 + index % maximum if coverage else rng.randint(1, maximum)
            elif field_type in {"number", "currency", "percent"}:
                value = round(5_000 + (index % 37) * 3_750 + rng.random() * 500, 2) if any(x in name for x in ("金额", "预算", "价格")) else index if coverage else rng.randint(0, 10_000)
            elif field_type in {"date", "datetime"}:
                value = date_value(name, index, rng, coverage, anchor)
            elif field_type in {"url", "link"}:
                value = f"https://example.invalid/qtable/{template_id}/{index + 1}"
            elif field_type == "email":
                value = f"seed-contact-{index + 1}@example.invalid"
            elif field_type == "phone":
                value = f"1380000{index % 10000:04d}"
            elif field_type in {"checkbox", "boolean"}:
                value = index % 3 == 0
            elif field_type == "attachment":
                value = []
            elif field_type in {"relation", "formula", "autoNumber"}:
                continue
            else:
                value = None
            row[field_id] = value
        rows.append(row)
    return rows


async def find_user(db, email: str) -> Optional[User]:
    result = await db.execute(select(User).where(func.lower(User.email) == email.casefold()))
    return result.scalars().first()


async def create_workspace(db, wid: str, name: str, owner_email: Optional[str]) -> tuple[Workspace, list[User], str]:
    if (await db.execute(select(Workspace).where(Workspace.id == wid))).scalars().first():
        raise SeedSafetyError(f"Seed workspace already exists: {wid}. Run --reset first.")
    if not wid.startswith(SEED_PREFIX) or not name.startswith(SEED_NAME_PREFIX):
        raise SeedSafetyError("Seed workspace must retain wkb_seed_ and [SEED] markers.")
    owner = await find_user(db, owner_email) if owner_email else (await db.execute(
        select(User).where(~User.email.like(SEED_EMAIL_LIKE)).order_by(User.id).limit(1)
    )).scalars().first()
    if owner_email and owner is None:
        raise RuntimeError(f"--owner-email user not found: {owner_email}")
    users = [owner] if owner else []
    password_hash = hash_password(seed_member_password())
    for i in range(1, 9 - len(users) + 1):
        email = f"{SEED_EMAIL_PREFIX}{i:02d}@{SEED_EMAIL_DOMAIN}"
        user = await find_user(db, email)
        if not user:
            user = User(email=email, name=f"Seed Member {i:02d}", password_hash=password_hash)
            db.add(user)
            await db.flush()
        users.append(user)
        if len(users) == 8:
            break
    ws = Workspace(id=wid, name=name, experience_mode="advanced")
    root = build_seed_root_item(wid, name)
    db.add(ws)
    db.add(root)
    await db.flush()
    for i, user in enumerate(users):
        db.add(WorkspaceMember(user_id=user.id, workspace_id=wid, role=WorkspaceRole.owner if i == 0 else WorkspaceRole.editor))
    await db.commit()
    return ws, users, root.id


async def create_table(db, *, wid: str, parent_id: str, owner_id: int, plan: TablePlan, order_index: int, seed: int, member_ids: Sequence[int], chunk_size: int, coverage: bool) -> tuple[str, list[str]]:
    table_id = f"seed_tbl_{plan.template_id}_{token(wid, plan.template_id, seed, length=10)}"
    if plan.template_id == "performance":
        snapshot = performance_snapshot()
    else:
        _, snapshot = await resolve_template_for_use(db, plan.template_id, user_id=owner_id, workspace_id=wid, target_table_id=table_id)
    views = snapshot.get("views") or []
    db.add(WorkspaceItem(id=table_id, workspace_id=wid, type="table", name=plan.name, parent_id=parent_id, order_index=order_index, default_view_id=str(views[0].get("id")) if views else None))
    await db.flush()
    await init_table_db(db, table_id, template_snapshot=snapshot)
    rows = generate_records(template_id=plan.template_id, fields=snapshot.get("fields") or [], count=plan.records, member_ids=member_ids, seed=seed, coverage=coverage)
    ids: list[str] = []
    for start in range(0, len(rows), chunk_size):
        created = await create_records_with_data(db, table_id, rows[start:start + chunk_size], created_by_user_id=owner_id, actor_type="system", source="dev_seed", trace_id=f"seed:{wid}:{plan.template_id}:{start // chunk_size}")
        ids.extend(str(x["id"]) for x in created)
    return table_id, ids


async def coverage_extras(db, *, wid: str, parent_id: str, owner_id: int, member_ids: Sequence[int], table_records: Mapping[str, Sequence[str]], seed: int) -> None:
    table_id = next(iter(table_records))
    records = list(table_records[table_id])
    if not records:
        return
    now = datetime.now(timezone.utc)
    first_inbox: Optional[str] = None
    statuses = ("pending", "archived", "ignored", "converted", "duplicate")
    for i in range(12):
        item_id = f"seed_inbox_{token(wid, seed, i, length=20)}"
        first_inbox = first_inbox or item_id
        source = f"Seed captured quote {i}"
        digest = hashlib.sha256(source.encode()).hexdigest()
        status = statuses[i % len(statuses)]
        db.add(SourceInboxItem(id=item_id, user_id=owner_id, workspace_id=wid, source_id=f"seed-source-{i:03d}", source_type="qnote", url=f"https://example.invalid/source/{i}", canonical_url=f"https://example.invalid/source/{i}", page_title=f"Seed source {i + 1}", quote=source, annotation=f"QA annotation {i}", anchor={"type": "text", "index": i}, captured_at=now - timedelta(days=i), tags=["seed", "coverage"], content_hash=digest, request_fingerprint=hashlib.sha256(f"{digest}:{i}".encode()).hexdigest(), status=status, duplicate_of_id=first_inbox if status == "duplicate" else None, target_table_id=table_id if status == "converted" else None, target_record_id=records[i % len(records)] if status == "converted" else None, converted_at=now if status == "converted" else None))
    comments: list[str] = []
    for i in range(8):
        author = int(member_ids[i % len(member_ids)])
        comment_id = f"seed_comment_{token(wid, seed, i, length=18)}"
        db.add(RecordComment(id=comment_id, workspace_id=wid, table_id=table_id, record_id=records[i % len(records)], author_id=author, parent_comment_id=comments[0] if i == 1 and comments else None, body=f"Seed QA comment {i + 1}: 请确认这个状态。", client_mutation_id=f"seed-{seed}-{i}"))
        comments.append(comment_id)
        if len(member_ids) > 1:
            mentioned = int(member_ids[(i + 1) % len(member_ids)])
            db.add(RecordCommentMention(id=f"seed_mention_{token(comment_id, mentioned, length=18)}", comment_id=comment_id, user_id=mentioned))
            db.add(UserNotification(id=f"seed_notif_{token(comment_id, mentioned, length=20)}", recipient_user_id=mentioned, type="mention", actor_id=author, workspace_id=wid, table_id=table_id, record_id=records[i % len(records)], comment_id=comment_id, event_id=f"seed-event-{i}", dedupe_key=f"seed:{wid}:{comment_id}:{mentioned}", read_at=now if i % 3 == 0 else None))
    for i, enabled in enumerate((False, True)):
        db.add(AutomationRule(id=f"seed_auto_{token(wid, seed, i, length=20)}", workspace_id=wid, table_id=table_id, name=f"[SEED] QA Automation {i + 1}", description="Coverage data for automation UI.", enabled=enabled, trigger={"type": "record.updated"}, conditions={"op": "and", "items": []}, actions=[{"type": "notify", "recipientUserIds": [owner_id], "notificationType": "automation", "message": "[SEED] Automation coverage notification"}], timezone="Asia/Shanghai", run_as_user_id=owner_id, created_by_user_id=owner_id, updated_by_user_id=owner_id))
    dashboard_id = f"dsb_seed_{token(wid, seed, length=18)}"
    db.add(Dashboard(id=dashboard_id, workspace_id=wid, description="[SEED] QA dashboard", is_public=False, published_by_user_id=owner_id))
    db.add(DashboardWidget(id=f"wdg_seed_{token(dashboard_id, 'metric', length=18)}", dashboard_id=dashboard_id, type="metric", title="Seed Records", layout={"x": 0, "y": 0, "w": 4, "h": 2}, config={"tableId": table_id, "dimensionFieldId": None, "metric": {"aggregation": "count", "fieldId": None}, "filters": [], "sort": {"by": "value", "order": "desc"}, "limit": 50}, order_index=0))
    db.add(WorkspaceItem(id=dashboard_id, workspace_id=wid, type="dashboard", name="[SEED] QA Dashboard", parent_id=parent_id, order_index=1000))
    await db.commit()


async def validate_workspace_tree(db, wid: str, root_id: str) -> None:
    items = list((await db.execute(select(WorkspaceItem).where(WorkspaceItem.workspace_id == wid))).scalars().all())
    roots = [item for item in items if item.parent_id is None]
    if len(roots) != 1 or roots[0].id != root_id or roots[0].type != "folder":
        found = [(item.id, item.type) for item in roots]
        raise RuntimeError(f"Invalid seed workspace tree: expected one folder root {root_id}, found {found}")
    misplaced = [(item.id, item.parent_id) for item in items if item.id != root_id and item.parent_id != root_id]
    if misplaced:
        raise RuntimeError(f"Invalid seed workspace tree: items are not attached to root {root_id}: {misplaced}")


async def reset_workspace(db, wid: str) -> bool:
    ws = (await db.execute(select(Workspace).where(Workspace.id == wid))).scalars().first()
    if not ws:
        print(f"Nothing to reset: {wid}")
        return False
    if not wid.startswith(SEED_PREFIX) or not str(ws.name or "").startswith(SEED_NAME_PREFIX):
        raise SeedSafetyError("Refusing reset: workspace lacks seed safety markers.")
    items = list((await db.execute(select(WorkspaceItem).where(WorkspaceItem.workspace_id == wid))).scalars().all())
    table_ids = [x.id for x in items if x.type == "table"]
    dashboard_ids = [x.id for x in items if x.type == "dashboard"]
    comment_ids = list((await db.execute(select(RecordComment.id).where(RecordComment.workspace_id == wid))).scalars().all())
    if comment_ids:
        await db.execute(delete(RecordCommentMention).where(RecordCommentMention.comment_id.in_(comment_ids)))
    await db.execute(delete(UserNotification).where(UserNotification.workspace_id == wid))
    await db.execute(delete(RecordComment).where(RecordComment.workspace_id == wid))
    await db.execute(delete(SourceInboxItem).where(SourceInboxItem.workspace_id == wid))
    rule_ids = list((await db.execute(select(AutomationRule.id).where(AutomationRule.workspace_id == wid))).scalars().all())
    if rule_ids:
        await db.execute(delete(AutomationExecution).where(AutomationExecution.automation_id.in_(rule_ids)))
    await db.execute(delete(AutomationEvent).where(AutomationEvent.workspace_id == wid))
    await db.execute(delete(AutomationRule).where(AutomationRule.workspace_id == wid))
    if dashboard_ids:
        await db.execute(delete(DashboardWidget).where(DashboardWidget.dashboard_id.in_(dashboard_ids)))
    await db.execute(delete(Dashboard).where(Dashboard.workspace_id == wid))
    await db.commit()
    for table_id in table_ids:
        await delete_table_data_db(db, table_id)
    item_ids = [x.id for x in items]
    if item_ids:
        await db.execute(delete(WorkspaceItemPermission).where(WorkspaceItemPermission.item_id.in_(item_ids)))
    await db.execute(delete(WorkspaceItem).where(WorkspaceItem.workspace_id == wid))
    await db.execute(delete(WorkspaceMember).where(WorkspaceMember.workspace_id == wid))
    await db.execute(delete(Workspace).where(Workspace.id == wid))
    await db.commit()
    seed_users = (await db.execute(select(User).where(User.email.like(SEED_EMAIL_LIKE)))).scalars().all()
    for user in seed_users:
        count = await db.scalar(select(func.count()).select_from(WorkspaceMember).where(WorkspaceMember.user_id == user.id))
        if not count:
            await db.delete(user)
    await db.commit()
    print(f"Reset complete: {wid}")
    return True


def print_plan(args: argparse.Namespace, plans: Sequence[TablePlan]) -> None:
    driver, host, database = db_target()
    wid = args.workspace_id or workspace_id(args.profile)
    name = args.workspace_name or workspace_name(args.profile)
    print(f"QTable Dev Seed\n  profile: {args.profile}\n  seed: {args.seed}\n  database: {driver}://{host}/{database}\n  workspace: {wid} ({name})\n  mode: {'RESET' if args.reset else 'DRY RUN' if args.dry_run else 'WRITE'}")
    if not args.reset:
        for plan in plans:
            print(f"  - {plan.name}: {plan.records:,} rows ({plan.template_id})")
        print(f"  total rows: {sum(x.records for x in plans):,}")
        if args.profile == "coverage":
            print("  extras: source inbox, comments, mentions, notifications, automation, dashboard")


async def run(args: argparse.Namespace) -> None:
    plans = plans_for(args.profile, args.records)
    print_plan(args, plans)
    require_write_opt_in(dry_run=args.dry_run, profile=args.profile)
    if args.dry_run:
        for plan in plans:
            if not template_file(plan.template_id).get("fields"):
                raise RuntimeError(f"Template has no fields: {plan.template_id}")
        print("Dry run OK: no database writes performed.")
        return
    if settings.DATA_BACKEND != "db":
        raise SeedSafetyError("Dev seed requires DATA_BACKEND=db.")
    wid = args.workspace_id or workspace_id(args.profile)
    name = args.workspace_name or workspace_name(args.profile)
    async with AsyncSessionLocal() as db:
        if args.reset:
            await reset_workspace(db, wid)
            return
        await sync_system_templates(db)
        _, users, root_id = await create_workspace(db, wid, name, args.owner_email)
        owner_id = int(users[0].id)
        member_ids = [int(x.id) for x in users]
        table_records: dict[str, list[str]] = {}
        for i, plan in enumerate(plans):
            print(f"Creating {plan.name}: {plan.records:,} rows ...")
            table_id, ids = await create_table(db, wid=wid, parent_id=root_id, owner_id=owner_id, plan=plan, order_index=i, seed=args.seed, member_ids=member_ids, chunk_size=args.chunk_size, coverage=args.profile == "coverage")
            table_records[table_id] = ids
        if args.profile == "coverage":
            await coverage_extras(db, wid=wid, parent_id=root_id, owner_id=owner_id, member_ids=member_ids, table_records=table_records, seed=args.seed)
        await validate_workspace_tree(db, wid, root_id)
        print(f"Seed complete: {wid} ({sum(map(len, table_records.values())):,} rows)")
        print(f"  owner: {users[0].email} (user_id={owner_id})")
        if len(users) > 1:
            print(f"  members: {', '.join(x.email for x in users[1:])}")
            print(f"  member password (dev only): {seed_member_password()}")
        print(f"  root: {root_id}")
        print("  workspace tree: OK")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Create repeatable QTable development/demo data safely.")
    p.add_argument("--profile", choices=("realistic", "coverage", "performance"), default="realistic")
    p.add_argument("--records", type=int, default=None, help="Scale realistic/coverage or set exact performance rows.")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--reset", action="store_true")
    p.add_argument("--workspace-id", default=None)
    p.add_argument("--workspace-name", default=None)
    p.add_argument("--owner-email", default=None)
    p.add_argument("--chunk-size", type=int, default=250)
    return p


def main() -> int:
    p = parser()
    args = p.parse_args()
    if args.records is not None and args.records < 1:
        p.error("--records must be >= 1")
    if not 1 <= args.chunk_size <= 1000:
        p.error("--chunk-size must be between 1 and 1000")
    if args.workspace_id and not args.workspace_id.startswith(SEED_PREFIX):
        p.error(f"--workspace-id must start with {SEED_PREFIX}")
    if args.workspace_name and not args.workspace_name.startswith(SEED_NAME_PREFIX):
        p.error(f"--workspace-name must start with {SEED_NAME_PREFIX}")
    if args.reset and args.dry_run:
        p.error("--reset and --dry-run are mutually exclusive")
    try:
        asyncio.run(run(args))
    except (SeedSafetyError, RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
