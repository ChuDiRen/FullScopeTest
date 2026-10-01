"""
FastAPI ASGI 中间件集

- DbSessionMiddleware: 每请求一个 DB 会话作用域（ContextVar 令牌，响应后 commit/回滚/remove）
- RequestContextMiddleware: 生成 request_id 并写入 scope state
- SecurityHeadersMiddleware: 安全响应头（与 app/middleware/security_headers.py 策略一致）
- BodySizeLimitMiddleware: 请求体大小限制（等价 MAX_CONTENT_LENGTH）
- RateLimitMiddleware: Redis 滑动窗口限流（复用 rate_limit_service）

限流 fail 策略：RATE_LIMIT_FAIL_OPEN（默认 true；生产环境未显式设置时为 false=拒绝请求）。
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Optional, Sequence

from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.concurrency import run_in_threadpool

from ...core.logging import get_logger

logger = get_logger(__name__)

# 不参与限流的路径前缀（健康检查/文档/指标）
RATE_LIMIT_EXEMPT_PREFIXES = (
    "/health",
    "/metrics",
    "/api/v2/docs",
    "/api/v2/redoc",
    "/api/v2/openapi.json",
)


# ---------------------------------------------------------------------------
# 请求作用域：request_id / 租户 / DB 会话生命周期（替代原 Flask 中间件栈）
# ---------------------------------------------------------------------------

class DbSessionMiddleware:
    """
    每请求一个数据库会话作用域：

    - 放置新的 scope 令牌（database._session_scope ContextVar），
      同步端点（anyio 线程池拷贝上下文）与异步端点共享同一 scoped session
    - 响应后 commit（异常回滚），最后 remove 释放会话
    - 响应头追加 x-request-id
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        from ...database import _session_scope
        from ...core.runtime import session_teardown

        scope_token = _session_scope.set(object())

        async def send_with_request_id(message):
            if message["type"] == "http.response.start":
                request_id = scope.get("state", {}).get("request_id", "")
                if request_id:
                    headers = list(message.get("headers", []))
                    headers.append((b"x-request-id", request_id.encode("latin-1")))
                    message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
            session_teardown(None)
        except BaseException as exc:
            session_teardown(exc)
            raise
        finally:
            _session_scope.reset(scope_token)
            rl_token = (scope.get("state") or {}).get("rl_token")
            if rl_token is not None:
                try:
                    from ...core.request_local import reset_request_info

                    reset_request_info(rl_token)
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# request_id
# ---------------------------------------------------------------------------

class RequestContextMiddleware:
    """
    请求上下文初始化：

    - 生成 request_id（优先复用上游 X-Request-ID）写入 scope state 与 runtime.ctx
    - 解析 X-Organization-ID 写入 runtime.ctx（成员资格由查询层校验）
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            from ...core.runtime import ctx

            headers = dict(
                (k.decode("latin-1"), v.decode("latin-1"))
                for k, v in scope.get("headers", [])
            )
            request_id = headers.get("x-request-id", "").strip() or f"req_{uuid.uuid4().hex[:16]}"
            scope.setdefault("state", {})
            scope["state"]["request_id"] = request_id
            scope["state"]["client_ip"] = headers.get("x-forwarded-for", "").split(",")[0].strip() \
                if os.environ.get("TRUST_PROXY_HEADERS", "").lower() == "true" else None

            ctx.set_request_id(request_id)
            org_raw = headers.get("x-organization-id", "").strip()
            ctx.set_organization_id(int(org_raw) if org_raw.isdigit() else None)

            # 填充 request_local（审计日志等同步服务读取请求信息的唯一通道）
            try:
                from ...core.request_local import set_request_info

                rl_token = None
                client = scope.get("client")
                direct_ip = client[0] if client else "127.0.0.1"
                client_ip = (
                    headers.get("x-forwarded-for", "").split(",")[0].strip()
                    if os.environ.get("TRUST_PROXY_HEADERS", "").lower() == "true"
                    else direct_ip
                )
                auth = headers.get("authorization", "")
                token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
                if not token:
                    for k, v in scope.get("headers", []):
                        if k.decode("latin-1").lower() == "cookie":
                            for part in v.decode("latin-1").split(";"):
                                part = part.strip()
                                if part.startswith("access_token_cookie="):
                                    token = part.split("=", 1)[1]
                                    break
                            break
                jwt_identity = None
                if token:
                    try:
                        from ...core.jwt import decode_token

                        jwt_identity = decode_token(token).get("sub")
                    except Exception:
                        jwt_identity = None

                qs = {}
                for pair in scope.get("query_string", b"").decode("latin-1").split("&"):
                    if not pair:
                        continue
                    k, _, v = pair.partition("=")
                    qs[k] = v

                rl_token = set_request_info(
                    method=scope.get("method", ""),
                    path=scope.get("path", ""),
                    url=str(scope.get("path", "")),
                    client_ip=client_ip,
                    user_agent=headers.get("user-agent", ""),
                    headers=headers,
                    query_params=qs,
                    jwt_identity=jwt_identity,
                )
                scope["state"]["rl_token"] = rl_token
            except Exception:
                pass  # request_local 填充失败不影响请求
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# 安全响应头
# ---------------------------------------------------------------------------

class SecurityHeadersMiddleware:
    """安全响应头，策略与 app/middleware/security_headers.py 保持一致"""

    def __init__(self, app: ASGIApp, is_production: bool = False):
        self.app = app
        self.is_production = is_production
        self.enabled = os.environ.get(
            "SECURITY_HEADERS_ENABLED", "true"
        ).strip().lower() == "true"

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.enabled:
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing = {k.lower() for k, _ in headers}

                def add(name: str, value: str):
                    key = name.lower().encode("latin-1")
                    if key not in existing:
                        headers.append((key, value.encode("latin-1")))

                add("X-Content-Type-Options", "nosniff")
                add("X-Frame-Options", "DENY")
                add("X-XSS-Protection", "0")
                add("Referrer-Policy", "strict-origin-when-cross-origin")
                add("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
                if self.is_production:
                    add("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
                    add(
                        "Content-Security-Policy",
                        "default-src 'self'; script-src 'self'; "
                        "style-src 'self' 'unsafe-inline'; img-src 'self' data: https:; "
                        "font-src 'self' https://fonts.gstatic.com; connect-src 'self'; "
                        "frame-ancestors 'none'",
                    )
                else:
                    add(
                        "Content-Security-Policy",
                        "default-src 'self'; script-src 'self' 'unsafe-eval' 'unsafe-inline'; "
                        "style-src 'self' 'unsafe-inline'; img-src 'self' data: https: blob:; "
                        "font-src 'self' https://fonts.gstatic.com data:; "
                        "connect-src 'self' ws: wss: http: https:; frame-ancestors 'none'",
                    )
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


# ---------------------------------------------------------------------------
# 请求体大小限制
# ---------------------------------------------------------------------------

class BodySizeLimitMiddleware:
    """请求体大小限制（等价 Flask MAX_CONTENT_LENGTH），超限返回 413"""

    def __init__(self, app: ASGIApp, max_bytes: int = 16 * 1024 * 1024):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(
                (k.decode("latin-1"), v.decode("latin-1"))
                for k, v in scope.get("headers", [])
            )
            content_length = headers.get("content-length")
            if content_length and content_length.isdigit() and int(content_length) > self.max_bytes:
                response_headers = [
                    (b"content-type", b"application/json"),
                ]
                body = (
                    b'{"code": 413, "message": "\\u8bf7\\u6c42\\u4f53\\u8d85\\u8fc7\\u5927\\u5c0f\\u9650\\u5236"}'
                )
                await send({
                    "type": "http.response.start",
                    "status": 413,
                    "headers": response_headers,
                })
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# 限流
# ---------------------------------------------------------------------------

class RateLimitMiddleware:
    """
    Redis 滑动窗口限流（等价 app/middleware/rate_limit.py）：

    - 登录用户按 user_id 限流（API token 使用独立额度）
    - 未登录按 IP 限流（默认 50/min）
    - 生产环境 Redis 故障时默认拒绝（fail-closed），可用 RATE_LIMIT_FAIL_OPEN=true 放开
    """

    def __init__(
        self,
        app: ASGIApp,
        jwt_secret: str,
        enabled: bool = True,
        anonymous_limit: int = 50,
        fail_open: bool = True,
    ):
        self.app = app
        self.jwt_secret = jwt_secret
        self.enabled = enabled
        self.anonymous_limit = anonymous_limit
        self.fail_open = fail_open

    def _identify(self, scope: Scope) -> tuple:
        """返回 (rate_key, limit)；解码失败视为匿名"""
        headers = dict(
            (k.decode("latin-1"), v.decode("latin-1"))
            for k, v in scope.get("headers", [])
        )
        auth = headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not token:
            for k, v in scope.get("headers", []):
                if k.decode("latin-1").lower() == "cookie":
                    cookies = dict(
                        part.strip().split("=", 1)
                        for part in v.decode("latin-1").split(";")
                        if "=" in part
                    )
                    token = cookies.get("access_token_cookie", "")
                    break

        is_api_token = bool(headers.get("authorization"))
        if token:
            try:
                import jwt as pyjwt

                claims = pyjwt.decode(
                    token, self.jwt_secret, algorithms=["HS256"], options={"verify_exp": True}
                )
                user_id = claims.get("sub")
                if user_id:
                    return f"rate_limit:user:{user_id}", None, is_api_token
            except Exception:
                pass

        client = scope.get("client")
        ip = client[0] if client else "127.0.0.1"
        return f"rate_limit:ip:{ip}", self.anonymous_limit, False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.enabled:
            await self.app(scope, receive, send)
            return

        # 开关每请求读取：测试/运维可运行时热切换（get_config 为进程级字典）
        from ...core.runtime import get_config
        if not get_config().get("RATELIMIT_ENABLED", True):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path.startswith(RATE_LIMIT_EXEMPT_PREFIXES) or scope.get("method") == "OPTIONS":
            await self.app(scope, receive, send)
            return

        try:
            from ...services.rate_limit_service import (
                get_rate_limit_headers,
                get_user_rate_limit,
                sliding_window_rate_limit,
            )

            rate_key, anon_limit, is_api_token = self._identify(scope)
            if anon_limit is None:
                limit = await run_in_threadpool(
                    get_user_rate_limit, int(rate_key.rsplit(":", 1)[1]), is_api_token
                )
            else:
                limit = anon_limit

            # 同步 Redis 客户端调用必须进线程池：直接在事件循环里调用会以
            # socket_timeout（2s）为粒度阻塞整个 loop（Redis 故障时每个请求都拖死全站）
            if not await run_in_threadpool(sliding_window_rate_limit, rate_key, limit):
                headers = await run_in_threadpool(get_rate_limit_headers, rate_key, limit)
                retry_after = int(headers.get("Retry-After", 60))
                response_headers = [
                    (b"content-type", b"application/json"),
                ]
                # 避免与 get_rate_limit_headers 返回的 Retry-After 重复
                if not any(name.lower() == "retry-after" for name in headers):
                    response_headers.append(
                        (b"retry-after", str(retry_after).encode("latin-1"))
                    )
                for name, value in headers.items():
                    response_headers.append(
                        (name.lower().encode("latin-1"), str(value).encode("latin-1"))
                    )
                body = (
                    '{"code": 429, "message": "请求频率超过限制 (%d req/min)", '
                    '"retry_after": %d}' % (limit, retry_after)
                ).encode("utf-8")
                await send({
                    "type": "http.response.start",
                    "status": 429,
                    "headers": response_headers,
                })
                await send({"type": "http.response.body", "body": body})
                return
        except Exception as exc:
            logger.error("Rate limit check failed", error=str(exc))
            if not self.fail_open:
                body = b'{"code": 503, "message": "\\u9650\\u6d41\\u670d\\u52a1\\u6682\\u4e0d\\u53ef\\u7528"}'
                await send({
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [(b"content-type", b"application/json")],
                })
                await send({"type": "http.response.body", "body": body})
                return

        await self.app(scope, receive, send)
