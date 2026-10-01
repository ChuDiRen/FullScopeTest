"""
FastAPI 认证模块（v2 表面）

安全修复（相对旧 v2 实现）：
- 登录/注册加滑动窗口限流 + 账户锁定（与 v1 强度一致，旧实现完全绕过）
- refresh 强制校验 token 类型（旧实现 access token 可无限续期）
- get_current_user 委托 deps 实现：黑名单 + token_version fail-closed 校验
- 登录成功写 httpOnly Cookie（与 v1 前端行为兼容）
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ...extensions import db
from ...models.user import User
from ...core.logging import get_logger
from .deps import (
    get_current_user as _deps_get_current_user,
    client_ip,
    set_auth_cookies,
    clear_auth_cookies,
)
from ...services.password_policy import (
    is_account_locked,
    record_login_failure,
    reset_login_failures,
)
from ...services.rate_limit_service import sliding_window_rate_limit
from sqlalchemy import select

logger = get_logger(__name__)

router = APIRouter(tags=["auth-v2"])

# 兼容既有 v2 模块的导入：from .auth import get_current_user
get_current_user = _deps_get_current_user


class LoginRequest(BaseModel):
    """登录请求"""
    username: str = Field(..., min_length=1, max_length=100, description='用户名或邮箱')
    password: str = Field(..., min_length=1, max_length=128, description='密码')


class RegisterRequest(BaseModel):
    """注册请求"""
    username: str = Field(..., min_length=3, max_length=50, description='用户名')
    email: str = Field(..., description='邮箱地址')
    password: str = Field(..., min_length=8, max_length=128, description='密码')


def _login_rate_key(request: Request) -> str:
    return f"rate_limit:login_ip:{client_ip(request)}"


@router.post("/login")
async def login_v2(request_data: LoginRequest, request: Request, response: Response):
    """用户登录 - v2 API（限流 5/min + 账户锁定）"""
    from ...core.passwords import check_password_hash
    from ...core.jwt import create_access_token, create_refresh_token

    if not sliding_window_rate_limit(_login_rate_key(request), 5):
        raise HTTPException(status_code=429, detail="尝试次数过多，请稍后再试")

    user = db.session.scalar(select(User).filter(
        (User.username == request_data.username) | (User.email == request_data.username.lower())
    ))

    if user and is_account_locked(user.id)[0]:
        raise HTTPException(status_code=423, detail="账号已锁定，请稍后再试")

    if not user or not check_password_hash(user.password_hash, request_data.password):
        if user:
            record_login_failure(user.id, ip_address=client_ip(request))
        logger.warning("v2 login failed", username=request_data.username[:50])
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="账号已被禁用")

    reset_login_failures(user.id)
    user.update_last_login()
    db.session.commit()

    access_token = create_access_token(identity=str(user.id))
    refresh_token = create_refresh_token(identity=str(user.id))
    set_auth_cookies(response, access_token, refresh_token)

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "user_id": user.id,
        "username": user.username,
    }


@router.post("/register", status_code=200)
async def register_v2(request_data: RegisterRequest, request: Request):
    """用户注册 - v2 API（限流 5/min）"""
    from ...core.passwords import generate_password_hash

    if not sliding_window_rate_limit(_login_rate_key(request), 5):
        raise HTTPException(status_code=429, detail="尝试次数过多，请稍后再试")

    if db.session.scalar(select(User).filter_by(username=request_data.username)):
        raise HTTPException(status_code=400, detail='用户名已被使用')

    if db.session.scalar(select(User).filter_by(email=request_data.email)):
        raise HTTPException(status_code=400, detail='邮箱已被注册')

    user = User(
        username=request_data.username,
        email=request_data.email,
        password_hash=generate_password_hash(request_data.password),
    )

    db.session.add(user)
    db.session.commit()

    return {"message": "注册成功", "user_id": user.id, "username": user.username}


@router.get("/me")
async def get_current_user_v2(user: User = Depends(_deps_get_current_user)):
    """获取当前用户信息"""
    return user.to_dict()


@router.post("/refresh")
async def refresh_token_v2(request: Request, response: Response):
    """刷新 Access Token（强制 refresh token 类型 + 黑名单校验）"""
    from ...core.jwt import create_access_token
    from .deps import _decode_token_checked, _extract_token, REFRESH_COOKIE_NAME

    token = _extract_token(request, cookie_name=REFRESH_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="缺少 refresh token")

    decoded = _decode_token_checked(token, expected_type="refresh")
    access_token = create_access_token(identity=str(decoded["sub"]))
    set_auth_cookies(response, access_token)
    return {"access_token": access_token}


@router.post("/logout")
async def logout_v2(request: Request, response: Response, user: User = Depends(_deps_get_current_user)):
    """登出：当前 token 加入黑名单并清除 Cookie"""
    from ...core.jwt import decode_token
    from ...services.token_blacklist import blacklist_token
    from .deps import _extract_token

    token = _extract_token(request)
    if token:
        decoded = decode_token(token)
        if decoded.get("jti") and decoded.get("exp"):
            blacklist_token(decoded["jti"], datetime.fromtimestamp(decoded["exp"], tz=timezone.utc))
    clear_auth_cookies(response)
    return {"message": "已登出"}
