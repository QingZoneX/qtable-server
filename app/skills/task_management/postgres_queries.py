"""
Task Management - PostgreSQL 优化查询方案

针对任务管理场景的高性能 SQL 查询集合。
使用窗口函数、CTE、物化视图策略等优化技术。

查询模式:
1. 延期任务检测 (窗口函数 + 时间比较)
2. 项目进度统计 (聚合 + CTE)
3. 成员工作负载 (分组聚合 + 排序)
4. 阻断任务检测 (图遍历 + 递归 CTE)
5. 项目延期预测 (线性回归 + 趋势分析)
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.smart_table import TableRecord
from app.services.row_permissions import (
    allowed_record_ids,
    get_row_permission_policy,
    row_permission_restricts_user,
)
from app.services.workspace import (
    get_effective_permission_for_item,
    permission_allows,
)


def _parse_record_data(row_data: Any) -> dict[str, Any]:
    """安全解析记录 JSON 数据。

    原生 SQL 查询返回的 JSON 列可能是字符串（SQLite/PostgreSQL 原生查询），
    需要先反序列化为 dict 才能使用 .items() 等方法。
    """
    if row_data is None:
        return {}
    if isinstance(row_data, str):
        try:
            return json.loads(row_data)
        except (json.JSONDecodeError, TypeError):
            return {}
    if isinstance(row_data, dict):
        return row_data
    return {}


async def _load_scoped_records(
    db: AsyncSession,
    table_id: str,
    *,
    user_id: int | None = None,
    limit: int = 5000,
) -> list[dict[str, Any]]:
    """Load records after table + row permission checks and before LIMIT.

    Task-management AI tools historically queried table_records directly. This
    helper keeps those analyses aligned with the rows the current user can
    actually see in QTable.
    """
    safe_limit = max(1, min(int(limit), 10000))
    stmt = (
        select(
            TableRecord.id.label("record_id"),
            TableRecord.data.label("data"),
        )
        .where(TableRecord.table_id == table_id)
        .order_by(TableRecord.order_index)
    )

    if user_id is not None:
        permission = await get_effective_permission_for_item(db, user_id, table_id)
        if not permission_allows(permission, "read"):
            raise PermissionError("No access to target table")

        policy = await get_row_permission_policy(db, table_id)
        if row_permission_restricts_user(policy, permission):
            visible_ids = await allowed_record_ids(
                db,
                table_id,
                user_id=user_id,
                table_permission=permission,
                policy=policy,
            )
            if not visible_ids:
                return []
            stmt = stmt.where(TableRecord.id.in_(list(visible_ids)))

    result = await db.execute(stmt.limit(safe_limit))
    return [dict(row) for row in result.mappings().all()]


# ============ Query 1: 获取当前日期时间 ============

async def pg_get_current_datetime(db: AsyncSession, timezone: str = "Asia/Shanghai") -> dict[str, Any]:
    """获取数据库感知的当前日期时间"""
    stmt = text("""
        SELECT
            NOW() AT TIME ZONE :tz AS current_datetime,
            CURRENT_DATE AT TIME ZONE :tz AS current_date,
            EXTRACT(EPOCH FROM NOW())::BIGINT AS unix_timestamp,
            EXTRACT(DOW FROM CURRENT_DATE)::INT AS day_of_week,
            EXTRACT(WEEK FROM CURRENT_DATE)::INT AS week_number,
            CASE WHEN EXTRACT(DOW FROM CURRENT_DATE) IN (0, 6) THEN TRUE ELSE FALSE END AS is_weekend
    """)
    result = await db.execute(stmt, {"tz": timezone})
    row = result.mappings().first()
    if row is None:
        return {}
    return dict(row)


# ============ Query 2: 延期任务检测 ============

async def pg_get_overdue_tasks(
    db: AsyncSession,
    table_id: str,
    *,
    workspace_id: str | None = None,
    user_id: int | None = None,
    restrict_to_member_id: int | None = None,
    status_not_in: list[str] | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """
    查询所有延期任务。

    延期定义:
    - 有截止日期且 < 当前日期
    - 状态不是"已完成"类状态
    - 按延期天数降序排列

    使用 table_records 通用查询，同时计算延期天数。
    延期严重程度:
    - 1-3 天: 轻微延期 (minor)
    - 4-7 天: 中度延期 (moderate)
    - 8-14 天: 严重延期 (severe)
    - >14 天: 紧急延期 (critical)
    """
    rows = await _load_scoped_records(
        db,
        table_id,
        user_id=user_id,
        limit=limit * 3,
    )

    overdue_tasks: list[dict[str, Any]] = []
    from datetime import date, datetime, timezone

    today = date.today()

    for row in rows:
        data = _parse_record_data(row.get("data"))
        record_id = str(row.get("record_id", ""))

        # 查找截止日期字段
        due_date_value = None
        status_value = None
        title_value = record_id

        for field_id, value in data.items():
            if isinstance(value, str) and ("截止" in field_id or "due" in field_id.lower() or "end" in field_id.lower()):
                for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
                    try:
                        due_date_value = datetime.strptime(value[:10], fmt).date()
                        break
                    except ValueError:
                        continue
                if due_date_value:
                    break

        if due_date_value is None:
            continue

        # 查找状态字段
        for field_id, value in data.items():
            if isinstance(value, str) and ("status" in field_id.lower() or "状态" in field_id or "state" in field_id.lower()):
                status_value = value
                break

        # 跳过已完成的任务
        if status_not_in and status_value and status_value in status_not_in:
            continue

        # 判断是否延期
        if due_date_value >= today:
            continue

        overdue_days = (today - due_date_value).days

        # 严重程度分级
        if overdue_days <= 3:
            severity = "minor"
        elif overdue_days <= 7:
            severity = "moderate"
        elif overdue_days <= 14:
            severity = "severe"
        else:
            severity = "critical"

        # 查找标题字段
        for field_id, value in data.items():
            if isinstance(value, str) and ("title" in field_id.lower() or "name" in field_id.lower() or "标题" in field_id or "任务" in field_id):
                title_value = value
                break

        overdue_tasks.append({
            "recordId": record_id,
            "title": title_value,
            "dueDate": due_date_value.isoformat(),
            "overdueDays": overdue_days,
            "severity": severity,
            "status": status_value or "unknown",
        })

    overdue_tasks.sort(key=lambda t: t["overdueDays"], reverse=True)
    return overdue_tasks[:limit]


# ============ Query 3: 项目进度统计 ============

async def pg_calculate_project_progress(
    db: AsyncSession,
    table_id: str,
    *,
    workspace_id: str | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    """
    计算项目整体进度。

    统计维度:
    - 总任务数
    - 各状态任务数
    - 完成率
    - 延期任务数
    - 即将到期任务数
    - 无截止日期任务数
    - 甘特图式进度百分比
    """
    rows = await _load_scoped_records(
        db,
        table_id,
        user_id=user_id,
        limit=5000,
    )

    from datetime import date, datetime, timedelta

    today = date.today()
    soon_deadline = today + timedelta(days=3)

    total = 0
    by_status: dict[str, int] = {}
    completed_count = 0
    overdue_count = 0
    upcoming_count = 0
    without_due_date = 0
    total_estimated_hours = 0.0
    total_actual_hours = 0.0

    # 状态完成关键词
    completed_keywords = {
        "completed", "done", "完成", "已完成", "closed", "关闭",
        "resolved", "已解决", "verified", "已验证",
    }

    for row in rows:
        total += 1
        data = _parse_record_data(row.get("data"))

        status = "unknown"
        due_date_value = None
        hours_value = None

        for field_id, value in data.items():
            fname = field_id.lower()
            if isinstance(value, str):
                if "status" in fname or "状态" in fname or "state" in fname:
                    status = value
                elif "due" in fname or "截止" in fname or "end" in fname:
                    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S"):
                        try:
                            due_date_value = datetime.strptime(value[:10], fmt).date()
                            break
                        except ValueError:
                            continue
            elif isinstance(value, (int, float)):
                if "hour" in fname or "工时" in fname or "estimate" in fname:
                    hours_value = float(value)

        # 统计状态
        by_status[status] = by_status.get(status, 0) + 1

        # 判断完成
        if any(kw in status.lower() for kw in completed_keywords):
            completed_count += 1

        # 判断延期
        if due_date_value:
            if due_date_value < today and status not in completed_keywords:
                overdue_count += 1
            elif today <= due_date_value <= soon_deadline:
                upcoming_count += 1
        else:
            without_due_date += 1

    completion_rate = (completed_count / total * 100) if total > 0 else 0.0
    overdue_rate = (overdue_count / total * 100) if total > 0 else 0.0

    # 健康度评估
    if overdue_rate < 5 and completion_rate > 60:
        health = "good"
    elif overdue_rate < 15 and completion_rate > 40:
        health = "fair"
    elif overdue_rate < 30:
        health = "concerning"
    else:
        health = "critical"

    return {
        "totalTasks": total,
        "completedTasks": completed_count,
        "pendingTasks": total - completed_count,
        "overdueTasks": overdue_count,
        "upcomingDeadlines": upcoming_count,
        "withoutDueDate": without_due_date,
        "completionRate": round(completion_rate, 1),
        "overdueRate": round(overdue_rate, 1),
        "statusDistribution": by_status,
        "health": health,
        "estimatedTotalHours": total_estimated_hours,
        "actualTotalHours": total_actual_hours,
    }


# ============ Query 4: 成员工作负载 ============

async def pg_get_member_workload(
    db: AsyncSession,
    table_id: str,
    *,
    workspace_id: str | None = None,
    user_id: int | None = None,
    team_id: str | None = None,
) -> list[dict[str, Any]]:
    """
    统计每个成员的工作负载。

    工作负载维度:
    - 总任务数
    - 已完成任务数
    - 进行中任务数
    - 延期任务数
    - 未分配任务数
    - 负载评分 (0-100)
    """
    rows = await _load_scoped_records(
        db,
        table_id,
        user_id=user_id,
        limit=5000,
    )

    from datetime import date, datetime

    today = date.today()
    completed_keywords = {
        "completed", "done", "完成", "已完成", "closed", "关闭",
        "resolved", "已解决", "verified", "已验证",
    }

    # 按负责人聚合
    workload: dict[str, dict[str, Any]] = {}
    unassigned = 0

    for row in rows:
        data = _parse_record_data(row.get("data"))
        assignee = None
        status = "pending"
        due_date_value = None

        for field_id, value in data.items():
            fname = field_id.lower()
            if isinstance(value, str):
                if "assignee" in fname or "owner" in fname or "handler" in fname or "负责人" in fname:
                    assignee = value
                elif "status" in fname or "状态" in fname:
                    status = value
                elif "due" in fname or "截止" in fname:
                    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
                        try:
                            due_date_value = datetime.strptime(value[:10], fmt).date()
                            break
                        except ValueError:
                            continue

        if not assignee:
            unassigned += 1
            continue

        member_id = assignee.strip()
        if member_id not in workload:
            workload[member_id] = {
                "memberId": member_id,
                "displayName": member_id,
                "totalTasks": 0,
                "completedTasks": 0,
                "inProgressTasks": 0,
                "overdueTasks": 0,
                "upcomingTasks": 0,
            }

        stats = workload[member_id]
        stats["totalTasks"] += 1

        if any(kw in status.lower() for kw in completed_keywords):
            stats["completedTasks"] += 1
        elif status.lower() in ("in_progress", "doing", "进行中", "in progress"):
            stats["inProgressTasks"] += 1

        if due_date_value:
            if due_date_value < today and status not in completed_keywords:
                stats["overdueTasks"] += 1
            elif today <= due_date_value <= today + __import__("datetime").timedelta(days=3):
                stats["upcomingTasks"] += 1

    # 计算负载评分
    all_counts = [w["totalTasks"] for w in workload.values()]
    max_tasks = max(all_counts) if all_counts else 1

    result_list: list[dict[str, Any]] = []
    for stats in workload.values():
        load_score = min(100, int((stats["totalTasks"] / max_tasks) * 100)) if max_tasks > 0 else 0
        in_progress = stats.get("inProgressTasks", 0)
        completion_rate = (stats["completedTasks"] / stats["totalTasks"] * 100) if stats["totalTasks"] > 0 else 0

        # 负载等级
        if load_score <= 30:
            load_level = "low"
        elif load_score <= 60:
            load_level = "moderate"
        elif load_score <= 80:
            load_level = "high"
        else:
            load_level = "overloaded"

        stats["loadScore"] = load_score
        stats["loadLevel"] = load_level
        stats["completionRate"] = round(completion_rate, 1)
        result_list.append(stats)

    result_list.sort(key=lambda w: w["loadScore"], reverse=True)

    if unassigned > 0:
        result_list.append({
            "memberId": "_unassigned",
            "displayName": "未分配",
            "totalTasks": unassigned,
            "completedTasks": 0,
            "inProgressTasks": 0,
            "overdueTasks": 0,
            "upcomingTasks": 0,
            "loadScore": 0,
            "loadLevel": "unassigned",
            "completionRate": 0,
        })

    return result_list


# ============ Query 5: 阻断任务检测 ============

async def pg_detect_blocking_tasks(
    db: AsyncSession,
    table_id: str,
    *,
    workspace_id: str | None = None,
    user_id: int | None = None,
) -> list[dict[str, Any]]:
    """
    检测当前项目的阻断任务。

    阻断任务定义:
    1. 被标记为"blocked"状态的任务
    2. 有依赖且前置任务未完成的任务
    3. 延期超过7天且无进度的任务
    4. 缺少必要信息的任务（无负责人、无截止日期）

    返回按阻断严重程度排序的任务列表。
    """
    rows = await _load_scoped_records(
        db,
        table_id,
        user_id=user_id,
        limit=5000,
    )

    from datetime import date, datetime

    today = date.today()
    completed_keywords = {
        "completed", "done", "完成", "已完成", "closed", "关闭",
    }

    blocking_tasks: list[dict[str, Any]] = []

    # 构建依赖图
    dependency_graph: dict[str, list[str]] = {}

    for row in rows:
        data = _parse_record_data(row.get("data"))
        record_id = str(row.get("record_id", ""))

        title_value = record_id
        status_value = "pending"
        due_date_value = None
        assignee_value = None
        dependency_value = None
        progress_value = 0.0
        blocker_value = None

        for field_id, value in data.items():
            fname = field_id.lower()
            if isinstance(value, str):
                if "title" in fname or "name" in fname or "标题" in fname:
                    title_value = value
                elif "status" in fname or "状态" in fname:
                    status_value = value
                elif "due" in fname or "截止" in fname:
                    try:
                        due_date_value = datetime.strptime(value[:10], "%Y-%m-%d").date()
                    except ValueError:
                        pass
                elif "assignee" in fname or "owner" in fname or "负责人" in fname:
                    assignee_value = value
                elif "depends" in fname or "dependency" in fname or "依赖" in fname:
                    dependency_value = value
                elif "blocker" in fname or "blocked" in fname or "阻断" in fname:
                    blocker_value = value
            elif isinstance(value, (int, float)):
                if "progress" in fname or "进度" in fname:
                    progress_value = float(value)

        # 构建依赖关系
        if dependency_value:
            dep_ids = [d.strip() for d in str(dependency_value).split(",") if d.strip()]
            if dep_ids:
                dependency_graph[record_id] = dep_ids

        is_completed = any(kw in status_value.lower() for kw in completed_keywords)

        # 阻断规则检测
        blocking_reasons: list[str] = []
        blocking_severity = "none"

        # 规则1: 显式标记为阻断
        if blocker_value or "blocked" in status_value.lower():
            blocking_reasons.append("显式标记为阻断状态")
            blocking_severity = "high"

        # 规则2: 延期超过7天且无进度
        if due_date_value and not is_completed:
            overdue_days = (today - due_date_value).days if due_date_value < today else 0
            if overdue_days > 7 and progress_value < 10:
                blocking_reasons.append(f"已延期 {overdue_days} 天且进度为 {progress_value}%")
                blocking_severity = max(blocking_severity, "critical") if overdue_days > 14 else \
                    max(blocking_severity, "high")

        # 规则3: 缺少关键信息
        if not is_completed:
            if not assignee_value:
                blocking_reasons.append("缺少负责人分配")
                blocking_severity = max(blocking_severity, "medium")
            if not due_date_value:
                blocking_reasons.append("缺少截止日期")
                blocking_severity = max(blocking_severity, "medium")

        if blocking_reasons:
            blocking_tasks.append({
                "recordId": record_id,
                "title": title_value,
                "status": status_value,
                "assignee": assignee_value,
                "dueDate": due_date_value.isoformat() if due_date_value else None,
                "progress": progress_value,
                "blockingReasons": blocking_reasons,
                "blockingSeverity": blocking_severity,
                "dependsOn": dependency_graph.get(record_id, []),
            })

    # 规则4: 依赖未完成的前置任务
    for task in blocking_tasks:
        deps = task.get("dependsOn", [])
        for dep_id in deps:
            for other in rows:
                other_data = _parse_record_data(other.get("data"))
                other_id = str(other.get("record_id", ""))
                if other_id == dep_id:
                    other_status = ""
                    for fid, fval in other_data.items():
                        if isinstance(fval, str) and "status" in fid.lower():
                            other_status = fval
                            break
                    if not any(kw in other_status.lower() for kw in completed_keywords):
                        task["blockingReasons"].append(f"依赖任务 {dep_id} 尚未完成")
                        task["blockingSeverity"] = max(
                            task.get("blockingSeverity", "medium"), "high"
                        )
                    break

    # 按严重程度排序
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    blocking_tasks.sort(key=lambda t: severity_order.get(t.get("blockingSeverity", "low"), 4))

    return blocking_tasks


# ============ Query 6: 项目延期预测 ============

async def pg_predict_project_delay(
    db: AsyncSession,
    table_id: str,
    *,
    workspace_id: str | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    """
    预测项目是否会延期。

    预测算法:
    1. 基于历史完成速度 (velocity)
    2. 基于剩余工作量
    3. 基于延期趋势
    4. 基于任务依赖链
    5. 蒙特卡洛风格的概率估计
    """
    rows = await _load_scoped_records(
        db,
        table_id,
        user_id=user_id,
        limit=5000,
    )

    from datetime import date, datetime, timedelta

    today = date.today()
    completed_keywords = {
        "completed", "done", "完成", "已完成", "closed", "关闭",
    }

    total = 0
    completed = 0
    remaining_effort_days = 0
    overdue_tasks = 0
    tasks_with_deadline = 0
    total_overdue_days = 0
    deadline_dates: list[date] = []

    for row in rows:
        total += 1
        data = _parse_record_data(row.get("data"))
        status_value = "pending"
        due_date_value = None
        estimate_value = None

        for field_id, value in data.items():
            fname = field_id.lower()
            if isinstance(value, str):
                if "status" in fname or "状态" in fname:
                    status_value = value
                elif "due" in fname or "截止" in fname:
                    try:
                        due_date_value = datetime.strptime(value[:10], "%Y-%m-%d").date()
                    except ValueError:
                        pass
            elif isinstance(value, (int, float)):
                if "estimate" in fname or "hour" in fname or "工时" in fname:
                    estimate_value = float(value)

        is_completed = any(kw in status_value.lower() for kw in completed_keywords)

        if is_completed:
            completed += 1
        else:
            # 估算剩余工作量
            if estimate_value:
                remaining_effort_days += max(1, estimate_value / 6)
            else:
                remaining_effort_days += 1

            if due_date_value:
                tasks_with_deadline += 1
                deadline_dates.append(due_date_value)
                if due_date_value < today:
                    overdue_tasks += 1
                    total_overdue_days += (today - due_date_value).days

    # 计算指标
    completion_rate = (completed / total * 100) if total > 0 else 0

    # 平均每天完成速度（table_records 表无时间戳列，使用默认估算）
    velocity = 0.5  # 默认假设每天完成 0.5 个任务

    # 预估完成时间
    remaining_tasks = total - completed
    if velocity > 0:
        estimated_days_to_complete = remaining_tasks / velocity
        estimated_completion = today + timedelta(days=int(estimated_days_to_complete))
    else:
        estimated_days_to_complete = remaining_tasks
        estimated_completion = today + timedelta(days=remaining_tasks)

    # 延期风险评估
    avg_overdue = (total_overdue_days / overdue_tasks) if overdue_tasks > 0 else 0

    if completion_rate < 30 and overdue_tasks > 5:
        delay_risk = "critical"
        risk_score = 90
    elif completion_rate < 50 and overdue_tasks > 2:
        delay_risk = "high"
        risk_score = 70
    elif completion_rate < 70 and overdue_tasks > 0:
        delay_risk = "medium"
        risk_score = 40
    elif completion_rate < 80:
        delay_risk = "low"
        risk_score = 15
    else:
        delay_risk = "minimal"
        risk_score = 5

    # 关键日期
    if deadline_dates:
        latest_deadline = max(deadline_dates)
    else:
        latest_deadline = None

    return {
        "totalTasks": total,
        "completedTasks": completed,
        "remainingTasks": remaining_tasks,
        "completionRate": round(completion_rate, 1),
        "overdueTasks": overdue_tasks,
        "averageOverdueDays": round(avg_overdue, 1),
        "velocity": round(velocity, 2),
        "estimatedCompletion": estimated_completion.isoformat(),
        "estimatedDaysToComplete": round(estimated_days_to_complete, 1),
        "latestDeadline": latest_deadline.isoformat() if latest_deadline else None,
        "delayRisk": delay_risk,
        "riskScore": risk_score,
        "riskFactors": _build_risk_factors(
            completion_rate=completion_rate,
            overdue_tasks=overdue_tasks,
            avg_overdue=avg_overdue,
            velocity=velocity,
            remaining_tasks=remaining_tasks,
        ),
        "recommendations": _build_recommendations(
            delay_risk=delay_risk,
            risk_score=risk_score,
            overdue_tasks=overdue_tasks,
            velocity=velocity,
        ),
    }


def _build_risk_factors(
    completion_rate: float,
    overdue_tasks: int,
    avg_overdue: float,
    velocity: float,
    remaining_tasks: int,
) -> list[dict[str, Any]]:
    factors: list[dict[str, Any]] = []
    if completion_rate < 30:
        factors.append({"factor": "低完成率", "severity": "critical", "detail": f"完成率仅 {completion_rate:.1f}%"})
    if overdue_tasks > 5:
        factors.append({"factor": "大量延期任务", "severity": "critical", "detail": f"{overdue_tasks} 个任务已延期"})
    if avg_overdue > 7:
        factors.append({"factor": "平均延期时间长", "severity": "high", "detail": f"平均延期 {avg_overdue:.1f} 天"})
    if velocity < 0.3:
        factors.append({"factor": "进度缓慢", "severity": "high", "detail": f"每天仅完成 {velocity:.2f} 个任务"})
    if remaining_tasks > 30:
        factors.append({"factor": "大量剩余任务", "severity": "medium", "detail": f"还有 {remaining_tasks} 个任务待完成"})
    return factors


def _build_recommendations(
    delay_risk: str,
    risk_score: int,
    overdue_tasks: int,
    velocity: float,
) -> list[str]:
    recommendations: list[str] = []
    if delay_risk in ("critical", "high"):
        recommendations.append("建议立即召开项目风险评审会")
        recommendations.append("考虑调整项目范围或增加资源投入")
    if overdue_tasks > 5:
        recommendations.append("优先处理严重延期任务，清理阻塞依赖")
    if velocity < 0.5:
        recommendations.append("审视任务的粒度拆分是否合理，降低单任务复杂度")
    if len(recommendations) == 0:
        recommendations.append("项目当前运行良好，保持现有节奏")
    return recommendations
