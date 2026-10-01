"""
AI 全局搜索模块 - FastAPI 平迁（自 app/api/global_search.py，蓝图挂载前缀 /api/v1）

路径/方法/状态码/响应字段与原 v1 完全一致（/api/v1/ai/global-search）。

鉴权映射：
- @jwt_required() → Depends(_current_user)（内部复用 deps.get_current_user）
- 公开端点：无（global_search 在 v1 要求 JWT）

会话生命周期：
- 同步端点统一加 @release_session（deps.release_session），防 NullPool 连接泄漏
- 鉴权依赖包装为 async _current_user：在事件循环线程执行 get_current_user

IDOR 防护：搜索结果按当前 user_id 在 execute_global_search 内部过滤（与 v1 一致），
仅返回当前用户可见资产。
"""

from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..deps import get_current_user, json_body, release_session
from ....core.logging import get_logger
from ....models.user import User
from ....utils.ai_search import execute_global_search

logger = get_logger(__name__)

router = APIRouter(tags=["global-search"])


# ---------------------------------------------------------------------------
# 鉴权包装 / 响应构造
# ---------------------------------------------------------------------------

async def _current_user(request: Request) -> User:
    """鉴权依赖：在事件循环线程内执行同步 get_current_user，session 随 app context 回收"""
    return get_current_user(request)


def _request_id() -> str:
    """读取 RequestContextMiddleware 写入 scope state 的 request_id"""
    try:
        from ....core.runtime import ctx

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
            "timestamp": datetime_now_iso(),
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
            "timestamp": datetime_now_iso(),
        },
    )


def datetime_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat() + "Z"


# ==================== AI 全局搜索 ====================

@router.post("/api/v1/ai/global-search")
@release_session
def global_search(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """AI 全局搜索资产（结果按当前用户可见域过滤）"""
    data = data or {}
    query = (data.get("query") or "").strip()

    if not query:
        return _error(400, "query is required")

    try:
        # 复用已平迁的 api_test 模块中的 AI runtime config 构造（等价 v1 的
        # from .api_test import _build_ai_runtime_config）
        from .api_test import _build_ai_runtime_config

        user_id = user.id
        runtime_config = _build_ai_runtime_config(data)

        results = execute_global_search(query, user_id, runtime_config)
        return _success(data={"results": results})
    except Exception as exc:
        return _error(500, f"全局搜索失败: {str(exc)}")
