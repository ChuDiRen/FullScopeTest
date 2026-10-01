"""
Webhook 调试器模块 - FastAPI 平迁（自 app/api/webhook_debugger.py，蓝图挂载前缀 /api/v1）

路径/方法/状态码/响应字段与原 v1 完全一致（/api/v1/webhook-debugger...、
/api/v1/webhook/{token}）。

鉴权映射：
- @jwt_required() → Depends(_current_user)（内部复用 deps.get_current_user）
- 公开端点（v1 即无鉴权，保持公开并注明）：
  * ANY /api/v1/webhook/{token} — 接收任意来源的 Webhook 请求（调试接收器）。
    与 v1 一致不做属主校验：token（16 位随机 hex）即能力凭证，不可枚举，
    非顺序 ID，不构成 IDOR 面。

会话生命周期：
- 同步端点统一加 @release_session（deps.release_session），防 NullPool 连接泄漏
- 鉴权依赖包装为 async _current_user：在事件循环线程执行 get_current_user

响应形态与 v1 一致：
- 接收端点成功时返回裸 JSON 体 {"ok": true}（非统一信封），
  并带 Access-Control-Allow-Origin 回显 Origin——复刻 Flask make_response 行为
"""

from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from ..deps import get_current_user, json_body, query_int, release_session
from ....core.logging import get_logger
from ....models.user import User

logger = get_logger(__name__)

router = APIRouter(tags=["webhook-debugger"])


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


async def _raw_body(request: Request) -> str:
    """读取原始请求体文本（同步端点无法 await，经依赖在事件循环线程完成读取）"""
    raw = await request.body()
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def _get_service():
    from ....services.webhook_debugger_service import get_webhook_debugger_service

    return get_webhook_debugger_service()


# ==================== 调试 Webhook 管理 ====================

@router.get("/api/v1/webhook-debugger")
@release_session
def list_debug_webhooks(user: User = Depends(_current_user)):
    """列出所有调试 Webhook"""
    service = _get_service()
    webhooks = service.list_webhooks()
    return _success(data=webhooks)


@router.post("/api/v1/webhook-debugger")
@release_session
def create_debug_webhook(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """创建调试 Webhook"""
    data = data or {}
    name = data.get("name", "")
    service = _get_service()
    result = service.create_webhook(name=name)
    return _success(data=result, message="Webhook 已创建", code=200)


@router.get("/api/v1/webhook-debugger/{token}/requests")
@release_session
def get_debug_webhook_requests(
    token: str,
    request: Request,
    user: User = Depends(_current_user),
):
    """获取 Webhook 请求日志"""
    limit = query_int(request, "limit", 100)
    service = _get_service()
    result = service.get_requests(token, limit=limit)
    if "error" in result:
        return _error(404, result["error"])
    return _success(data=result)


@router.delete("/api/v1/webhook-debugger/{token}/requests")
@release_session
def clear_debug_webhook_requests(token: str, user: User = Depends(_current_user)):
    """清空 Webhook 请求日志"""
    service = _get_service()
    service.clear_requests(token)
    return _success(message="日志已清空")


# ==================== Webhook 接收器（公开端点） ====================

@router.api_route(
    "/api/v1/webhook/{token}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
)
@release_session
def receive_webhook(
    token: str,
    request: Request,
    raw_body: str = Depends(_raw_body),
):
    """
    接收 Webhook 请求（公开端点：v1 无鉴权，供第三方服务回调调试，保持公开）。

    成功响应为裸 JSON 体 {"ok": true}（复刻 v1 make_response），非统一信封。
    """
    service = _get_service()
    result = service.record_request(
        token=token,
        method=request.method,
        path=request.url.path,
        headers=dict(request.headers),
        body=raw_body,
        query_params={k: v for k, v in request.query_params.items()},
    )
    if "error" in result:
        return _error(404, result["error"])

    return Response(
        content='{"ok": true}',
        status_code=200,
        media_type="application/json",
        headers={
            "Access-Control-Allow-Origin": request.headers.get("Origin", ""),
        },
    )
