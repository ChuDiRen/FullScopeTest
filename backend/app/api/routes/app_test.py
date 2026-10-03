"""
APP 测试模块 - FastAPI 平迁（自 app/api/app_test.py，蓝图挂载前缀 /api/v1）

路径/方法/状态码/响应字段与原 v1 完全一致（/api/v1/app-test/...），
前端与 CI 脚本零改动。全部端点为同步 def，运行于 RequestContextMiddleware
push 的 app context 内，直接复用 数据库会话（app/database.py ContextVar 作用域）。

鉴权映射：
- @jwt_required() → Depends(_current_user)（内部复用 deps.get_current_user）
- 公开端点（v1 即无鉴权，保持公开并注明）：
  * GET /api/v1/app-test/health — 模块健康检查

会话生命周期：
- 同步端点统一加 @release_session（deps.release_session）：视图返回后
  rollback/commit/remove 本 worker 线程的 scoped session，防 NullPool 连接泄漏
- 鉴权依赖包装为 async _current_user：在事件循环线程执行 get_current_user，
  其 session 由 teardown_appcontext 正常回收（与 api_test.py/reports.py 范例一致）

IDOR 修复（沿用 v1 已有的属主过滤，越权/不存在一律 404）：
- collections/scripts 的 GET 列表、GET/PUT/DELETE 详情均按 user_id 过滤（v1 即如此）
- run 仅执行当前用户的脚本（v1 即如此），Celery 派发/同步 subprocess 逻辑原样平迁
"""

from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from app.api.v2.deps import get_current_user, json_body, query_int, query_str, release_session
from app.core.logging import get_logger
from app.extensions import db
from app.models.app_test_collection import AppTestCollection
from app.models.app_test_script import AppTestScript
from app.models.user import User
from sqlalchemy import select

logger = get_logger(__name__)

router = APIRouter(tags=["app-test"])


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


def _validate_json(data: Dict[str, Any], *required_fields: str):
    """等价 v1 @validate_json 装饰器的校验语义（错误消息与 v1 完全一致）"""
    if not data:
        return _error(400, "请求体不能为空")
    missing = [field for field in required_fields if field not in data]
    if missing:
        return _error(400, f'缺少必需字段: {", ".join(missing)}')
    return None


# ==================== 健康检查 ====================

@router.get("/api/v1/app-test/health")
@release_session
def app_test_health():
    """APP 测试模块健康检查（公开端点，v1 无 @jwt_required，保持公开）"""
    return _success(data={"status": "ok"}, message="APP 测试模块正常")


# ==================== 用例集管理 ====================

@router.get("/api/v1/app-test/collections")
@release_session
def get_app_collections(request: Request, user: User = Depends(_current_user)):
    """获取用例集列表（按属主过滤，v1 即有 user_id 过滤）"""
    user_id = user.id
    project_id = query_int(request, "project_id", 0) or None

    query = select(AppTestCollection).filter_by(user_id=user_id)
    if project_id:
        query = query.filter_by(project_id=project_id)

    collections = db.session.scalars(query.order_by(AppTestCollection.sort_order, AppTestCollection.created_at.desc())).all()
    return _success(data=[c.to_dict() for c in collections])


@router.post("/api/v1/app-test/collections")
@release_session
def create_app_collection(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """创建用例集"""
    data = data or {}
    error = _validate_json(data, "name")
    if error:
        return error

    collection = AppTestCollection(
        name=data["name"],
        description=data.get("description", ""),
        project_id=data.get("project_id"),
        user_id=user.id,
    )
    db.session.add(collection)
    db.session.commit()

    return _success(data=collection.to_dict(), message="创建成功", code=200)


@router.put("/api/v1/app-test/collections/{collection_id}")
@release_session
def update_app_collection(
    collection_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """更新用例集（属主过滤，越权/不存在 404）"""
    collection = db.session.scalar(select(AppTestCollection).filter_by(id=collection_id, user_id=user.id))

    if not collection:
        return _error(404, "用例集不存在")

    data = data or {}
    if "name" in data:
        collection.name = data["name"]
    if "description" in data:
        collection.description = data["description"]
    if "sort_order" in data:
        collection.sort_order = data["sort_order"]

    db.session.commit()
    return _success(data=collection.to_dict(), message="更新成功")


@router.delete("/api/v1/app-test/collections/{collection_id}")
@release_session
def delete_app_collection(collection_id: int, user: User = Depends(_current_user)):
    """删除用例集（属主过滤，越权/不存在 404）"""
    collection = db.session.scalar(select(AppTestCollection).filter_by(id=collection_id, user_id=user.id))

    if not collection:
        return _error(404, "用例集不存在")

    db.session.delete(collection)
    db.session.commit()
    return _success(message="删除成功")


# ==================== 脚本管理 ====================

@router.get("/api/v1/app-test/scripts")
@release_session
def get_app_scripts(request: Request, user: User = Depends(_current_user)):
    """获取脚本列表（按属主过滤，v1 即有 user_id 过滤）"""
    user_id = user.id
    project_id = query_int(request, "project_id", 0) or None
    collection_id = query_int(request, "collection_id", 0) or None

    query = select(AppTestScript).filter_by(user_id=user_id)
    if project_id:
        query = query.filter_by(project_id=project_id)
    if collection_id:
        query = query.filter_by(collection_id=collection_id)

    scripts = db.session.scalars(query.order_by(AppTestScript.sort_order, AppTestScript.created_at.desc())).all()
    return _success(data=[s.to_dict() for s in scripts])


@router.post("/api/v1/app-test/scripts")
@release_session
def create_app_script(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """创建脚本"""
    data = data or {}
    error = _validate_json(data, "name")
    if error:
        return error

    script = AppTestScript(
        name=data["name"],
        description=data.get("description", ""),
        project_id=data.get("project_id"),
        collection_id=data.get("collection_id"),
        platform=data.get("platform", "android"),
        app_path=data.get("app_path"),
        app_package=data.get("app_package"),
        app_activity=data.get("app_activity"),
        bundle_id=data.get("bundle_id"),
        device_name=data.get("device_name"),
        platform_version=data.get("platform_version"),
        automation_name=data.get("automation_name", "UiAutomator2"),
        appium_server=data.get("appium_server", "http://localhost:4723"),
        script_content=data.get("script_content", ""),
        user_id=user.id,
    )
    db.session.add(script)
    db.session.commit()

    return _success(data=script.to_dict(), message="创建成功", code=200)


@router.get("/api/v1/app-test/scripts/{script_id}")
@release_session
def get_app_script(script_id: int, user: User = Depends(_current_user)):
    """获取单个脚本（属主过滤，越权/不存在 404）"""
    script = db.session.scalar(select(AppTestScript).filter_by(id=script_id, user_id=user.id))

    if not script:
        return _error(404, "脚本不存在")

    return _success(data=script.to_dict())


@router.put("/api/v1/app-test/scripts/{script_id}")
@release_session
def update_app_script(
    script_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """更新脚本（属主过滤，越权/不存在 404）"""
    script = db.session.scalar(select(AppTestScript).filter_by(id=script_id, user_id=user.id))

    if not script:
        return _error(404, "脚本不存在")

    data = data or {}
    updatable_fields = [
        "name", "description", "collection_id", "platform", "app_path",
        "app_package", "app_activity", "bundle_id", "device_name",
        "platform_version", "automation_name", "appium_server",
        "script_content", "is_enabled", "sort_order",
    ]

    for field in updatable_fields:
        if field in data:
            setattr(script, field, data[field])

    db.session.commit()
    return _success(data=script.to_dict(), message="更新成功")


@router.delete("/api/v1/app-test/scripts/{script_id}")
@release_session
def delete_app_script(script_id: int, user: User = Depends(_current_user)):
    """删除脚本（属主过滤，越权/不存在 404）"""
    script = db.session.scalar(select(AppTestScript).filter_by(id=script_id, user_id=user.id))

    if not script:
        return _error(404, "脚本不存在")

    db.session.delete(script)
    db.session.commit()
    return _success(message="删除成功")


# ==================== 执行测试 ====================

@router.post("/api/v1/app-test/scripts/{script_id}/run")
@release_session
def run_app_script(script_id: int, user: User = Depends(_current_user)):
    """
    执行单个脚本（属主过滤，越权/不存在 404）

    与 v1 完全一致的双路径派发：
    - CELERY_ENABLE=true 时派发 run_app_test_task（保持原调用）
    - 否则同步 subprocess 执行脚本内容
    """
    from app.core.runtime import get_config

    user_id = user.id
    script = db.session.scalar(select(AppTestScript).filter_by(id=script_id, user_id=user_id))

    if not script:
        return _error(404, "脚本不存在")

    if script.status == "running":
        return _error(400, "脚本正在执行中")

    # 检查 Celery 是否启用
    if get_config().get("CELERY_ENABLE", False):
        from app.tasks import run_app_test_task
        task = run_app_test_task.apply_async(args=[script_id, user_id])
        return _success(
            data={"script_id": script.id, "task_id": task.id, "status": "running"},
            message="脚本已提交执行",
        )
    else:
        # Celery 未启用时，同步执行
        script.status = "running"
        script.last_run_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.session.commit()

        try:
            import os

            from app.utils.sandbox import execute_script

            work_dir = os.path.join(str(Path(__file__).resolve().parents[3]), "data", "app_tests", str(script_id))
            os.makedirs(work_dir, exist_ok=True)

            # 与 Celery 任务同款沙箱：AST 检查 + 最小化 env（不继承后端密钥）+ 审计日志
            sandbox_result = execute_script(
                script_content=script.script_content,
                user_id=user_id,
                timeout=300,
                work_dir=work_dir,
                script_id=script_id,
                script_type="app",
            )

            success = sandbox_result["success"]
            script.status = "passed" if success else "failed"
            script.last_result = {
                "success": success,
                "duration": sandbox_result["duration"],
                "stdout": sandbox_result["stdout"],
                "stderr": sandbox_result["stderr"],
                "return_code": sandbox_result.get("return_code"),
                "error": sandbox_result.get("error"),
                "timestamp": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            }
            db.session.commit()

            return _success(
                data={"script_id": script.id, "status": script.status, "result": script.last_result},
                message="脚本执行完成",
            )

        except Exception as e:
            script.status = "failed"
            script.last_result = {
                "success": False,
                "error": str(e),
                "timestamp": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            }
            db.session.commit()
            return _error(500, f"执行失败: {str(e)}")


@router.get("/api/v1/app-test/devices")
@release_session
def get_devices(request: Request, user: User = Depends(_current_user)):
    """
    获取 Appium Server 连接的设备列表

    查询参数:
        server_url: Appium Server 地址（默认 http://localhost:4723）
    """
    server_url = query_str(request, "server_url", "http://localhost:4723") or "http://localhost:4723"

    # 尝试连接 Appium Server 获取设备信息（连接失败降级为空列表，与 v1 一致）
    try:
        import requests as req
        resp = req.get(f"{server_url}/status", timeout=5)
        if resp.status_code == 200:
            server_info = resp.json()
            # 尝试获取 sessions 信息
            try:
                sessions_resp = req.get(f"{server_url}/sessions", timeout=5)
                sessions = sessions_resp.json().get("value", []) if sessions_resp.status_code == 200 else []
            except Exception:
                sessions = []

            return _success(data={
                "server_status": {
                    "url": server_url,
                    "connected": True,
                    "version": server_info.get("value", {}).get("build", {}).get("version", "unknown"),
                    "device_count": len(sessions),
                },
                "devices": [
                    {
                        "id": s.get("id", ""),
                        "name": s.get("capabilities", {}).get("deviceName", "Unknown"),
                        "platform": s.get("capabilities", {}).get("platformName", "android").lower(),
                        "version": s.get("capabilities", {}).get("platformVersion", "unknown"),
                        "model": s.get("capabilities", {}).get("deviceModel", s.get("capabilities", {}).get("deviceName", "")),
                        "udid": s.get("capabilities", {}).get("udid", ""),
                        "status": "online",
                        "screen_size": s.get("capabilities", {}).get("screenSize", ""),
                    }
                    for s in sessions
                ],
            })
        else:
            return _success(data={
                "server_status": {"url": server_url, "connected": False},
                "devices": [],
            })
    except Exception as e:
        logger.debug("Appium Server 连接失败", server_url=server_url, error=str(e))
        return _success(data={
            "server_status": {"url": server_url, "connected": False},
            "devices": [],
        })
