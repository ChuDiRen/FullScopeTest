"""
JWT 工具（纯 PyJWT 实现，零 Flask 依赖）

令牌 claims:
{
  "sub": "<user_id 字符串>",
  "type": "access" | "refresh",
  "jti": "<uuid>",
  "iat": <unix>,
  "nbf": <unix>,
  "exp": <unix>,
  "token_version": <int>
}
"""

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import jwt as pyjwt

from ..core.logging import get_logger

logger = get_logger(__name__)

ALGORITHM = "HS256"


def _secret() -> str:
    from .runtime import get_config

    secret = get_config().get("JWT_SECRET_KEY") or os.environ.get("JWT_SECRET_KEY", "")
    if not secret:
        raise RuntimeError("JWT_SECRET_KEY 未配置")
    return secret


def _expires(key: str, default: timedelta) -> timedelta:
    from .runtime import get_config

    value = get_config().get(key, default)
    if isinstance(value, timedelta):
        return value
    return timedelta(seconds=int(value))


def _create_token(identity: str, token_type: str, expires_delta: timedelta) -> str:
    now = datetime.now(timezone.utc)
    from ..services.token_blacklist import get_user_token_version

    try:
        token_version = get_user_token_version(int(identity))
    except (ValueError, TypeError):
        token_version = 0

    payload = {
        "sub": str(identity),
        "type": token_type,
        "jti": uuid.uuid4().hex,
        "iat": now,
        "nbf": now,
        "exp": now + expires_delta,
        "token_version": token_version,
    }
    return pyjwt.encode(payload, _secret(), algorithm=ALGORITHM)


def create_access_token(identity: str, additional_claims: Optional[Dict[str, Any]] = None) -> str:
    """创建访问令牌（30 分钟，JWT_ACCESS_TOKEN_EXPIRES 可覆盖）"""
    token = _create_token(identity, "access", _expires("JWT_ACCESS_TOKEN_EXPIRES", timedelta(minutes=30)))
    if additional_claims:
        logger.warning("additional_claims 参数已弃用，token_version 由 runtime 自动注入")
    return token


def create_refresh_token(identity: str) -> str:
    """创建刷新令牌（30 天，JWT_REFRESH_TOKEN_EXPIRES 可覆盖）"""
    return _create_token(identity, "refresh", _expires("JWT_REFRESH_TOKEN_EXPIRES", timedelta(days=30)))


def decode_token(token: str) -> Dict[str, Any]:
    """
    解码并校验令牌（签名 + exp/nbf）。

    失败抛 PyJWT 异常（与 flask_jwt_extended 一样由调用方转 401）。
    """
    return pyjwt.decode(
        token,
        _secret(),
        algorithms=[ALGORITHM],
        options={"require": ["exp", "sub"]},
    )
