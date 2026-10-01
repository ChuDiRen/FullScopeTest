"""
GitHub 集成模块 - FastAPI 平迁（自 app/api/github_integration.py，蓝图挂载前缀 /api/v1）

路径/方法/状态码/响应字段与原 v1 完全一致（/api/v1/integrations/github/...），
前端与 CI 脚本零改动。全部端点为同步 def，运行于 RequestContextMiddleware
push 的 app context 内，直接复用 services 层（github_oauth_service）。

鉴权映射：
- @jwt_required() → Depends(_current_user)（内部复用 deps.get_current_user）
- 公开端点（v1 无 @jwt_required 设计，保持公开并注明）：
  * GET /api/v1/integrations/github/callback — GitHub OAuth 回调（GitHub 服务器
    重定向而来，无法携带本地 JWT，v1 即公开）
  * GET /api/v1/integrations/github/config   — OAuth 配置查询（前端登录页需要，v1 即公开）

会话状态迁移：
- v1 用 Flask session 在 /auth 与 /callback 间传递 oauth state（CSRF 防护）；
  ASGI 层没有 Flask request session，平迁改为进程内 state 存储
  （_oauth_state_store：state → (user_id, 时间戳)，10 分钟 TTL，一次性消费），
  CSRF 语义保持一致：state 不匹配/过期 → 302 redirect github_error=invalid_state

回调重定向：
- Flask redirect(...)（302）→ fastapi RedirectResponse(status_code=302)，
  Location 目标（FRONTEND_URL + /settings?github_*）与 v1 完全一致
"""

import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ..deps import get_current_user, release_session
from ....core.logging import get_logger
from ....models.user import User
from ....services.github_oauth_service import (
    create_or_update_integration,
    exchange_code_for_token,
    generate_authorize_url,
    get_github_oauth_config,
    get_github_user_info,
    get_integration_by_user,
    revoke_integration,
)

# Flask redirect() 默认 302，与 Starlette RedirectResponse 的默认 307 区分
_REDIRECT_302 = 302

logger = get_logger(__name__)

router = APIRouter(tags=["github-integration"])

# OAuth state 进程内存储（替代 v1 的 Flask session）：state -> (user_id, created_ts)
_OAUTH_STATE_TTL_SECONDS = 600
_oauth_state_store: Dict[str, Tuple[int, float]] = {}
_oauth_state_lock = threading.Lock()


def _oauth_state_put(state: str, user_id: int) -> None:
    now = time.time()
    with _oauth_state_lock:
        # 顺带清理过期 state
        expired = [k for k, (_, ts) in _oauth_state_store.items() if now - ts > _OAUTH_STATE_TTL_SECONDS]
        for k in expired:
            _oauth_state_store.pop(k, None)
        _oauth_state_store[state] = (user_id, now)


def _oauth_state_pop(state: str) -> Optional[int]:
    """取回并消费 state（一次性），过期/不存在返回 None"""
    with _oauth_state_lock:
        item = _oauth_state_store.pop(state, None)
    if not item:
        return None
    user_id, ts = item
    if time.time() - ts > _OAUTH_STATE_TTL_SECONDS:
        return None
    return user_id


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


def _frontend_url() -> str:
    """等价 v1 get_config().get('FRONTEND_URL', 'http://localhost:3000')"""
    from ....core.runtime import get_config

    return get_config().get("FRONTEND_URL", "http://localhost:3000")


# ==================== OAuth 授权 ====================

@router.get("/api/v1/integrations/github/auth")
@release_session
def github_auth(request: Request, user: User = Depends(_current_user)):
    """
    获取 GitHub OAuth 授权 URL

    用户点击此接口后，会重定向到 GitHub 授权页面。
    授权完成后，GitHub 会回调 /api/v1/integrations/github/callback
    """
    try:
        user_id = user.id

        # 生成回调 URL（等价 v1 request.host_url）
        base_url = str(request.base_url).rstrip("/")
        redirect_uri = f"{base_url}/api/v1/integrations/github/callback"

        # 生成授权 URL
        authorize_url, state = generate_authorize_url(redirect_uri)

        # 存储 state 用于 CSRF 验证（替代 v1 的 Flask session）
        _oauth_state_put(state, user_id)

        logger.info("GitHub OAuth initiated", user_id=user_id)

        return _success(
            data={
                "authorize_url": authorize_url,
                "state": state,
            }
        )

    except Exception as exc:
        logger.error("GitHub OAuth init failed", error=str(exc))
        return _error(500, f"GitHub OAuth 初始化失败: {str(exc)}")


@router.get("/api/v1/integrations/github/callback")
@release_session
def github_callback(request: Request):
    """
    GitHub OAuth 回调接口（公开端点：v1 无鉴权设计，GitHub 服务器重定向而来，保持公开）

    GitHub 授权完成后会重定向到此接口，携带 code 和 state 参数。
    此接口交换 code 为 token，获取用户信息，创建或更新绑定记录。
    全程 302 重定向回前端，与 v1 的 redirect(...) 一致。
    """
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")

    # 检查是否有错误
    if error:
        error_description = request.query_params.get("error_description", "")
        logger.warning(
            "GitHub OAuth callback error", error=error, description=error_description
        )
        # 重定向到前端错误页面
        frontend_url = _frontend_url()
        return RedirectResponse(f"{frontend_url}/settings?github_error={error}", status_code=_REDIRECT_302)

    # 验证参数
    if not code or not state:
        logger.warning("GitHub OAuth callback missing parameters")
        frontend_url = _frontend_url()
        return RedirectResponse(f"{frontend_url}/settings?github_error=missing_params", status_code=_REDIRECT_302)

    # 验证 state（CSRF 防护，替代 v1 的 Flask session）
    user_id = _oauth_state_pop(state)

    if not user_id:
        logger.warning("GitHub OAuth state mismatch")
        frontend_url = _frontend_url()
        return RedirectResponse(f"{frontend_url}/settings?github_error=invalid_state", status_code=_REDIRECT_302)

    try:
        # 交换 code 为 token
        token_data = exchange_code_for_token(code)
        github_user_data = get_github_user_info(token_data["access_token"])

        # 创建或更新集成记录
        integration = create_or_update_integration(
            user_id=user_id,
            github_user_data=github_user_data,
            token_data=token_data,
        )

        logger.info(
            "GitHub OAuth completed successfully",
            user_id=user_id,
            github_username=integration.github_username,
        )

        # 重定向到前端成功页面
        frontend_url = _frontend_url()
        return RedirectResponse(f"{frontend_url}/settings?github_success=true", status_code=_REDIRECT_302)

    except Exception as exc:
        logger.error("GitHub OAuth callback failed", error=str(exc), user_id=user_id)
        frontend_url = _frontend_url()
        return RedirectResponse(f"{frontend_url}/settings?github_error=callback_failed", status_code=_REDIRECT_302)


# ==================== 绑定管理 ====================

@router.get("/api/v1/integrations/github/status")
@release_session
def github_status(user: User = Depends(_current_user)):
    """获取当前用户的 GitHub 绑定状态"""
    try:
        user_id = user.id

        integration = get_integration_by_user(user_id)

        if not integration:
            return _success(
                data={
                    "connected": False,
                    "integration": None,
                }
            )

        return _success(
            data={
                "connected": True,
                "integration": integration.to_dict(),
            }
        )

    except Exception as exc:
        logger.error("Get GitHub status failed", error=str(exc))
        return _error(500, f"获取 GitHub 状态失败: {str(exc)}")


@router.post("/api/v1/integrations/github/unbind")
@release_session
def github_unbind(user: User = Depends(_current_user)):
    """解绑 GitHub 账号"""
    try:
        user_id = user.id

        integration = get_integration_by_user(user_id)

        if not integration:
            return _error(404, "未找到 GitHub 绑定信息")

        success = revoke_integration(integration.id, user_id)
        if not success:
            return _error(500, "解绑失败")

        logger.info("GitHub unbind successful", user_id=user_id)
        return _success(message="GitHub 账号已解绑")

    except Exception as exc:
        logger.error("GitHub unbind failed", error=str(exc))
        return _error(500, f"解绑失败: {str(exc)}")


@router.get("/api/v1/integrations/github/config")
@release_session
def github_config():
    """获取 GitHub OAuth 配置（公开接口，v1 无需认证，保持公开）"""
    try:
        config = get_github_oauth_config()

        return _success(data=config)

    except Exception as exc:
        logger.error("Get GitHub config failed", error=str(exc))
        return _error(500, f"获取配置失败: {str(exc)}")
