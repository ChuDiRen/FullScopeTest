"""
全局 AI Copilot 模块 - FastAPI 平迁（自 app/api/ai_copilot.py，蓝图挂载前缀 /api/v1）

路径/方法/状态码/响应字段与原 v1 完全一致（/api/v1/copilot/chat）。
同步端点，运行于 RequestContextMiddleware push 的 app context 内。

v1 全量路由清单（grep "@.*_bp.route" app/api/ai_copilot.py）：
- POST /api/v1/copilot/chat

鉴权映射：
- @jwt_required() → Depends(_current_user)（内部复用 deps.get_current_user）
- 本模块无公开端点（v1 带 @jwt_required）

AI 调用：
- 沿用 app/utils/ai_copilot.process_copilot_chat（services/ai 层），零改动复用；
  测试通过 monkeypatch 路由模块属性 mock 掉 AI 客户端（零真实外呼、零 API key 依赖）
- 错误信息脱敏与 v1 一致：APP_ENV=production 返回通用文案，否则透传异常信息

IDOR 说明：
- 聊天接口无资源 id 参数；user_id 仅用于工具调用（如 create_performance_test）
  创建"当前用户自有"的 PerfTestScenario（utils.ai_copilot.execute_tool_call 内
  user_id=user_id），无越权面。

会话生命周期：
- 同步端点统一加 @release_session（deps.release_session）
- 鉴权依赖包装为 async _current_user：在事件循环线程执行 get_current_user
"""

import os
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..deps import get_current_user, json_body, release_session
from ....core.logging import get_logger
from ....models.user import User
from ....utils.ai_copilot import process_copilot_chat

logger = get_logger(__name__)

router = APIRouter(tags=["ai-copilot"])


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


def _safe_error_msg(exc: Exception) -> str:
    """生产环境返回通用错误信息，不暴露内部异常详情（与 v1 一致）"""
    if os.environ.get("APP_ENV") == "production":
        return "Copilot 服务异常"
    return str(exc)


# ==================== Copilot 聊天 ====================


@router.post("/api/v1/copilot/chat")
@release_session
def copilot_chat(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """全局 AI Copilot 聊天接口"""
    data = data or {}
    messages = data.get("messages", [])

    if not messages:
        return _error(400, "messages is required")

    try:
        user_id = user.id

        from ....core.runtime import get_config

        runtime_config = {
            "AI_ASSISTANT_ENABLED": get_config().get("AI_ASSISTANT_ENABLED", True),
            "AI_ASSISTANT_BASE_URL": get_config().get("AI_ASSISTANT_BASE_URL", ""),
            "AI_ASSISTANT_API_KEY": get_config().get("AI_ASSISTANT_API_KEY", ""),
            "AI_ASSISTANT_MODEL": get_config().get("AI_ASSISTANT_MODEL", ""),
            "AI_VISION_BASE_URL": get_config().get("AI_VISION_BASE_URL", ""),
            "AI_VISION_API_KEY": get_config().get("AI_VISION_API_KEY", ""),
            "AI_VISION_MODEL": get_config().get("AI_VISION_MODEL", ""),
        }

        # 允许前端覆盖配置
        if data.get("base_url"):
            runtime_config["AI_ASSISTANT_BASE_URL"] = str(data.get("base_url")).strip()
        if data.get("model"):
            runtime_config["AI_ASSISTANT_MODEL"] = str(data.get("model")).strip()
        if data.get("api_key"):
            runtime_config["AI_ASSISTANT_API_KEY"] = str(data.get("api_key")).strip()
        if data.get("vision_base_url"):
            runtime_config["AI_VISION_BASE_URL"] = str(data.get("vision_base_url")).strip()
        if data.get("vision_model"):
            runtime_config["AI_VISION_MODEL"] = str(data.get("vision_model")).strip()
        if data.get("vision_api_key"):
            runtime_config["AI_VISION_API_KEY"] = str(data.get("vision_api_key")).strip()

        reply = process_copilot_chat(messages, user_id, runtime_config)
        return _success(data=reply)
    except Exception as exc:
        logger.error("copilot chat failed", error=str(exc))
        return _error(500, _safe_error_msg(exc))
