"""
AI Prompt 版本管理模块 - FastAPI 平迁（自 app/api/ai_prompt_versions.py，蓝图挂载前缀 /api/v1）

⚠ v1 注册状态说明（重要）：
Flask 源文件 app/api/ai_prompt_versions.py 在 v1 从未被注册为路由：
app/api/__init__.py 未 import 该模块；且它与 app/api/prompt_versions.py 定义了
同名 view 函数（list_prompt_versions / create_prompt_version /
get_prompt_version / update_prompt_version / deactivate_prompt_version），
一旦 import 会直接触发 Flask duplicate endpoint AssertionError。

v1 源文件全量路由清单（grep "@.*_bp.route" app/api/ai_prompt_versions.py）及平迁归口：
- GET    /ai/prompt-versions                → 与 prompt_versions.py 重复，归口 prompt_versions.py 路由（本模块不重复注册，避免遮蔽活路由改变 v1 行为：POST 201→200、feature 白名单校验丢失）
- POST   /ai/prompt-versions                → 同上，归口 prompt_versions.py 路由
- GET    /ai/prompt-versions/{version_id}   → 同上，归口 prompt_versions.py 路由
- PUT    /ai/prompt-versions/{version_id}   → 同上，归口 prompt_versions.py 路由
- DELETE /ai/prompt-versions/{version_id}   → 同上，归口 prompt_versions.py 路由
- GET    /ai/prompt-versions/{version_id}/stats → 本模块独有路由，在此迁移注册

本模块注册的端点为同步 def，运行于 RequestContextMiddleware push 的 app context 内，
直接复用 数据库会话（app/database.py ContextVar 作用域）。

鉴权映射：
- @jwt_required() → Depends(_current_user)（内部复用 deps.get_current_user）
- 本模块无公开端点（源文件全部端点均带 @jwt_required）

IDOR 修复（属主字段：PromptVersion.created_by）：
- GET /{version_id}/stats 按可见域（当前用户创建 + 平台全局预置 created_by IS NULL）
  过滤，越权/不存在一律 404（v1 该路由未注册，无对应行为，平迁直接按属主过滤实现）
- 版本内的调用趋势日志按 prompt_version_id 关联，访问权随版本属主走

会话生命周期：
- 同步端点统一加 @release_session（deps.release_session）
- 鉴权依赖包装为 async _current_user：在事件循环线程执行 get_current_user
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import or_

from app.api.v2.deps import get_current_user, release_session
from app.core.logging import get_logger
from app.models.ai_invocation_log import AIInvocationLog
from app.models.prompt_version import PromptVersion
from app.models.user import User
from sqlalchemy import select
from app.extensions import db

logger = get_logger(__name__)

router = APIRouter(tags=["ai-prompt-versions"])


# ---------------------------------------------------------------------------
# 鉴权包装 / 响应构造 / 属主过滤
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


def _get_visible_version(user_id: int, version_id: int) -> Optional[PromptVersion]:
    """
    按 id 取可见版本（own + 平台全局预置 created_by IS NULL）：
    不存在/越权一律 None → 404（与 prompt_versions.py 的可见域口径一致）
    """
    return db.session.scalar(select(PromptVersion).filter(
        or_(PromptVersion.created_by == user_id, PromptVersion.created_by.is_(None))
    ).filter_by(id=version_id))


# ==================== 版本详细统计 ====================


@router.get("/api/v1/ai/prompt-versions/{version_id}/stats")
@release_session
def get_prompt_version_stats(version_id: int, user: User = Depends(_current_user)):
    """
    获取 Prompt 版本的详细统计（IDOR 修复：仅可见 own + 全局预置，越权 404）

    包含：
    - 基本信息和聚合统计
    - 最近 N 次调用的成功率趋势
    - 平均延迟和 token 消耗
    """
    pv = _get_visible_version(user.id, version_id)
    if not pv:
        return _error(404, "Prompt 版本不存在")

    # 获取最近 30 条调用日志用于趋势分析
    recent_logs = (
        db.session.scalars(select(AIInvocationLog).filter_by(prompt_version_id=version_id).order_by(AIInvocationLog.created_at.desc()).limit(30)).all())

    trend = []
    for log in reversed(recent_logs):
        trend.append(
            {
                "created_at": log.created_at.isoformat() if log.created_at else None,
                "success": log.success,
                "latency_ms": log.latency_ms,
                "total_tokens": log.total_tokens,
                "cost_estimate": log.cost_estimate,
            }
        )

    stats = pv.to_dict()
    stats["recent_trend"] = trend

    return _success(data=stats)
