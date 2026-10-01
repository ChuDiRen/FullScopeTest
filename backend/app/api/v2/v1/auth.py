"""
认证接口（自 Flask 蓝图 app/api/auth.py 平迁，路径/方法/状态码与 v1 完全一致）

覆盖原蓝图全部 15 个端点：
- register / login / me(GET,PUT) / me/avatar / refresh / logout / password
- forgot-password / reset-password
- SSO: providers / oidc/login / oidc/callback / ldap/login / config(管理员)

与 v1 的对应关系：
- @jwt_required()                → Depends(deps.get_current_user)（黑名单 + token_version fail-closed）
- @jwt_required(refresh=True)    → deps._extract_token + deps._decode_token_checked(expected_type="refresh")
- @limiter.limit("5/minute")     → sliding_window_rate_limit("rate_limit:<purpose>_ip:<ip>", n)
- 账户锁定                       → services/password_policy（连续失败锁定，423）
- success_response/error_response → 本模块 _success/_error（信封字段与 v1 完全一致）
- set_access/refresh_cookies     → deps.set_auth_cookies；unset_jwt_cookies → deps.clear_auth_cookies

限流/安全头由全局中间件（app/api/v2/middleware.py）负责，本模块不重复实现。
"""

import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from ....core.passwords import check_password_hash, generate_password_hash

from ..deps import (
    REFRESH_COOKIE_NAME,
    _decode_token_checked,
    _extract_token,
    clear_auth_cookies,
    client_ip,
    get_current_user,
    query_str,
    set_auth_cookies,
)
from ....core.logging import get_logger
from ....extensions import db
from ....models.organization import Organization, OrganizationMember
from ....models.user import User
from ....services import password_policy as _password_policy
from ....services.password_policy import (
    get_login_failures,
    is_account_locked,
    record_login_failure,
    reset_login_failures,
)
from ....services.rate_limit_service import sliding_window_rate_limit
from ....utils.validators import is_valid_email, validate_password_strength
from sqlalchemy import select
from sqlalchemy import func

logger = get_logger(__name__)


def _ensure_user_has_organization(user_id: int) -> None:
    """确保用户至少有一个组织，没有则自动创建个人空间。

    等价原 app/middleware/tenant.py 的 ensure_user_has_organization
    （该模块随零 Flask 改造删除，逻辑原样迁入本模块）。
    """
    memberships = db.session.scalar(select(func.count()).select_from(select(OrganizationMember).filter_by(
        user_id=user_id, is_active=True
    ).subquery()))

    if memberships > 0:
        return

    user = db.session.get(User, user_id)
    if not user:
        return

    # 创建个人空间组织
    slug = f"personal-{user.username}-{user.id}"
    org = Organization(
        name=f"{user.username} 的个人空间",
        slug=slug,
        description="系统自动创建的个人空间",
        owner_id=user.id,
        is_active=True,
    )
    db.session.add(org)
    db.session.flush()

    # 添加为所有者
    member = OrganizationMember(
        organization_id=org.id,
        user_id=user.id,
        role='owner',
        invited_by=user.id,
        is_active=True,
    )
    db.session.add(member)
    db.session.commit()

    logger.info(f'Auto-created personal org for user {user.username}', org_id=org.id)


router = APIRouter(prefix="/api/v1/auth", tags=["v1-auth"])


# ---------------------------------------------------------------------------
# Redis 可用性探测（带时间预算，杜绝向不可用 Redis 反复建连造成的请求阻塞）
# ---------------------------------------------------------------------------

_REDIS_CLIENT = None
_REDIS_LAST_PROBE = 0.0
_REDIS_PROBE_INTERVAL = 30.0
_REDIS_PROBE_LOCK = threading.Lock()


def _ping_with_budget(client, seconds: float = 4.0) -> bool:
    """在守护线程里 ping，超时视为不可用（避免个别环境下 connect 长时间阻塞）"""
    result: dict = {}

    def _check():
        try:
            result["ok"] = bool(client.ping())
        except Exception:
            result["ok"] = False

    t = threading.Thread(target=_check, daemon=True)
    t.start()
    t.join(seconds)
    return bool(result.get("ok"))


def _usable_redis():
    """
    返回一个探测可用的 Redis 客户端（严格 2s socket 超时），不可用返回 None。

    探测失败后 30s 内不再重试；期间路由级限流按 rate_limit_service 的既定
    降级策略 fail-open（全局 RateLimitMiddleware 仍生效）。
    """
    global _REDIS_CLIENT, _REDIS_LAST_PROBE

    if _REDIS_CLIENT is not None:
        return _REDIS_CLIENT

    now = time.monotonic()
    if now - _REDIS_LAST_PROBE < _REDIS_PROBE_INTERVAL:
        return None

    with _REDIS_PROBE_LOCK:
        if _REDIS_CLIENT is not None:
            return _REDIS_CLIENT
        now = time.monotonic()
        if now - _REDIS_LAST_PROBE < _REDIS_PROBE_INTERVAL:
            return None
        _REDIS_LAST_PROBE = now
        try:
            import redis as redis_lib

            url = os.environ.get("REDIS_URL") or "redis://localhost:6379/0"
            client = redis_lib.from_url(
                url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2
            )
            if _ping_with_budget(client):
                _REDIS_CLIENT = client
                return _REDIS_CLIENT
        except Exception as exc:
            logger.warning("auth 路由限流 Redis 探测失败，fail-open", error=str(exc))
            return None

        logger.warning("auth 路由限流 Redis 不可用，fail-open（30s 后重探）")
        return None


# ---------------------------------------------------------------------------
# v1 响应信封（字段与 app/utils/response.py 完全一致）
# ---------------------------------------------------------------------------

def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat() + "Z"


def _request_id() -> str:
    try:
        from ....core.runtime import ctx

        return ctx.get_request_id() or ""
    except Exception:
        return ""


def _success(data=None, message: str = "success", code: int = 200) -> JSONResponse:
    """等价 v1 success_response(data, message, code)"""
    return JSONResponse(
        status_code=code,
        content={"code": code, "message": message, "data": data, "timestamp": _timestamp()},
    )


def _error(code: int, message: str, errors: Any = None) -> JSONResponse:
    """等价 v1 error_response(code, message, errors)"""
    return JSONResponse(
        status_code=code,
        content={
            "code": code,
            "message": message,
            "errors": errors,
            "request_id": _request_id(),
            "timestamp": _timestamp(),
        },
    )


def _limit_or_429(request: Request, purpose: str, limit: int) -> None:
    """
    等价 v1 的 @limiter.limit：Redis 滑动窗口，按用途 + 来源 IP 计数。

    Redis 不可用时 fail-open（与 rate_limit_service/全局中间件的降级策略一致）；
    尊重 RATELIMIT_ENABLED 总开关（testing 环境为 False 时与全局中间件一并禁用）。
    """
    from ....core.runtime import get_config
    if not get_config().get("RATELIMIT_ENABLED", True):
        return
    redis_client = _usable_redis()
    if redis_client is None:
        return
    key = f"rate_limit:{purpose}_ip:{client_ip(request)}"
    if not sliding_window_rate_limit(key, limit, redis_client=redis_client):
        raise HTTPException(status_code=429, detail="尝试次数过多，请稍后再试")


# ---------------------------------------------------------------------------
# 请求体模型（必需字段与 v1 @validate_json 一致）
# ---------------------------------------------------------------------------

class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=1)
    email: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)
    invite_code: str = ""


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class UpdateProfileRequest(BaseModel):
    username: Optional[str] = None
    email: Optional[str] = None
    avatar: Optional[str] = None


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=1)


class ForgotPasswordRequest(BaseModel):
    email: str = Field(..., min_length=1)


class ResetPasswordRequest(BaseModel):
    token: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=1)


class OIDCCallbackRequest(BaseModel):
    code: str = Field(..., min_length=1)
    state: str = Field(..., min_length=1)
    redirect_uri: str = ""


class LDAPLoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class _UploadFileAdapter:
    """把 FastAPI UploadFile 适配为 v1 upload_to_oss 期望的 file-like（filename/seek/read）"""

    def __init__(self, upload: UploadFile):
        self.filename = upload.filename or ""
        self._fp = upload.file

    def seek(self, *args, **kwargs):
        return self._fp.seek(*args, **kwargs)

    def read(self, *args, **kwargs):
        return self._fp.read(*args, **kwargs)


# ---------------------------------------------------------------------------
# 注册 / 登录
# ---------------------------------------------------------------------------

@router.post("/register")
def register(data: RegisterRequest, request: Request):
    """
    用户注册（限流 5/min）

    请求体:
        username: 用户名 (3-50字符)
        email: 邮箱地址
        password: 密码 (至少8位，包含大小写字母、数字、特殊字符)
        invite_code: 组织邀请码 (可选，留空则创建个人空间)
    """
    username = data.username.strip()
    email = data.email.strip().lower()
    password = data.password
    invite_code = (data.invite_code or "").strip()

    _limit_or_429(request, "register", 5)

    # 验证用户名长度
    if len(username) < 3 or len(username) > 50:
        return _error(400, '用户名长度应为 3-50 个字符')

    # 验证邮箱格式
    if not is_valid_email(email):
        return _error(400, '邮箱格式不正确')

    # 验证密码强度
    is_valid, error_msg = validate_password_strength(password)
    if not is_valid:
        return _error(400, error_msg)

    # 检查用户名是否已存在
    if db.session.scalar(select(User).filter_by(username=username)):
        return _error(400, '用户名已被使用')

    # 检查邮箱是否已存在
    if db.session.scalar(select(User).filter_by(email=email)):
        return _error(400, '邮箱已被注册')

    # 验证邀请码
    target_org = None
    if invite_code:
        from ....models.organization import Organization

        target_org = db.session.scalar(select(Organization).filter_by(invite_code=invite_code, is_active=True))
        if not target_org:
            return _error(400, '邀请码无效或组织已禁用')

    # 创建用户
    user = User(
        username=username,
        email=email,
        password_hash=generate_password_hash(password),
    )

    db.session.add(user)
    db.session.flush()  # 获取 user.id

    if target_org:
        # 加入邀请码对应的组织
        from ....models.organization import OrganizationMember

        member = OrganizationMember(
            organization_id=target_org.id,
            user_id=user.id,
            role='member',
            invited_by=target_org.owner_id,
            is_active=True,
        )
        db.session.add(member)
        db.session.commit()
        return _success(
            data={'user_id': user.id, 'username': user.username, 'role': user.role,
                  'organization': target_org.to_dict()},
            message=f'注册成功，已加入组织「{target_org.name}」',
            code=200,
        )

    # 无邀请码，创建个人空间
    _ensure_user_has_organization(user.id)
    db.session.commit()
    return _success(
        data={'user_id': user.id, 'username': user.username, 'role': user.role},
        message='注册成功',
        code=200,
    )


@router.post("/login")
def login(data: LoginRequest, request: Request):
    """
    用户登录（限流 5/min + 账户锁定）

    请求体:
        username: 用户名或邮箱
        password: 密码

    安全机制：
        - 同一 IP 每分钟最多 5 次登录尝试（超出返回 429）
        - 连续 5 次登录失败后锁定账户 15 分钟（返回 HTTP 423）
        - 成功登录后重置失败计数
    """
    _limit_or_429(request, "login", 5)

    username = data.username.strip()
    ip_address = client_ip(request)

    # 支持用户名或邮箱登录
    user = db.session.scalar(select(User).filter(
        (User.username == username) | (User.email == username.lower())
    ))

    # 用户不存在时也记录（但不锁定，因为没有 user_id）
    if not user:
        logger.warning("登录失败：用户不存在", username=username, ip=ip_address)
        return _error(401, '用户名或密码错误')

    # 检查账户锁定状态
    locked, remaining = is_account_locked(user.id)
    if locked:
        logger.warning("登录尝试：账户已锁定",
                       user_id=user.id, username=username,
                       remaining_seconds=remaining, ip=ip_address)
        return _error(
            423,
            f'账户已锁定，请在 {remaining // 60} 分 {remaining % 60} 秒后重试',
            errors={
                'locked': True,
                'remaining_seconds': remaining,
                'max_failures': _password_policy.MAX_LOGIN_FAILURES,
            },
        )

    # 验证密码
    if not check_password_hash(user.password_hash, data.password):
        record_login_failure(user.id, ip_address=ip_address, username=username)
        failures = get_login_failures(user.id)
        return _error(
            401,
            '用户名或密码错误',
            errors={'failures': failures, 'max_failures': _password_policy.MAX_LOGIN_FAILURES},
        )

    # 检查账户是否激活
    if not user.is_active:
        return _error(403, '账号已被禁用')

    # 登录成功：重置失败计数
    reset_login_failures(user.id)

    # 更新最后登录时间
    user.update_last_login()
    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.error("更新最后登录时间失败", user_id=user.id, error=str(exc))

    # 生成 Token（identity 需要是字符串）
    from ....core.jwt import create_access_token, create_refresh_token

    access_token = create_access_token(identity=str(user.id))
    refresh_token = create_refresh_token(identity=str(user.id))

    # Token 同时在 body 中返回（兼容 API 客户端和测试）和 httpOnly Cookie 中设置（前端安全使用）
    resp = _success(
        data={
            'access_token': access_token,
            'refresh_token': refresh_token,
            'user': user.to_dict(),
        },
        message='登录成功',
    )
    set_auth_cookies(resp, access_token, refresh_token)
    return resp


# ---------------------------------------------------------------------------
# 当前用户资料
# ---------------------------------------------------------------------------

@router.get("/me")
def get_me(user: User = Depends(get_current_user)):
    """获取当前登录用户信息"""
    return _success(data=user.to_dict())


@router.put("/me")
def update_profile(data: UpdateProfileRequest, user: User = Depends(get_current_user)):
    """修改个人信息（仅操作当前登录用户本人的字段）"""
    provided = data.model_fields_set

    if "username" in provided and data.username is not None:
        username = data.username.strip()
        if len(username) < 3 or len(username) > 50:
            return _error(400, '用户名长度应为 3-50 个字符')
        if db.session.scalar(select(User).filter(User.username == username, User.id != user.id)):
            return _error(400, '用户名已被使用')
        user.username = username

    if "email" in provided and data.email is not None:
        email = data.email.strip().lower()
        if not is_valid_email(email):
            return _error(400, '邮箱格式不正确')
        if db.session.scalar(select(User).filter(User.email == email, User.id != user.id)):
            return _error(400, '邮箱已被注册')
        user.email = email

    if "avatar" in provided and data.avatar is not None:
        user.avatar = data.avatar

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.error("更新用户资料失败", user_id=user.id, error=str(exc))
        return _error(500, '更新失败')

    return _success(data=user.to_dict(), message='个人信息修改成功')


@router.post("/me/avatar")
def upload_avatar(file: Optional[UploadFile] = File(None), user: User = Depends(get_current_user)):
    """上传个人头像到 OSS"""
    if file is None:
        return _error(400, '未找到文件')

    if not file.filename:
        return _error(400, '未选择文件')

    from ....utils.oss_upload import upload_to_oss

    success, result = upload_to_oss(_UploadFileAdapter(file), folder='avatars')
    if not success:
        return _error(500, result)

    user.avatar = result
    db.session.commit()

    return _success(data={'avatar': result}, message='头像上传成功')


# ---------------------------------------------------------------------------
# Token 刷新 / 登出
# ---------------------------------------------------------------------------

@router.post("/refresh")
def refresh_token(request: Request):
    """刷新 Access Token（强制 refresh token 类型，access token 调用返回 401）"""
    token = _extract_token(request, cookie_name=REFRESH_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="缺少 refresh token")

    # 类型校验：access token 不能用于刷新（修复 v1 混用问题）
    decoded = _decode_token_checked(token, expected_type="refresh")

    from ....core.jwt import create_access_token

    access_token = create_access_token(identity=str(decoded["sub"]))

    # 通过 httpOnly Cookie 设置新的 access_token，同时在 body 中返回（兼容性）
    resp = _success(data={'access_token': access_token}, message='Token 刷新成功')
    set_auth_cookies(resp, access_token)
    return resp


@router.post("/logout")
def logout(request: Request, user: User = Depends(get_current_user)):
    """
    登出 - 注销当前 Token

    将当前 Access Token 加入黑名单，使其立即失效。清除 httpOnly Cookie。
    """
    from ....services.token_blacklist import blacklist_token

    token = _extract_token(request)
    if token:
        decoded = _decode_token_checked(token, expected_type="access")
        jti = decoded.get("jti")
        exp_timestamp = decoded.get("exp")
        if jti and exp_timestamp:
            # naive UTC（与 v1 datetime.utcfromtimestamp 及 blacklist_token 的 ttl 计算一致）
            expires_at = datetime.fromtimestamp(exp_timestamp, tz=timezone.utc).replace(tzinfo=None)
            # Redis 不可用时跳过（blacklist_token 自身对 Redis 故障同样是降级放行）
            if _usable_redis() is not None:
                blacklist_token(jti, expires_at)

    logger.info('User logged out', user_id=user.id)

    # 清除 httpOnly Cookie
    resp = _success(message='已成功登出')
    clear_auth_cookies(resp)
    return resp


# ---------------------------------------------------------------------------
# 密码管理
# ---------------------------------------------------------------------------

@router.put("/password")
def change_password(data: ChangePasswordRequest, user: User = Depends(get_current_user)):
    """修改密码"""
    # 验证旧密码
    if not check_password_hash(user.password_hash, data.old_password):
        return _error(400, '原密码错误')

    # 验证新密码强度
    is_valid, error_msg = validate_password_strength(data.new_password)
    if not is_valid:
        return _error(400, error_msg)

    # 更新密码
    user.password_hash = generate_password_hash(data.new_password)
    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.error("密码修改失败", user_id=user.id, error=str(exc))
        return _error(500, '密码修改失败')

    return _success(message='密码修改成功')


@router.post("/forgot-password")
def forgot_password(data: ForgotPasswordRequest, request: Request):
    """
    忘记密码 - 发送重置链接（限流 3/min）

    流程:
        1. 用户提交邮箱 → 生成 token → 存储哈希到数据库
        2. 通过 EmailService 发送重置链接邮件
        3. 返回统一提示（不论用户是否存在，防止邮箱枚举攻击）
    """
    _limit_or_429(request, "forgot_password", 3)

    email = data.email.strip().lower()

    user = db.session.scalar(select(User).filter_by(email=email))

    # 不论用户是否存在，都返回相同消息（防止邮箱枚举攻击）
    if not user or not user.is_active:
        return _success(message='如果该邮箱已注册，重置链接已发送')

    # 生成重置 Token（有效期 1 小时）
    reset_token = secrets.token_urlsafe(32)
    user.reset_token = generate_password_hash(reset_token)
    user.reset_token_expires = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.error("密码重置 token 保存失败", user_id=user.id, error=str(exc))
        return _error(500, '密码重置请求失败')

    logger.info('Password reset requested', user_id=user.id, email=email)

    # 通过邮件服务发送重置链接
    from ....services.email_service import email_service

    email_service.send_password_reset_email(
        to=user.email,
        username=user.username,
        reset_token=reset_token,
    )

    return _success(message='如果该邮箱已注册，重置链接已发送')


@router.post("/reset-password")
def reset_password(data: ResetPasswordRequest, request: Request):
    """
    重置密码（限流 5/min）

    请求体:
        token: 重置 Token（从 forgot-password 接口获取）
        new_password: 新密码（至少8位，包含大小写字母、数字、特殊字符）
    """
    _limit_or_429(request, "reset_password", 5)

    # 验证新密码强度
    is_valid, error_msg = validate_password_strength(data.new_password)
    if not is_valid:
        return _error(400, error_msg)

    # 查找所有有待重置 token 的活跃用户（不能直接通过 token 查找，因为存的是 hash）
    users = db.session.scalars(select(User).filter(
        User.reset_token.isnot(None),
        User.reset_token_expires.isnot(None),
        User.is_active == True,  # noqa: E712
    )).all()

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    matched_user = None
    for user in users:
        if user.reset_token_expires < now:
            continue
        if check_password_hash(user.reset_token, data.token):
            matched_user = user
            break

    if not matched_user:
        return _error(400, '重置 Token 无效或已过期')

    # 更新密码，清除重置 token
    matched_user.password_hash = generate_password_hash(data.new_password)
    matched_user.reset_token = None
    matched_user.reset_token_expires = None
    db.session.commit()

    logger.info('Password reset completed', user_id=matched_user.id)

    return _success(message='密码重置成功，请使用新密码登录')


# ── SSO 单点登录 ─────────────────────────────────────────────────────────────

@router.get("/sso/providers")
def list_sso_providers():
    """获取可用的 SSO 提供商列表"""
    from ....services.sso_service import get_available_providers

    return _success(data=get_available_providers())


@router.get("/sso/oidc/login")
def oidc_login_url(request: Request):
    """
    获取 OIDC 登录 URL

    查询参数:
        redirect_uri: 回调 URL（默认使用前端域名 + /sso/callback）
    """
    from ....services.sso_service import oidc_provider

    if not oidc_provider.is_configured():
        return _error(400, 'OIDC 未配置')

    redirect_uri = query_str(request, "redirect_uri", "")
    if not redirect_uri:
        frontend_url = os.environ.get('FRONTEND_URL', 'http://localhost:5173')
        redirect_uri = f'{frontend_url}/sso/callback'

    # 生成 state 参数防 CSRF
    state = secrets.token_urlsafe(32)

    # 将 state 存入 Redis（TTL 5 分钟），回调时校验防止 CSRF
    redis_client = _usable_redis()
    if redis_client:
        try:
            redis_client.setex(f'oidc_state:{state}', 300, '1')
        except Exception as exc:
            logger.warning("OIDC state 存储失败（Redis 不可用）", error=str(exc))

    try:
        login_url = oidc_provider.get_login_url(redirect_uri, state)
    except Exception as exc:
        logger.error("OIDC 登录 URL 生成失败", error=str(exc))
        return _error(502, 'OIDC 服务不可用')

    return _success(data={'login_url': login_url, 'state': state})


@router.post("/sso/oidc/callback")
def oidc_callback(data: OIDCCallbackRequest):
    """
    处理 OIDC 回调

    请求体:
        code: 授权码
        state: CSRF 防护参数（必须与登录时生成的一致）
        redirect_uri: 回调 URL（与登录时一致）
    """
    from ....services.sso_service import find_or_create_sso_user, oidc_provider

    if not oidc_provider.is_configured():
        return _error(400, 'OIDC 未配置')

    state = data.state
    redirect_uri = data.redirect_uri

    # 校验 state 参数防止 CSRF 登录攻击
    redis_client = _usable_redis()
    if redis_client:
        try:
            stored = redis_client.get(f'oidc_state:{state}')
            if not stored:
                logger.warning("OIDC state 校验失败", state=state[:8] + '...')
                return _error(403, '无效的 state 参数，可能存在 CSRF 攻击')
            # 使用后立即删除，防止重放
            redis_client.delete(f'oidc_state:{state}')
        except Exception as exc:
            logger.warning("OIDC state 校验异常（Redis 不可用，跳过校验）", error=str(exc))

    if not redirect_uri:
        frontend_url = os.environ.get('FRONTEND_URL', 'http://localhost:5173')
        redirect_uri = f'{frontend_url}/sso/callback'

    sso_info = oidc_provider.handle_callback(data.code, redirect_uri)
    if not sso_info:
        return _error(401, 'OIDC 认证失败')

    user = find_or_create_sso_user(sso_info, 'oidc')

    # 确保 SSO 用户有组织上下文（首次登录自动创建个人空间）
    _ensure_user_has_organization(user.id)

    # 生成 Token 并返回（与普通登录一致）
    from ....core.jwt import create_access_token, create_refresh_token

    access_token = create_access_token(identity=str(user.id))
    refresh_token = create_refresh_token(identity=str(user.id))

    resp = _success(
        data={
            'access_token': access_token,
            'refresh_token': refresh_token,
            'user': user.to_dict(),
        },
        message='SSO 登录成功',
    )
    set_auth_cookies(resp, access_token, refresh_token)
    return resp


@router.post("/sso/ldap/login")
def ldap_login(data: LDAPLoginRequest, request: Request):
    """
    LDAP 登录（限流 10/min）

    请求体:
        username: 用户名
        password: 密码
    """
    _limit_or_429(request, "ldap_login", 10)

    from ....services.sso_service import find_or_create_sso_user, ldap_provider

    if not ldap_provider.is_configured():
        return _error(400, 'LDAP 未配置')

    sso_info = ldap_provider.authenticate(data.username.strip(), data.password)
    if not sso_info:
        return _error(401, 'LDAP 认证失败，用户名或密码错误')

    user = find_or_create_sso_user(sso_info, 'ldap')

    # 确保 SSO 用户有组织上下文（首次登录自动创建个人空间）
    _ensure_user_has_organization(user.id)

    from ....core.jwt import create_access_token, create_refresh_token

    access_token = create_access_token(identity=str(user.id))
    refresh_token = create_refresh_token(identity=str(user.id))

    resp = _success(
        data={
            'access_token': access_token,
            'refresh_token': refresh_token,
            'user': user.to_dict(),
        },
        message='LDAP 登录成功',
    )
    set_auth_cookies(resp, access_token, refresh_token)
    return resp


@router.get("/sso/config")
def get_sso_config(user: User = Depends(get_current_user)):
    """获取 SSO 配置信息（管理员可见）"""
    from ....services.sso_service import ldap_provider, oidc_provider

    if not user.is_admin():
        return _error(403, '需要管理员权限')

    return _success(data={
        'oidc': {
            'configured': oidc_provider.is_configured(),
            'issuer_url': os.environ.get('OIDC_ISSUER_URL', ''),
            'client_id': os.environ.get('OIDC_CLIENT_ID', ''),
            'scopes': os.environ.get('OIDC_SCOPES', 'openid email profile'),
        },
        'ldap': {
            'configured': ldap_provider.is_configured(),
            'server_url': os.environ.get('LDAP_SERVER_URL', ''),
            'base_dn': os.environ.get('LDAP_BASE_DN', ''),
            'search_filter': os.environ.get('LDAP_USER_SEARCH_FILTER', '(uid={username})'),
        },
    })
