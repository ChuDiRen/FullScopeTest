"""
独立 Mock Server API - FastAPI 平迁（自 app/api/mock_server.py，蓝图挂载前缀 /api/v1）

路径/方法/状态码/响应字段与原 v1 完全一致（/api/v1/mock-servers、
/api/v1/mock-rules、/api/v1/mock/{server_id}/{subpath}），前端与 CI 脚本零改动。
管理类端点全部为同步 def，运行于 RequestContextMiddleware push 的 app context 内，
直接复用 数据库会话（app/database.py ContextVar 作用域） 与 MockServerService 服务层。

鉴权映射：
- @jwt_required() → Depends(_current_user)（内部复用 deps.get_current_user）
- 公开端点（v1 即无鉴权，保持公开并注明）：
  * ANY /api/v1/mock/{server_id}/{subpath:path} — Mock 代理执行端点
    （7 种 HTTP 方法，供前端开发/第三方直接调用的无鉴权设计端点，
    靠 server_id 不可枚举性 + 可选规则匹配保护，无 JWT）

会话生命周期：
- 同步端点统一加 @release_session（deps.release_session）
- 鉴权依赖包装为 async _current_user（与 api_test.py/reports.py 范例一致）
- Mock 代理端点需要读取请求体（同步 def 无法 await），body 经 async 依赖
  _mock_request_payload 在事件循环线程读取后传入同步视图，DB 操作仍在
  视图内（事件循环线程的 session 由 teardown_appcontext 回收）

属主校验（与源文件一致，全部走服务层 user_id 参数，路由层把 current_user.id 传下去）：
- MockServer 无 user_id 字段，服务层通过 project.owner_id 比对
  （MockServerService._verify_server_owner），不存在/越权统一 404
- 路由层额外校验（与 v1 一致/补齐）：
  * GET  /mock-servers：project_id 归属校验（v1 已有，越权 404）
  * POST /mock-servers：project_id 归属校验（IDOR 修复：v1 服务层 create_server
    不校验项目归属，可向他人项目注入 Mock 服务器）
  * rule CRUD / 日志：服务层 _get_owned_server / update_rule 内部校验
"""

from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from app.api.v2.deps import get_current_user, json_body, query_int, release_session
from app.core.logging import get_logger
from app.models.project import Project
from app.models.user import User
from app.utils.exceptions import NotFoundError
from sqlalchemy import select
from app.extensions import db

logger = get_logger(__name__)

router = APIRouter(tags=["mock-server"])


# ---------------------------------------------------------------------------
# 鉴权包装 / 响应构造（等价 app/utils/response.py 的 JSON 结构）
# ---------------------------------------------------------------------------

async def _current_user(request: Request) -> User:
    """鉴权依赖：在事件循环线程内执行同步 get_current_user，session 随 app context 回收"""
    return get_current_user(request)


def _request_id() -> str:
    """读取 RequestContextMiddleware 写入 scope state 的 request_id"""
    try:
        from app.core.runtime import ctx

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


def _get_mock_server_service():
    """获取 MockServerService 单例（惰性导入，与 v1 一致）"""
    from app.services.mock_server_service import get_mock_server_service

    return get_mock_server_service()


def _owned_project(user_id: int, project_id: Any) -> bool:
    """校验项目归属当前用户（不存在/越权/非法 id 一律 False）"""
    try:
        pid = int(project_id)
    except (TypeError, ValueError):
        return False
    return bool(db.session.scalar(select(Project).filter_by(id=pid, owner_id=user_id)))


# ==================== Mock Server CRUD ====================

@router.get("/api/v1/mock-servers")
@release_session
def get_mock_servers(request: Request, user: User = Depends(_current_user)):
    """获取项目下的 Mock 服务器列表（project 归属校验，v1 已有，越权 404）"""
    project_id = query_int(request, "project_id", 0)
    if not project_id:
        return _error(400, "缺少 project_id 参数")

    # 校验项目归属当前用户，防止越权枚举他人项目的 Mock 服务器（v1 已有）
    if not _owned_project(user.id, project_id):
        return _error(404, "项目不存在")

    try:
        service = _get_mock_server_service()
        servers = service.get_servers(project_id)
        return _success(data=servers)
    except Exception as exc:
        logger.error("获取 Mock 服务器列表失败", error=str(exc))
        return _error(500, f"获取失败: {str(exc)}")


@router.post("/api/v1/mock-servers")
@release_session
def create_mock_server(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """创建 Mock 服务器（IDOR 修复：补 project 归属校验，防止向他人项目注入）"""
    data = data or {}
    if not data.get("name"):
        return _error(400, "缺少服务器名称")
    if not data.get("project_id"):
        return _error(400, "缺少 project_id")

    # IDOR 修复：v1 路由/服务层均未校验项目归属，可向他人项目注入 Mock 服务器
    # （管理类端点属主校验要求：与列表端点的 project.owner_id 校验同一边界）
    if not _owned_project(user.id, data.get("project_id")):
        return _error(404, "项目不存在")

    try:
        service = _get_mock_server_service()
        server = service.create_server(data, user_id=user.id)
        return _success(data=server, message="Mock 服务器已创建", code=200)
    except Exception as exc:
        logger.error("创建 Mock 服务器失败", error=str(exc))
        return _error(500, f"创建失败: {str(exc)}")


@router.get("/api/v1/mock-servers/{server_id}")
@release_session
def get_mock_server(server_id: int, user: User = Depends(_current_user)):
    """获取 Mock 服务器详情（含规则，服务层 user_id 属主校验，越权 404）"""
    try:
        service = _get_mock_server_service()
        server = service.get_server(server_id, user_id=user.id)
        return _success(data=server)
    except NotFoundError as exc:
        return _error(404, str(exc))
    except Exception as exc:
        logger.error("获取 Mock 服务器详情失败", error=str(exc))
        return _error(500, f"获取失败: {str(exc)}")


@router.put("/api/v1/mock-servers/{server_id}")
@release_session
def update_mock_server(
    server_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """更新 Mock 服务器（服务层 user_id 属主校验，越权 404）"""
    data = data or {}

    try:
        service = _get_mock_server_service()
        server = service.update_server(server_id, data, user_id=user.id)
        return _success(data=server, message="Mock 服务器已更新")
    except NotFoundError as exc:
        return _error(404, str(exc))
    except Exception as exc:
        logger.error("更新 Mock 服务器失败", error=str(exc))
        return _error(500, f"更新失败: {str(exc)}")


@router.delete("/api/v1/mock-servers/{server_id}")
@release_session
def delete_mock_server(server_id: int, user: User = Depends(_current_user)):
    """删除 Mock 服务器（服务层 user_id 属主校验，越权 404）"""
    try:
        service = _get_mock_server_service()
        service.delete_server(server_id, user_id=user.id)
        return _success(message="Mock 服务器已删除")
    except NotFoundError as exc:
        return _error(404, str(exc))
    except Exception as exc:
        logger.error("删除 Mock 服务器失败", error=str(exc))
        return _error(500, f"删除失败: {str(exc)}")


# ==================== Mock Rule 管理 ====================

@router.post("/api/v1/mock-servers/{server_id}/rules")
@release_session
def create_mock_rule(
    server_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """创建 Mock 规则（服务层经 server → project.owner 属主校验，越权 404）"""
    data = data or {}
    if not data.get("name"):
        return _error(400, "缺少规则名称")
    if not data.get("match_path"):
        return _error(400, "缺少匹配路径")

    try:
        service = _get_mock_server_service()
        rule = service.create_rule(server_id, data, user_id=user.id)
        return _success(data=rule, message="规则已创建", code=200)
    except NotFoundError as exc:
        return _error(404, str(exc))
    except Exception as exc:
        logger.error("创建 Mock 规则失败", error=str(exc))
        return _error(500, f"创建失败: {str(exc)}")


@router.put("/api/v1/mock-rules/{rule_id}")
@release_session
def update_mock_rule(
    rule_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """更新 Mock 规则（服务层经 rule → server → project.owner 属主校验，越权 404）"""
    data = data or {}

    try:
        service = _get_mock_server_service()
        rule = service.update_rule(rule_id, data, user_id=user.id)
        return _success(data=rule, message="规则已更新")
    except NotFoundError as exc:
        return _error(404, str(exc))
    except Exception as exc:
        logger.error("更新 Mock 规则失败", error=str(exc))
        return _error(500, f"更新失败: {str(exc)}")


@router.delete("/api/v1/mock-rules/{rule_id}")
@release_session
def delete_mock_rule(rule_id: int, user: User = Depends(_current_user)):
    """删除 Mock 规则（服务层经 rule → server → project.owner 属主校验，越权 404）"""
    try:
        service = _get_mock_server_service()
        service.delete_rule(rule_id, user_id=user.id)
        return _success(message="规则已删除")
    except NotFoundError as exc:
        return _error(404, str(exc))
    except Exception as exc:
        logger.error("删除 Mock 规则失败", error=str(exc))
        return _error(500, f"删除失败: {str(exc)}")


# ==================== Mock 请求处理 ====================

async def _mock_request_payload(request: Request) -> Dict[str, Any]:
    """
    Mock 代理请求载荷依赖：在事件循环线程读取 body（同步视图无法 await）。

    返回 {origin, method, path, query_params, headers, body}，与 v1
    handler 内 request.args/headers/get_data 的取值语义对齐。
    注：starlette Headers 键为小写（v1 Flask 为原始大小写），规则
    match_header 精确匹配时使用小写键即可对齐。
    """
    raw = await request.body()
    return {
        "origin": request.headers.get("Origin", ""),
        "method": request.method,
        "path": "/" + str(request.path_params.get("subpath", "")),
        "query_params": dict(request.query_params),
        "headers": dict(request.headers),
        "body": raw.decode("utf-8", errors="replace"),
    }


@router.api_route(
    "/api/v1/mock/{server_id}/{subpath:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
@release_session
def mock_server_proxy(
    server_id: int,
    request: Request,
    payload: Dict[str, Any] = Depends(_mock_request_payload),
):
    """
    Mock Server 代理端点（公开端点：v1 无鉴权设计，供前端开发直接调用，保持公开）

    接收请求，匹配规则，返回 Mock 响应。
    """
    origin = payload["origin"]
    method = payload["method"]

    # 处理 OPTIONS
    if method == "OPTIONS":
        return Response(
            status_code=200,
            headers={
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, PATCH, OPTIONS, HEAD",
                "Access-Control-Allow-Headers": "Content-Type, Authorization",
            },
        )

    try:
        service = _get_mock_server_service()
        result = service.handle_request(
            server_id=server_id,
            method=method,
            path=payload["path"],
            query_params=payload["query_params"],
            headers=payload["headers"],
            body=payload["body"],
        )

        headers = dict(result.get("headers") or {})
        headers["Access-Control-Allow-Origin"] = origin
        if "Content-Type" not in result.get("headers", {}):
            headers["Content-Type"] = "application/json"

        # 添加 Mock 标识
        if result.get("rule_name"):
            headers["X-Mock-Rule"] = result["rule_name"]
        headers["X-Mock-Server-ID"] = str(server_id)

        return Response(
            content=result.get("body", ""),
            status_code=result.get("code", 200),
            headers=headers,
        )

    except NotFoundError:
        return _error(404, "Mock 服务器不存在")
    except Exception as exc:
        logger.error("Mock 请求处理失败", server_id=server_id, error=str(exc))
        return _error(500, f"Mock 请求处理失败: {str(exc)}")


# ==================== 请求日志 ====================

@router.get("/api/v1/mock-servers/{server_id}/logs")
@release_session
def get_mock_request_logs(request: Request, server_id: int, user: User = Depends(_current_user)):
    """获取 Mock 请求日志（服务层 user_id 属主校验，越权 404）"""
    limit = query_int(request, "limit", 100)

    try:
        service = _get_mock_server_service()
        logs = service.get_request_logs(server_id, limit=limit, user_id=user.id)
        return _success(data=logs)
    except Exception as exc:
        logger.error("获取请求日志失败", error=str(exc))
        return _error(500, f"获取失败: {str(exc)}")


@router.delete("/api/v1/mock-servers/{server_id}/logs")
@release_session
def clear_mock_request_logs(server_id: int, user: User = Depends(_current_user)):
    """清空 Mock 请求日志（服务层 user_id 属主校验，越权 404）"""
    try:
        service = _get_mock_server_service()
        count = service.clear_request_logs(server_id, user_id=user.id)
        return _success(data={"deleted": count}, message="日志已清空")
    except Exception as exc:
        logger.error("清空日志失败", error=str(exc))
        return _error(500, f"清空失败: {str(exc)}")
