"""
审计日志模块 - FastAPI 平迁（自 app/api/audit_logs.py，蓝图挂载前缀 /api/v1）

路径/方法/状态码/响应字段与原 v1 完全一致（/api/v1/audit-logs），
前端与 CI 脚本零改动。全部端点为同步 def，运行于 RequestContextMiddleware
push 的 app context 内，直接复用 数据库会话（app/database.py ContextVar 作用域）。

鉴权映射：
- @jwt_required() → Depends(_current_user)（内部复用 deps.get_current_user）

可见域过滤（IDOR/越权修复；v1 对非管理员未做任何过滤，任何登录用户可读全部日志）：
- 管理员（role ∈ {admin, super_admin}，与 v1 export 的管理员口径一致）：可见全部
- 普通用户：仅可见 自己产生的日志（user_id = 当前用户）∪ 所在组织的日志
  （organization_id ∈ 用户活跃组织）；越权/不可见 → 404，与 v1 错误风格一致
- 列表 / 详情 / 统计 / 导出四个查询端点统一套用

其他修正：
- v1 CSV 导出使用不存在的 log.details 字段（模型字段为 changes），v1 实际必然
  AttributeError → 500；平迁改为 details 属性存在则用之，否则回退 log.changes

会话生命周期：同步端点统一加 @release_session，防 NullPool 连接泄漏。
"""

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func as sa_func
from sqlalchemy import or_

from app.api.v2.deps import get_current_user, query_int, query_str, release_session
from app.core.logging import get_logger
from app.extensions import db
from app.models.audit_log import AuditLog
from app.models.organization import OrganizationMember
from app.models.user import User
from sqlalchemy import select

logger = get_logger(__name__)

router = APIRouter(tags=["audit-logs"])

ADMIN_ROLES = ("admin", "super_admin")


# ---------------------------------------------------------------------------
# 鉴权包装 / 响应构造 / 可见域过滤
# ---------------------------------------------------------------------------

async def _current_user(request: Request) -> User:
    """鉴权依赖：在事件循环线程内执行同步 get_current_user，session 随 app context 回收"""
    return get_current_user(request)


def _request_id() -> str:
    """读取 RequestContextMiddleware 写入 scope state 的 request_id"""
    try:
        from app.core.runtime import ctx

        return ctx.get_request_id() or ""
    except Exception:
        return ""


def _success(data=None, message: str = "success", code: int = 200) -> JSONResponse:
    """等价 v1 success_response：{"code","message","data","timestamp"}"""
    return JSONResponse(
        status_code=code,
        content={
            "code": code,
            "message": message,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
        },
    )


def _error(code: int, message: str, errors=None) -> JSONResponse:
    """等价 v1 error_response：{"code","message","errors","request_id","timestamp"}"""
    return JSONResponse(
        status_code=code,
        content={
            "code": code,
            "message": message,
            "errors": errors,
            "request_id": _request_id(),
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
        },
    )


def _is_admin(user: User) -> bool:
    return user.role in ADMIN_ROLES


def _user_org_ids(user_id: int) -> List[int]:
    """用户所属（活跃）组织 ID 列表"""
    return [
        m.organization_id
        for m in db.session.scalars(select(OrganizationMember).filter_by(user_id=user_id, is_active=True)).all()]


def _visible_condition(user: User):
    """
    可见域过滤条件（IDOR 修复）：
    管理员 → None（不过滤，可见全部）；
    普通用户 → 自己的日志 ∪ 所在组织的日志。
    """
    if _is_admin(user):
        return None
    org_ids = _user_org_ids(user.id)
    cond = AuditLog.user_id == user.id
    if org_ids:
        cond = or_(cond, AuditLog.organization_id.in_(org_ids))
    return cond


def _apply_visible_domain(query, user: User):
    cond = _visible_condition(user)
    return query.filter(cond) if cond is not None else query


def _get_visible_log(log_id: int, user: User) -> Optional[AuditLog]:
    """按 id 取日志并套用可见域过滤：不存在/越权 → None → 404"""
    log = db.session.get(AuditLog, log_id)
    if not log:
        return None
    cond = _visible_condition(user)
    if cond is None:
        return log
    visible = db.session.scalar(select(AuditLog).filter(AuditLog.id == log_id).filter(cond))
    return visible


def _parse_iso(value: str) -> Optional[datetime]:
    """解析 ISO 时间（兼容 Z 后缀），失败返回 None"""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# ── 审计日志查询 ──────────────────────────────────────────────────────────────
# 注意：/stats、/export 必须注册在 /{log_id} 之前，避免被路径参数吞掉

@router.get("/api/v1/audit-logs")
@release_session
def get_audit_logs(request: Request, user: User = Depends(_current_user)):
    """
    获取审计日志列表（可见域过滤）

    查询参数:
        page: 页码 (默认 1)
        per_page: 每页数量 (默认 20)
        user_id: 按用户过滤
        action: 按操作类型过滤
        resource_type: 按资源类型过滤
        start_time: 开始时间 (ISO 格式)
        end_time: 结束时间 (ISO 格式)
    """
    page = query_int(request, "page", 1)
    per_page = query_int(request, "per_page", 20)
    filter_user_id = query_int(request, "user_id", 0) or None
    action = query_str(request, "action").strip()
    resource_type = query_str(request, "resource_type").strip()
    start_time = query_str(request, "start_time").strip()
    end_time = query_str(request, "end_time").strip()

    query = _apply_visible_domain(select(AuditLog), user)

    if filter_user_id:
        query = query.filter(AuditLog.user_id == filter_user_id)
    if action:
        query = query.filter(AuditLog.action == action)
    if resource_type:
        query = query.filter(AuditLog.resource_type == resource_type)
    if start_time:
        start_dt = _parse_iso(start_time)
        if start_dt is None:
            return _error(400, "start_time 格式无效")
        query = query.filter(AuditLog.created_at >= start_dt)
    if end_time:
        end_dt = _parse_iso(end_time)
        if end_dt is None:
            return _error(400, "end_time 格式无效")
        query = query.filter(AuditLog.created_at <= end_dt)

    total = db.session.scalar(select(sa_func.count()).select_from(query.subquery()))
    logs = db.session.scalars(
        query.order_by(AuditLog.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    ).all()

    return _success(data={
        "items": [log.to_dict() for log in logs],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": (total + per_page - 1) // per_page,
    })


@router.get("/api/v1/audit-logs/stats")
@release_session
def get_audit_stats(request: Request, user: User = Depends(_current_user)):
    """
    获取审计日志统计（可见域过滤）

    查询参数:
        days: 统计天数（默认 30）
    """
    days = query_int(request, "days", 30)
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)

    # 按操作类型统计
    action_stats = db.session.execute(
        _apply_visible_domain(
            select(AuditLog.action, sa_func.count(AuditLog.id).label("count")).filter(
                AuditLog.created_at >= since
            ),
            user,
        ).group_by(AuditLog.action)
    ).all()

    # 按资源类型统计
    resource_stats = db.session.execute(
        _apply_visible_domain(
            select(AuditLog.resource_type, sa_func.count(AuditLog.id).label("count")).filter(
                AuditLog.created_at >= since
            ),
            user,
        ).group_by(AuditLog.resource_type)
    ).all()

    # 最近活跃用户
    active_users = db.session.execute(
        _apply_visible_domain(
            select(AuditLog.user_id, sa_func.count(AuditLog.id).label("count")).filter(
                AuditLog.created_at >= since, AuditLog.user_id.isnot(None)
            ),
            user,
        ).group_by(AuditLog.user_id).order_by(sa_func.count(AuditLog.id).desc()).limit(10)
    ).all()

    return _success(data={
        "period_days": days,
        "by_action": {row.action: row.count for row in action_stats},
        "by_resource": {row.resource_type: row.count for row in resource_stats},
        "active_users": [{"user_id": row.user_id, "count": row.count} for row in active_users],
    })


@router.get("/api/v1/audit-logs/export")
@release_session
def export_audit_logs(request: Request, user: User = Depends(_current_user)):
    """
    导出审计日志为 CSV/JSON（管理员专用 + 可见域过滤）

    查询参数:
        format: csv 或 json（默认 csv）
        action: 操作类型筛选
        resource_type: 资源类型筛选
        days: 时间范围（默认 30 天）
    """
    if not _is_admin(user):
        return _error(403, "仅管理员可导出审计日志")

    export_format = query_str(request, "format", "csv")
    action_filter = query_str(request, "action")
    resource_filter = query_str(request, "resource_type")
    days = query_int(request, "days", 30)

    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
    query = _apply_visible_domain(
        select(AuditLog).filter(AuditLog.created_at >= since), user
    )
    if action_filter:
        query = query.filter_by(action=action_filter)
    if resource_filter:
        query = query.filter_by(resource_type=resource_filter)

    logs = db.session.scalars(query.order_by(AuditLog.created_at.desc()).limit(10000)).all()

    if export_format == "json":
        data = [log.to_dict() for log in logs]
        return _success(data=data)

    # CSV 导出
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "用户ID", "操作", "资源类型", "资源ID", "详情", "IP", "时间"])
    for log in logs:
        # v1 使用不存在的 log.details 字段（模型字段为 changes），此处修复
        details = getattr(log, "details", None) or log.changes
        writer.writerow([
            log.id, log.user_id, log.action, log.resource_type,
            log.resource_id, str(details or ""), log.ip_address,
            log.created_at.isoformat() if log.created_at else "",
        ])

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=audit-logs-{days}d.csv"},
    )


@router.get("/api/v1/audit-logs/{log_id}")
@release_session
def get_audit_log(log_id: int, user: User = Depends(_current_user)):
    """获取单条审计日志详情（可见域过滤，越权/不存在 → 404）"""
    log = _get_visible_log(log_id, user)
    if not log:
        return _error(404, "审计日志不存在")
    return _success(data=log.to_dict())
