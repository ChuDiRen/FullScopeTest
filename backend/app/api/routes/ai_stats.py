"""
AI 能力统计模块 - FastAPI 平迁（自 app/api/ai_stats.py，蓝图挂载前缀 /api/v1）

路径/方法/状态码/响应字段与原 v1 完全一致（/api/v1/ai/stats/...），
前端与 CI 脚本零改动。全部端点为同步 def，运行于 RequestContextMiddleware
push 的 app context 内，直接复用 数据库会话（app/database.py ContextVar 作用域）。

鉴权映射：
- @jwt_required() → Depends(_current_user)（内部复用 deps.get_current_user）
- 本模块无公开端点（v1 全部端点均带 @jwt_required）

IDOR 修复（按属主过滤，读 AIInvocationLog.user_id / PromptVersion.created_by 确认属主字段）：
- v1 的统计聚合对任意登录用户暴露全平台所有用户的调用数据（token 消耗、成本、
  prompt 版本内容统计），属水平越权信息泄露。平迁后：
  * AIInvocationLog 聚合一律按 AIInvocationLog.user_id == 当前用户过滤
  * prompt-versions-comparison 按 PromptVersion.created_by ∈ {当前用户, NULL(平台全局预置)} 过滤
- user_id 为 NULL 的系统级调用日志归平台所有，不计入任何用户的个人统计

会话生命周期：
- 同步端点统一加 @release_session（deps.release_session）
- 鉴权依赖包装为 async _current_user：在事件循环线程执行 get_current_user
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import case, func, or_

from app.api.v2.deps import get_current_user, query_int, query_str, release_session
from app.core.logging import get_logger
from app.models.ai_invocation_log import AIInvocationLog
from app.models.prompt_version import PromptVersion
from app.models.user import User
from sqlalchemy import select
from app.extensions import db

logger = get_logger(__name__)

router = APIRouter(tags=["ai-stats"])


# ---------------------------------------------------------------------------
# 鉴权包装 / 响应构造
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


# ==================== AI 概览统计 ====================


@router.get("/api/v1/ai/stats/overview")
@release_session
def get_ai_stats_overview(user: User = Depends(_current_user)):
    """
    获取 AI 功能概览统计（IDOR 修复：仅聚合当前用户自己的调用日志）

    返回:
        total_invocations: 总调用次数
        success_rate: 成功率 (%)
        total_tokens: 总 token 消耗
        total_cost: 总成本估算
        avg_latency_ms: 平均延迟
        features: 各功能模块的调用量分布
    """
    # 总调用次数
    total = (
        db.session.scalar(select(func.count(AIInvocationLog.id)).filter(AIInvocationLog.user_id == user.id))
        or 0
    )

    # 成功次数
    success_count = (
        db.session.scalar(
            select(func.count(AIInvocationLog.id))
            .filter(AIInvocationLog.user_id == user.id)
            .filter(AIInvocationLog.success == True)  # noqa: E712
        )
        or 0
    )

    # 成功率
    success_rate = round(success_count / total * 100, 2) if total > 0 else 0.0

    # 总 token 消耗
    total_tokens = (
        db.session.scalar(
            select(func.coalesce(func.sum(AIInvocationLog.total_tokens), 0)).filter(
                AIInvocationLog.user_id == user.id
            )
        )
        or 0
    )

    # 总成本
    total_cost = (
        db.session.scalar(
            select(func.coalesce(func.sum(AIInvocationLog.cost_estimate), 0.0)).filter(
                AIInvocationLog.user_id == user.id
            )
        )
        or 0.0
    )

    # 平均延迟
    avg_latency = (
        db.session.scalar(
            select(func.coalesce(func.avg(AIInvocationLog.latency_ms), 0)).filter(
                AIInvocationLog.user_id == user.id
            )
        )
        or 0
    )

    # 各功能模块调用量
    feature_rows = (
        db.session.execute(
            select(AIInvocationLog.feature, func.count(AIInvocationLog.id)).filter(
                AIInvocationLog.user_id == user.id
            ).group_by(AIInvocationLog.feature)
        ).all()
    )

    features = {row[0]: row[1] for row in feature_rows}

    return _success(
        data={
            "total_invocations": total,
            "success_rate": success_rate,
            "total_tokens": int(total_tokens),
            "total_cost": round(float(total_cost), 4),
            "avg_latency_ms": round(float(avg_latency), 1),
            "features": features,
        }
    )


# ==================== 成功率趋势 ====================


@router.get("/api/v1/ai/stats/success-rate-trend")
@release_session
def get_success_rate_trend(request: Request, user: User = Depends(_current_user)):
    """
    获取 AI 调用成功率趋势（IDOR 修复：仅聚合当前用户自己的调用日志）

    查询参数:
        days: 天数（默认 30）
        feature: 可选，按功能模块过滤

    返回:
        按天聚合的成功率趋势数据
    """
    days = query_int(request, "days", 30)
    feature = query_str(request, "feature").strip()

    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)

    query = (
        select(
            func.date(AIInvocationLog.created_at).label("date"),
            func.count(AIInvocationLog.id).label("total"),
            func.sum(case((AIInvocationLog.success == True, 1), else_=0)).label("success"),  # noqa: E712
        )
        .filter(AIInvocationLog.user_id == user.id)
        .filter(AIInvocationLog.created_at >= since)
    )

    if feature:
        query = query.filter(AIInvocationLog.feature == feature)

    query = query.group_by(func.date(AIInvocationLog.created_at)).order_by(
        func.date(AIInvocationLog.created_at)
    )

    rows = db.session.execute(query).all()

    trend = []
    for row in rows:
        total = row.total or 0
        success = row.success or 0
        trend.append(
            {
                "date": str(row.date),
                "total": total,
                "success": success,
                "success_rate": round(success / total * 100, 2) if total > 0 else 0.0,
            }
        )

    return _success(data=trend)


# ==================== 延迟趋势 ====================


@router.get("/api/v1/ai/stats/latency-trend")
@release_session
def get_latency_trend(request: Request, user: User = Depends(_current_user)):
    """
    获取平均响应时间趋势（IDOR 修复：仅聚合当前用户自己的调用日志）

    查询参数:
        days: 天数（默认 30）
        feature: 可选，按功能模块过滤

    返回:
        按天聚合的平均延迟趋势数据
    """
    days = query_int(request, "days", 30)
    feature = query_str(request, "feature").strip()

    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)

    query = (
        select(
            func.date(AIInvocationLog.created_at).label("date"),
            func.avg(AIInvocationLog.latency_ms).label("avg_latency"),
            func.avg(AIInvocationLog.total_tokens).label("avg_tokens"),
        )
        .filter(AIInvocationLog.user_id == user.id)
        .filter(AIInvocationLog.created_at >= since)
    )

    if feature:
        query = query.filter(AIInvocationLog.feature == feature)

    query = query.group_by(func.date(AIInvocationLog.created_at)).order_by(
        func.date(AIInvocationLog.created_at)
    )

    rows = db.session.execute(query).all()

    trend = []
    for row in rows:
        trend.append(
            {
                "date": str(row.date),
                "avg_latency_ms": round(float(row.avg_latency or 0), 1),
                "avg_tokens": round(float(row.avg_tokens or 0), 1),
            }
        )

    return _success(data=trend)


# ==================== Token 消耗 ====================


@router.get("/api/v1/ai/stats/token-consumption")
@release_session
def get_token_consumption(request: Request, user: User = Depends(_current_user)):
    """
    获取 token 消耗统计（IDOR 修复：仅聚合当前用户自己的调用日志）

    查询参数:
        days: 天数（默认 30）

    返回:
        按天聚合的 token 消耗数据
    """
    days = query_int(request, "days", 30)

    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)

    rows = (
        select(
            func.date(AIInvocationLog.created_at).label("date"),
            func.coalesce(func.sum(AIInvocationLog.prompt_tokens), 0).label("prompt_tokens"),
            func.coalesce(func.sum(AIInvocationLog.completion_tokens), 0).label("completion_tokens"),
            func.coalesce(func.sum(AIInvocationLog.total_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(AIInvocationLog.cost_estimate), 0.0).label("cost"),
        )
        .filter(AIInvocationLog.user_id == user.id)
        .filter(AIInvocationLog.created_at >= since)
        .group_by(func.date(AIInvocationLog.created_at))
        .order_by(func.date(AIInvocationLog.created_at))
    )
    rows = db.session.execute(rows).all()

    data = []
    for row in rows:
        data.append(
            {
                "date": str(row.date),
                "prompt_tokens": int(row.prompt_tokens),
                "completion_tokens": int(row.completion_tokens),
                "total_tokens": int(row.total_tokens),
                "cost": round(float(row.cost), 4),
            }
        )

    return _success(data=data)


# ==================== Prompt 版本效果对比 ====================


@router.get("/api/v1/ai/stats/prompt-versions-comparison")
@release_session
def get_prompt_versions_comparison(request: Request, user: User = Depends(_current_user)):
    """
    获取 Prompt 版本效果对比（IDOR 修复：仅返回 当前用户创建 + 平台全局预置（created_by IS NULL）的版本）

    查询参数:
        feature: 可选，按功能模块过滤

    返回:
        各 Prompt 版本的统计数据对比
    """
    feature = query_str(request, "feature").strip()

    query = select(PromptVersion).filter(
        or_(PromptVersion.created_by == user.id, PromptVersion.created_by.is_(None))
    )
    if feature:
        query = query.filter_by(feature=feature)

    versions = db.session.scalars(query.order_by(
        PromptVersion.feature, PromptVersion.version.desc()
    )).all()

    data = []
    for pv in versions:
        success_rate = 0.0
        if pv.total_invocations > 0:
            success_rate = round(pv.success_count / pv.total_invocations * 100, 2)

        data.append(
            {
                "id": pv.id,
                "feature": pv.feature,
                "name": pv.name,
                "version": pv.version,
                "is_active": pv.is_active,
                "total_invocations": pv.total_invocations,
                "success_count": pv.success_count,
                "failure_count": pv.failure_count,
                "success_rate": success_rate,
                "avg_latency_ms": round(pv.avg_latency_ms, 1) if pv.avg_latency_ms else 0,
                "avg_tokens": round(pv.avg_tokens, 1) if pv.avg_tokens else 0,
                "avg_cost": round(pv.avg_cost, 4) if pv.avg_cost else 0,
            }
        )

    return _success(data=data)
