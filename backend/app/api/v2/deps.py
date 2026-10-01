"""
FastAPI 依赖注入核心（v1 平迁路由与 v2 路由共用）

职责：
- JWT 鉴权依赖：token 同时接受 headers 与 cookies
  （token 位置 headers + cookies，Cookie 名 access_token_cookie / refresh_token_cookie）
- token 黑名单 + token_version 校验（校验异常时 fail-closed，修复 v1 的 fail-open 问题）
- 分页参数、JSON body 读取、客户端 IP（默认不信任 X-Forwarded-For，修复 v1 盲信 XFF 的问题）
- 认证 Cookie 读写助手（登录/登出响应用）

注意：模型/服务层的 DB 会话由 DbSessionMiddleware 放置的 ContextVar 作用域令牌保证
（纯 SQLAlchemy 实现，见 app/database.py），每个请求独立会话，响应后统一释放。
"""

from __future__ import annotations

import functools
import os
from typing import Any, Dict, Optional, Tuple

from fastapi import Depends, Request
from ...core.jwt import decode_token

from ...core.logging import get_logger
from ...models.user import User
from ...extensions import db

logger = get_logger(__name__)

ACCESS_COOKIE_NAME = "access_token_cookie"
REFRESH_COOKIE_NAME = "refresh_token_cookie"


# ---------------------------------------------------------------------------
# Token 提取与校验
# ---------------------------------------------------------------------------

def _extract_token(request: Request, cookie_name: str = ACCESS_COOKIE_NAME) -> Optional[str]:
    """从 Authorization: Bearer 或 Cookie 中提取 token（与 v1 的 JWT_TOKEN_LOCATION 对齐）"""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
        if token:
            return token
    return request.cookies.get(cookie_name) or None


def _decode_token_checked(token: str, expected_type: Optional[str] = None) -> Dict[str, Any]:
    """
    解码并校验 token。

    校验链（任一失败抛 HTTPException 401）：
    1. 签名与有效期（decode_token）
    2. token 类型（access/refresh 不能混用，修复 v2 refresh 不校验类型的问题）
    3. jti 是否在黑名单（登出/改密后失效）
    4. token_version 是否仍有效（改密后旧 token 失效，校验异常 fail-closed）
    """
    from fastapi import HTTPException

    try:
        decoded = decode_token(token)
    except Exception:
        raise HTTPException(status_code=401, detail="无效的认证凭据")

    if expected_type and decoded.get("type") != expected_type:
        raise HTTPException(status_code=401, detail="无效的认证凭据")

    jti = decoded.get("jti")
    if jti:
        try:
            from ...services.token_blacklist import is_token_blacklisted
            if is_token_blacklisted(jti):
                raise HTTPException(status_code=401, detail="认证凭据已注销")
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("token blacklist check failed", error=str(exc))

    sub = decoded.get("sub")
    token_version = decoded.get("token_version", 0)
    try:
        from ...services.token_blacklist import is_token_version_valid
        if not is_token_version_valid(int(sub), token_version):
            raise HTTPException(status_code=401, detail="认证凭据已失效，请重新登录")
    except HTTPException:
        raise
    except Exception as exc:
        # fail-closed：校验异常时拒绝（修复 v1 token_verification_loader 的 fail-open）
        logger.warning("token_version check failed, rejecting token", error=str(exc))
        raise HTTPException(status_code=401, detail="认证凭据校验失败")

    return decoded


def get_current_user(request: Request) -> User:
    """必需鉴权依赖：Cookie 或 Bearer 中的 access token"""
    from fastapi import HTTPException

    token = _extract_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="未授权访问")
    decoded = _decode_token_checked(token, expected_type="access")
    return _load_active_user(decoded.get("sub"))


def _load_active_user(user_id: Any) -> User:
    from fastapi import HTTPException
    from ...extensions import db
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="无效的认证凭据")

    user = db.session.get(User, uid)
    if not user:
        raise HTTPException(status_code=401, detail="用户不存在")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="账号已被禁用")
    return user


def get_current_user_optional(request: Request) -> Optional[User]:
    """可选鉴权依赖：无 token / token 无效时返回 None"""
    try:
        return get_current_user(request)
    except Exception:
        return None


def get_admin_user(user: User = Depends(get_current_user)) -> User:
    """管理员鉴权依赖"""
    from fastapi import HTTPException
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


# ---------------------------------------------------------------------------
# 请求辅助
# ---------------------------------------------------------------------------

async def json_body(request: Request) -> Dict[str, Any]:
    """
    读取 JSON body（解析失败或非 JSON 时返回 {}）
    非 JSON body / 空 body 返回 {}。
    """
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def query_int(request: Request, name: str, default: int) -> int:
    """读取整数 query 参数（非法值返回 default）"""
    raw = request.query_params.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def query_str(request: Request, name: str, default: str = "") -> str:
    return request.query_params.get(name, default) or default


def client_ip(request: Request) -> str:
    """
    获取客户端真实 IP。

    默认只信任连接对端地址；仅当显式设置 TRUST_PROXY_HEADERS=true
    （部署在已知反向代理之后）时才解析 X-Forwarded-For 的最左非受信项，
    防止伪造 XFF 绕过真实来源判定。
    """
    direct = request.client.host if request.client else "127.0.0.1"
    if os.environ.get("TRUST_PROXY_HEADERS", "").strip().lower() != "true":
        return direct
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        # 最左侧第一个非空项即最初客户端（代理链可信前提下）
        first = xff.split(",")[0].strip()
        if first:
            return first
    real = request.headers.get("X-Real-IP", "").strip()
    return real or direct


def user_agent(request: Request) -> str:
    return request.headers.get("User-Agent", "") or ""


def pagination_params(
    request: Request,
    default_page: int = 1,
    default_page_size: int = 20,
    max_page_size: int = 100,
) -> Tuple[int, int]:
    """分页参数，等价 v1 的 page/page_size/per_page 约定"""
    page = max(1, query_int(request, "page", default_page))
    size = query_int(request, "page_size", query_int(request, "per_page", default_page_size))
    return page, max(1, min(size, max_page_size))


# ---------------------------------------------------------------------------
# 认证 Cookie 助手（响应层）
# ---------------------------------------------------------------------------

def _cookie_params() -> Dict[str, Any]:
    """从运行时配置读取 Cookie 参数，保证与 v1 行为一致"""
    from ...core.runtime import get_config

    cfg = get_config()
    return {
        "secure": bool(cfg.get("JWT_COOKIE_SECURE", False)),
        "httponly": bool(cfg.get("JWT_COOKIE_HTTP_ONLY", True)),
        "samesite": cfg.get("JWT_COOKIE_SAMESITE", "Lax"),
        "path": cfg.get("JWT_ACCESS_COOKIE_PATH", "/"),
    }


def set_auth_cookies(response, access_token: str, refresh_token: Optional[str] = None) -> None:
    """登录/刷新成功后写认证 Cookie（httpOnly）"""
    from ...core.runtime import get_config

    params = _cookie_params()
    access_max_age = int(get_config().get("JWT_ACCESS_TOKEN_EXPIRES").total_seconds())
    response.set_cookie(
        ACCESS_COOKIE_NAME, access_token,
        max_age=access_max_age, **params,
    )
    if refresh_token:
        refresh_path = get_config().get("JWT_REFRESH_COOKIE_PATH", "/")
        refresh_max_age = int(get_config().get("JWT_REFRESH_TOKEN_EXPIRES").total_seconds())
        response.set_cookie(
            REFRESH_COOKIE_NAME, refresh_token,
            max_age=refresh_max_age,
            secure=params["secure"], httponly=params["httponly"],
            samesite=params["samesite"], path=refresh_path,
        )


def clear_auth_cookies(response) -> None:
    """登出时清除认证 Cookie"""
    response.delete_cookie(ACCESS_COOKIE_NAME, path="/")
    response.delete_cookie(REFRESH_COOKIE_NAME, path="/")


# ---------------------------------------------------------------------------
# 同步端点会话释放
# ---------------------------------------------------------------------------

def release_session(fn):
    """
    同步端点装饰器：视图返回后释放本 worker 线程的 scoped session。

    背景：同步端点在 anyio 线程池执行，若线程内创建了 SQLAlchemy 会话，
    事件循环线程的 app context teardown 可能回收不到，导致 NullPool 连接
    泄漏（Windows 上表现为测试库文件被锁，WinError 32）。

    语义与 v1 的 teardown_appcontext 一致：异常时 rollback，正常返回时
    commit 未提交变更，最后 remove。
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        from ...extensions import db
        try:
            result = fn(*args, **kwargs)
        except Exception:
            for op in (db.session.rollback, db.session.remove):
                try:
                    op()
                except Exception:  # pragma: no cover
                    pass
            raise
        try:
            db.session.commit()
        except Exception:
            try:
                db.session.rollback()
            except Exception:  # pragma: no cover
                pass
        try:
            db.session.remove()
        except Exception:  # pragma: no cover
            pass
        return result

    return wrapper
