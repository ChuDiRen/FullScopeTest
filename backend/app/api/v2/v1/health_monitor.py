"""
API 健康监控模块 - FastAPI 平迁（自 app/api/health_monitor.py，蓝图挂载前缀 /api/v1）

路径/方法/状态码/响应字段与原 v1 完全一致（/api/v1/health-monitor/...）。
全部端点为同步 def，统一加 @release_session（deps.release_session）。

鉴权映射（与 v1 一致）：
- v1 六个端点均为 @jwt_required()（登录用户即可，无 admin 校验），
  平迁统一用 deps.get_current_user（经 async 包装在事件循环线程执行）：
  * GET    /api/v1/health-monitor
  * POST   /api/v1/health-monitor
  * GET    /api/v1/health-monitor/{monitor_id}
  * DELETE /api/v1/health-monitor/{monitor_id}
  * POST   /api/v1/health-monitor/{monitor_id}/check
  * GET    /api/v1/health-monitor/{monitor_id}/stats

注意：monitor 存储为服务层内存态（health_monitor_service._monitor_store），
与 v1 行为一致，不做持久化迁移。
"""

from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..deps import get_current_user, json_body, query_int, release_session
from ....core.logging import get_logger
from ....models.user import User
from ....services.health_monitor_service import get_health_monitor_service

logger = get_logger(__name__)

router = APIRouter(tags=["health-monitor"])


# ---------------------------------------------------------------------------
# 鉴权包装 / 响应构造（等价 app/utils/response.py 的 JSON 结构）
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


@router.get("/api/v1/health-monitor")
@release_session
def list_monitors(user: User = Depends(_current_user)):
    """列出所有监控规则（v1 仅 @jwt_required，登录用户即可）"""
    service = get_health_monitor_service()
    monitors = service.list_monitors()
    return _success(data=monitors)


@router.post("/api/v1/health-monitor")
@release_session
def create_monitor(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """创建监控规则（v1 仅 @jwt_required）"""
    data = data or {}
    if not data.get('url'):
        return _error(400, '缺少监控 URL')
    if not data.get('name'):
        return _error(400, '缺少监控名称')

    service = get_health_monitor_service()
    result = service.create_monitor(data)
    return _success(data=result, message='监控规则已创建', code=200)


@router.get("/api/v1/health-monitor/{monitor_id}")
@release_session
def get_monitor(monitor_id: int, user: User = Depends(_current_user)):
    """获取监控详情（v1 仅 @jwt_required）"""
    service = get_health_monitor_service()
    result = service.get_monitor(monitor_id)
    if not result:
        return _error(404, '监控规则不存在')
    return _success(data=result)


@router.delete("/api/v1/health-monitor/{monitor_id}")
@release_session
def delete_monitor(monitor_id: int, user: User = Depends(_current_user)):
    """删除监控规则（v1 仅 @jwt_required）"""
    service = get_health_monitor_service()
    if service.delete_monitor(monitor_id):
        return _success(message='监控规则已删除')
    return _error(404, '监控规则不存在')


@router.post("/api/v1/health-monitor/{monitor_id}/check")
@release_session
def run_health_check(monitor_id: int, user: User = Depends(_current_user)):
    """执行一次健康检查（v1 仅 @jwt_required）"""
    service = get_health_monitor_service()
    result = service.run_check(monitor_id)
    if 'error' in result:
        return _error(400, result['error'])
    return _success(data=result, message='检查完成')


@router.get("/api/v1/health-monitor/{monitor_id}/stats")
@release_session
def get_uptime_stats(request: Request, monitor_id: int, user: User = Depends(_current_user)):
    """获取可用率统计（v1 仅 @jwt_required）"""
    days = query_int(request, 'days', 7)
    service = get_health_monitor_service()
    result = service.get_uptime_stats(monitor_id, days=days)
    if 'error' in result:
        return _error(404, result['error'])
    return _success(data=result)
