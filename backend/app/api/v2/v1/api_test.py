"""
接口测试模块 - FastAPI 平迁（自 app/api/api_test.py，蓝图挂载前缀 /api/v1）

路径/方法/状态码/响应字段与原 v1 完全一致（/api/v1/api-test/...），
前端与 CI 脚本零改动。全部端点为同步 def，运行于 RequestContextMiddleware
push 的 app context 内，直接复用 数据库会话（app/database.py ContextVar 作用域） 与 services 层。

鉴权映射：
- @jwt_required() → Depends(_current_user)（内部复用 deps.get_current_user）
- 公开端点（v1 即无鉴权，保持公开并注明）：
  * GET  /api/v1/api-test/health        — 模块健康检查
  * *    /api/v1/api-test/mock/{id}     — Mock Server（7 种方法，供前端/第三方直接调用的无鉴权设计端点）

会话生命周期：
- 同步端点统一加 @release_session（deps.release_session）：视图返回后
  rollback/commit/remove 本 worker 线程的 scoped session，防 NullPool 连接泄漏
- 鉴权依赖包装为 async _current_user：在事件循环线程执行 get_current_user，
  其 session 由 teardown_appcontext 正常回收（与 reports.py 范例一致）

IDOR 修复（按 id 取对象一律带属主过滤，越权/不存在 → 404）：
- GET/PUT/DELETE /api/v1/api-test/cases/{id}          ：service 本就有 user_id 过滤，沿用
- POST /api/v1/api-test/cases/{id}/run                ：service 本就有 user_id 过滤，沿用
- POST /api/v1/api-test/collections/{id}/run          ：service 本就有 user_id 过滤 + IDOR 拦截（403→越权场景不会到达），沿用
- GET  /api/v1/api-test/runs/{id}/progress            ：v1 无属主校验，补 TestRun.triggered_user_id 过滤
- GET  /api/v1/api-test/cases/{id}/versions           ：v1 无属主校验，补用例属主过滤
- GET  /api/v1/api-test/versions/{id}、versions/diff  ：v1 无属主校验，补 版本→用例→user_id 过滤
- POST /api/v1/api-test/execute（body.case_id）       ：v1 service query.get 无属主校验，补用例属主过滤
- POST /api/v1/api-test/heal-case、apply-heal         ：v1 query.get 无属主校验（apply-heal 甚至不校验存在性），补用例属主过滤
- POST /api/v1/api-test/detect-changes（body.case_id）：补用例属主过滤
- GET  /api/v1/api-test/collections/{id}/estimate     ：v1 无属主校验，补集合属主过滤
- project_id 类端点（import/postman、import/csv、smart-select、tags/stats、tags/filter、
  import-har、bdd/parse、bdd/import）：补 project 属主/组织可访问域校验（越权 404）
- history 系列沿用 v1 的 user_id 过滤（本就安全）

SSRF 补齐（接受用户 URL 的执行类路由，validate_url_safety）：
- POST /api/v1/api-test/execute          ：url 校验（mock 模式不外呼，跳过路由级校验，service 层发送前仍有 is_safe_url）
- POST /api/v1/api-test/execute-scenario ：base_url 与各步骤最终 URL（变量替换 + base_url 拼接后）校验

其他必要修正：
- POST /api/v1/api-test/bdd/import：v1 创建 ApiTestCollection/ApiTestCase 时漏传 user_id
  （NOT NULL 列，v1 实际必然 500），平迁时补 user_id=当前用户
"""

import os
import re
import shlex
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from ..deps import get_current_user, json_body, query_int, query_str, release_session
from ....core.logging import get_logger
from ....extensions import db
from ....models.api_test_case import ApiTestCollection, ApiTestCase
from ....models.environment import Environment
from ....models.organization import OrganizationMember
from ....models.project import Project
from ....models.response_history import ResponseHistory
from ....models.test_case_version import TestCaseVersion
from ....models.test_run import TestRun
from ....models.user import User
from ....services.api_case_service import ApiCaseService
from ....services.api_collection_service import ApiCollectionService
from ....services.api_execution_service import ApiExecutionService
from ....services.ai_config_service import AiConfigService
from ....utils.exceptions import AppError, NotFoundError, ValidationError, PermissionError
from ....utils.org_filter import filter_by_org_projects, filter_by_owner_or_org
from ....utils.sandbox import validate_url_safety
from ....utils.validators import validate_required
from ....utils.ai_planner import generate_api_test_plan
from ....utils.ai_data_synthesizer import synthesize_test_cases
from ....utils.ai_reviewer import review_api_collection
from sqlalchemy import select
from sqlalchemy import update

logger = get_logger(__name__)

# 初始化 Service 实例（与 v1 一致，路由只做参数处理与响应组装）
collection_service = ApiCollectionService()
case_service = ApiCaseService()
execution_service = ApiExecutionService()
ai_config_service = AiConfigService()

router = APIRouter(tags=["api-test"])


# ---------------------------------------------------------------------------
# 鉴权包装 / 响应构造 / 属主过滤
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


def _safe_error_msg(exc: Exception, prefix: str = "") -> str:
    """生产环境返回通用错误信息，不暴露内部异常详情（与 v1 一致）"""
    if os.environ.get("APP_ENV") == "production":
        return prefix or "服务器内部错误"
    return f"{prefix}: {exc}" if prefix else str(exc)


def _to_int(value: Any) -> Optional[int]:
    """body 内的 id 容错转 int（非法/缺失返回 None → 走 404 分支）"""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _accessible_project_ids(user_id: int) -> list:
    """用户可访问的项目 ID（自己创建的 + 所在组织的），等价 v1/reports 的访问域"""
    org_ids = [
        om.organization_id
        for om in db.session.scalars(select(OrganizationMember).filter_by(user_id=user_id, is_active=True)).all()]
    owned_ids = [p.id for p in db.session.scalars(select(Project).filter_by(owner_id=user_id)).all()]
    org_proj_ids = (
        [p.id for p in db.session.scalars(select(Project).filter(Project.organization_id.in_(org_ids))).all()]
        if org_ids
        else []
    )
    return list(set(owned_ids + org_proj_ids))


def _ensure_project_accessible(user_id: int, project_id: Any) -> bool:
    """指定 project_id 时校验其属于用户可访问域（IDOR 修复）"""
    pid = _to_int(project_id)
    if pid is None:
        return False
    return pid in _accessible_project_ids(user_id)


def _get_owned_case(user_id: int, case_id: Any) -> Optional[ApiTestCase]:
    """按 id 取用例并做属主过滤：不存在/越权/非法 id 一律 None → 404"""
    cid = _to_int(case_id)
    if cid is None:
        return None
    return db.session.scalar(select(ApiTestCase).filter_by(id=cid, user_id=user_id))


def _get_owned_collection(user_id: int, collection_id: Any) -> Optional[ApiTestCollection]:
    """按 id 取集合并做属主过滤：不存在/越权/非法 id 一律 None → 404"""
    cid = _to_int(collection_id)
    if cid is None:
        return None
    return db.session.scalar(select(ApiTestCollection).filter_by(id=cid, user_id=user_id))


def _get_owned_version(user_id: int, version_id: Any) -> Optional[TestCaseVersion]:
    """按 id 取版本并经 用例→user_id 做属主过滤：不存在/越权一律 None → 404"""
    vid = _to_int(version_id)
    if vid is None:
        return None
    version = db.session.get(TestCaseVersion, vid)
    if not version or version.case_type != "api":
        return None
    if not _get_owned_case(user_id, version.case_id):
        return None
    return version


def _build_ai_runtime_config(data: dict, *, timeout: int = 30) -> dict:
    """从 Flask config 和请求 data 中构建 AI runtime_config，支持前端 per-request 覆盖（与 v1 一致）"""
    from ....core.runtime import get_config

    runtime_config = {
        "AI_ASSISTANT_ENABLED": get_config().get("AI_ASSISTANT_ENABLED", True),
        "AI_ASSISTANT_BASE_URL": get_config().get("AI_ASSISTANT_BASE_URL", ""),
        "AI_ASSISTANT_API_KEY": get_config().get("AI_ASSISTANT_API_KEY", ""),
        "AI_ASSISTANT_MODEL": get_config().get("AI_ASSISTANT_MODEL", ""),
        "AI_VISION_BASE_URL": get_config().get("AI_VISION_BASE_URL", ""),
        "AI_VISION_API_KEY": get_config().get("AI_VISION_API_KEY", ""),
        "AI_VISION_MODEL": get_config().get("AI_VISION_MODEL", ""),
        "AI_ASSISTANT_TIMEOUT": get_config().get("AI_ASSISTANT_TIMEOUT", timeout),
    }
    # Frontend runtime override
    if data.get("base_url"):
        runtime_config["AI_ASSISTANT_BASE_URL"] = str(data.get("base_url")).strip()
    if data.get("model"):
        runtime_config["AI_ASSISTANT_MODEL"] = str(data.get("model")).strip()
    if data.get("api_key"):
        runtime_config["AI_ASSISTANT_API_KEY"] = str(data.get("api_key")).strip()
    if data.get("vision_base_url"):
        runtime_config["AI_VISION_BASE_URL"] = str(data.get("vision_base_url")).strip()
    if data.get("vision_model"):
        runtime_config["AI_VISION_MODEL"] = str(data.get("vision_model")).strip()
    if data.get("vision_api_key"):
        runtime_config["AI_VISION_API_KEY"] = str(data.get("vision_api_key")).strip()
    return runtime_config


# ==================== 健康检查 ====================

@router.get("/api/v1/api-test/health")
@release_session
def api_test_health():
    """接口测试模块健康检查（公开端点，v1 无 @jwt_required，保持公开）"""
    return _success(message="接口测试模块正常")


# ==================== AI 配置与能力 ====================

@router.get("/api/v1/api-test/ai/config")
@release_session
def get_ai_config(user: User = Depends(_current_user)):
    """Get the current global AI assistant configuration"""
    try:
        config = ai_config_service.get_config()
        return _success(data=config, message="AI configuration fetched")
    except Exception as exc:
        logger.error("get ai config failed", error=str(exc))
        return _error(500, _safe_error_msg(exc, "获取 AI 配置失败"))


@router.post("/api/v1/api-test/ai/config")
@release_session
def save_ai_config(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """保存全局 AI 助手配置"""
    try:
        result = ai_config_service.save_config(data)
        if not result["success"]:
            return _error(400, result["error"])
        return _success(data=result["data"], message="AI 配置已保存到 .env")
    except Exception as exc:
        logger.error("save ai config failed", error=str(exc))
        return _error(500, _safe_error_msg(exc, "保存 AI 配置失败"))


@router.post("/api/v1/api-test/ai/plan")
@release_session
def generate_ai_plan(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """Generate AI operations plan for API workspace."""
    user_id = user.id
    prompt = (data.get("prompt") or "").strip()

    if not prompt:
        return _error(400, "prompt is required")

    collections = db.session.scalars(
        filter_by_org_projects(select(ApiTestCollection), ApiTestCollection).filter_by(
            user_id=user_id
        )
    ).all()
    cases = db.session.scalars(
        filter_by_org_projects(select(ApiTestCase), ApiTestCase)
        .filter_by(user_id=user_id)
        .order_by(ApiTestCase.updated_at.desc())
        .limit(200)
    ).all()
    projects = db.session.scalars(filter_by_owner_or_org(select(Project), Project, user_id)).all()
    project_ids = [p.id for p in projects]
    envs = []
    if project_ids:
        envs = db.session.scalars(select(Environment).filter(Environment.project_id.in_(project_ids))).all()

    context = {
        "selected_collection_id": data.get("collection_id"),
        "selected_case_id": data.get("case_id"),
        "selected_env_id": data.get("environment_id"),
        "project_id": data.get("project_id"),
        "collections": [
            {"id": c.id, "name": c.name, "project_id": c.project_id}
            for c in collections
        ],
        "cases": [
            {
                "id": c.id,
                "name": c.name,
                "method": c.method,
                "url": c.url,
                "collection_id": c.collection_id,
                "environment_id": c.environment_id,
            }
            for c in cases
        ],
        "environments": [
            {
                "id": e.id,
                "name": e.name,
                "project_id": e.project_id,
                "base_url": e.base_url,
            }
            for e in envs
        ],
    }

    try:
        runtime_config = _build_ai_runtime_config(data)
        plan = generate_api_test_plan(prompt=prompt, context=context, config=runtime_config)
        return _success(data=plan, message="AI plan generated")
    except ValueError as exc:
        return _error(400, _safe_error_msg(exc))
    except Exception as exc:
        logger.error("AI plan generation failed", error=str(exc), exc_info=True)
        return _error(500, _safe_error_msg(exc, "AI plan generation failed"))


@router.post("/api/v1/api-test/ai/synthesize-cases")
@release_session
def synthesize_api_cases(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """AI 智能扩充接口测试用例"""
    base_request = data.get("base_request")
    count = data.get("count", 5)

    if not base_request:
        return _error(400, "base_request is required")

    try:
        runtime_config = _build_ai_runtime_config(data)
        cases = synthesize_test_cases(base_request, count, runtime_config)
        return _success(data={"cases": cases}, message="AI 用例扩充成功")
    except Exception as exc:
        return _error(500, _safe_error_msg(exc, "AI 用例扩充失败"))


@router.post("/api/v1/api-test/ai/review-collection")
@release_session
def review_collection_cases(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """AI 智能评审测试用例集合并补充用例（集合属主校验与 v1 一致）"""
    user_id = user.id
    collection_id = data.get("collection_id")

    if not collection_id:
        return _error(400, "collection_id is required")

    collection = db.session.scalar(select(ApiTestCollection).filter_by(id=collection_id, user_id=user_id))
    if not collection:
        return _error(404, "集合不存在")

    cases = db.session.scalars(select(ApiTestCase).filter_by(collection_id=collection_id, user_id=user_id)).all()
    if not cases:
        return _error(400, "该集合下没有测试用例，无法评审")

    case_list = []
    for c in cases:
        case_list.append({
            "name": c.name,
            "method": c.method,
            "url": c.url,
            "headers": c.headers,
            "params": c.params,
            "body": c.body,
            "body_type": c.body_type,
        })

    try:
        runtime_config = _build_ai_runtime_config(data, timeout=60)
        result = review_api_collection(collection.name, case_list, runtime_config)
        return _success(data=result, message="AI 评审完成")
    except Exception as exc:
        return _error(500, _safe_error_msg(exc, "AI 评审失败"))


# ==================== 用例集合 ====================

@router.get("/api/v1/api-test/collections")
@release_session
def get_collections(request: Request, user: User = Depends(_current_user)):
    """获取用例集合列表"""
    user_id = user.id
    project_id = query_int(request, "project_id", 0) or None
    try:
        data = collection_service.get_collections(user_id, project_id)
        return _success(data=data)
    except Exception as exc:
        logger.error("get collections failed", error=str(exc))
        return _error(500, _safe_error_msg(exc, "获取集合失败"))


@router.post("/api/v1/api-test/collections")
@release_session
def create_collection(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """创建用例集合"""
    user_id = user.id

    error = validate_required(data, ["name"])
    if error:
        return _error(400, error)

    try:
        result = collection_service.create_collection(
            user_id=user_id,
            name=data["name"],
            description=data.get("description", ""),
            project_id=data.get("project_id"),
        )
        return _success(data=result, message="创建成功")
    except Exception as exc:
        logger.error("create collection failed", error=str(exc))
        return _error(500, _safe_error_msg(exc, "创建集合失败"))


@router.put("/api/v1/api-test/collections/{collection_id}")
@release_session
def update_collection(
    collection_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """更新用例集合（service 层按 user_id 过滤，越权 404）"""
    user_id = user.id
    try:
        result = collection_service.update_collection(collection_id, user_id, data)
        return _success(data=result, message="更新成功")
    except NotFoundError as exc:
        return _error(404, _safe_error_msg(exc))
    except Exception as exc:
        logger.error("update collection failed", error=str(exc))
        return _error(500, _safe_error_msg(exc, "更新集合失败"))


@router.delete("/api/v1/api-test/collections/{collection_id}")
@release_session
def delete_collection(collection_id: int, user: User = Depends(_current_user)):
    """删除用例集合（service 层按 user_id 过滤，越权 404）"""
    user_id = user.id
    try:
        collection_service.delete_collection(collection_id, user_id)
        return _success(message="删除成功")
    except NotFoundError as exc:
        return _error(404, _safe_error_msg(exc))
    except Exception as exc:
        logger.error("delete collection failed", error=str(exc))
        return _error(500, _safe_error_msg(exc, "删除集合失败"))


# ==================== 测试用例 ====================

@router.get("/api/v1/api-test/cases")
@release_session
def get_cases(request: Request, user: User = Depends(_current_user)):
    """获取测试用例列表（支持多维筛选，service 层按 user_id 过滤）"""
    user_id = user.id
    collection_id = query_int(request, "collection_id", 0) or None
    project_id = query_int(request, "project_id", 0) or None
    method = query_str(request, "method") or None
    url_contains = query_str(request, "url_contains") or None
    tags = query_str(request, "tags") or None
    priority = query_str(request, "priority") or None
    try:
        data = case_service.get_cases(
            user_id, collection_id, project_id,
            method=method, url_contains=url_contains, tags=tags, priority=priority,
        )
        return _success(data=data)
    except Exception as exc:
        logger.error("get cases failed", error=str(exc))
        return _error(500, _safe_error_msg(exc, "获取用例失败"))


@router.post("/api/v1/api-test/cases")
@release_session
def create_case(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """创建测试用例"""
    user_id = user.id
    try:
        result = case_service.create_case(user_id, data)
        return _success(data=result, message="创建成功")
    except ValidationError as exc:
        return _error(400, _safe_error_msg(exc))
    except Exception as exc:
        logger.error("create case failed", error=str(exc))
        return _error(500, _safe_error_msg(exc, "创建用例失败"))


@router.get("/api/v1/api-test/cases/{case_id}")
@release_session
def get_case(case_id: int, user: User = Depends(_current_user)):
    """获取用例详情（service 层按 user_id 过滤，越权 404）"""
    user_id = user.id
    try:
        result = case_service.get_case(case_id, user_id)
        return _success(data=result)
    except NotFoundError as exc:
        return _error(404, _safe_error_msg(exc))
    except Exception as exc:
        logger.error("get case failed", error=str(exc))
        return _error(500, _safe_error_msg(exc, "获取用例失败"))


@router.put("/api/v1/api-test/cases/{case_id}")
@release_session
def update_case(
    case_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """更新测试用例（service 层按 user_id 过滤，越权 404）"""
    user_id = user.id
    try:
        result = case_service.update_case(case_id, user_id, data)
        return _success(data=result, message="更新成功")
    except NotFoundError as exc:
        return _error(404, _safe_error_msg(exc))
    except Exception as exc:
        logger.error("update case failed", error=str(exc))
        return _error(500, _safe_error_msg(exc, "更新用例失败"))


@router.delete("/api/v1/api-test/cases/{case_id}")
@release_session
def delete_case(case_id: int, user: User = Depends(_current_user)):
    """删除测试用例（service 层按 user_id 过滤，越权 404）"""
    user_id = user.id
    try:
        case_service.delete_case(case_id, user_id)
        return _success(message="删除成功")
    except NotFoundError as exc:
        return _error(404, _safe_error_msg(exc))
    except Exception as exc:
        logger.error("delete case failed", error=str(exc))
        return _error(500, _safe_error_msg(exc, "删除用例失败"))


# ==================== Mock Server ====================

@router.api_route(
    "/api/v1/api-test/mock/{case_id}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
@release_session
def mock_api_endpoint(case_id: int, request: Request):
    """
    Mock Server 端点（公开端点：v1 无鉴权，供前端/第三方直接调用的 Mock 服务，保持公开）

    根据用例 ID 返回预设的 Mock 数据，允许跨域，方便前端直接调用
    """
    origin = request.headers.get("Origin", "")

    # 处理跨域 OPTIONS 请求
    if request.method == "OPTIONS":
        return Response(
            status_code=200,
            headers={
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, PATCH, OPTIONS, HEAD",
                "Access-Control-Allow-Headers": "Content-Type, Authorization",
            },
        )

    case = db.session.get(ApiTestCase, case_id)

    if not case:
        return _error(404, "用例不存在")

    if not case.mock_enabled:
        return _error(400, "该用例未开启 Mock 功能")

    # 模拟延迟
    if case.mock_delay_ms and case.mock_delay_ms > 0:
        time.sleep(case.mock_delay_ms / 1000.0)

    # 设置响应头
    headers = {}
    if case.mock_response_headers:
        for k, v in case.mock_response_headers.items():
            headers[k] = v

    # 默认 Content-Type 为 application/json 如果未设置
    if "Content-Type" not in [k.title() for k in (case.mock_response_headers or {}).keys()]:
        headers.setdefault("Content-Type", "application/json")

    # 允许跨域
    headers["Access-Control-Allow-Origin"] = origin

    return Response(
        content=case.mock_response_body or "",
        status_code=case.mock_response_code or 200,
        headers=headers,
    )


# ==================== 导入导出 ====================

@router.post("/api/v1/api-test/import/postman")
@release_session
def import_postman(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """
    从 Postman Collection JSON 导入用例

    请求体:
        project_id: 项目 ID (必填)
        collection_id: 集合 ID (可选)
        content: JSON 字符串 (必填)
    """
    from ....services.import_export_service import import_from_postman_json

    user_id = user.id
    data = data or {}

    project_id = data.get("project_id")
    if not project_id:
        return _error(400, "缺少 project_id")

    # IDOR 修复：校验项目属于用户可访问域
    if not _ensure_project_accessible(user_id, project_id):
        return _error(404, "项目不存在")

    content = data.get("content", "")
    if not content:
        return _error(400, "缺少导入内容")

    try:
        results = import_from_postman_json(
            user_id=user_id,
            project_id=project_id,
            json_content=content,
            collection_id=data.get("collection_id"),
        )
        return _success(data=results, message=f'导入完成: {results["imported"]} 条用例')
    except AppError as e:
        return _error(e.code, e.message, errors=e.errors)


@router.post("/api/v1/api-test/import/csv")
@release_session
def import_csv(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """
    从 CSV 格式批量导入用例

    请求体:
        project_id: 项目 ID (必填)
        collection_id: 集合 ID (可选)
        content: CSV 文本 (必填)
    """
    from ....services.import_export_service import import_from_csv

    user_id = user.id
    data = data or {}

    project_id = data.get("project_id")
    if not project_id:
        return _error(400, "缺少 project_id")

    # IDOR 修复：校验项目属于用户可访问域
    if not _ensure_project_accessible(user_id, project_id):
        return _error(404, "项目不存在")

    content = data.get("content", "")
    if not content:
        return _error(400, "缺少导入内容")

    try:
        results = import_from_csv(
            user_id=user_id,
            project_id=project_id,
            csv_content=content,
            collection_id=data.get("collection_id"),
        )
        return _success(data=results, message=f'导入完成: {results["imported"]} 条用例')
    except AppError as e:
        return _error(e.code, e.message, errors=e.errors)


@router.get("/api/v1/api-test/import/template")
@release_session
def get_csv_template(user: User = Depends(_current_user)):
    """获取 CSV 导入模板"""
    from ....services.import_export_service import generate_csv_template

    template = generate_csv_template()
    return _success(data={"template": template})


# ==================== 执行测试 ====================

@router.post("/api/v1/api-test/execute")
@release_session
def execute_request(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """
    执行 HTTP 请求（快速测试）

    不保存用例，直接执行并返回结果
    支持环境配置的应用、前置脚本和后置断言

    IDOR 修复：body.case_id 指向他人用例 → 404
    SSRF 补齐：真实外发请求前用 validate_url_safety 校验 url（mock 模式不外呼，跳过路由级校验）
    """
    user_id = user.id
    data = data or {}

    error = validate_required(data, ["method", "url"])
    if error:
        return _error(400, error)

    # IDOR 修复：case_id 归属校验（v1 service 内 query.get 无属主过滤）
    mock_mode = bool(data.get("mock_enabled"))
    case_id = _to_int(data.get("case_id"))
    if case_id is not None:
        case = db.session.scalar(select(ApiTestCase).filter_by(id=case_id, user_id=user_id))
        if not case:
            return _error(404, "用例不存在")
        if case.mock_enabled:
            mock_mode = True

    # SSRF 补齐：mock 模式不发起真实请求，跳过路由级校验（service 发送前仍有 is_safe_url）
    if not mock_mode:
        safe, reason = validate_url_safety(str(data.get("url") or ""))
        if not safe:
            return _error(400, f"URL 安全校验失败: {reason}")

    try:
        result = execution_service.execute_request(data, user_id)
        # 保存响应历史
        if result.get("success"):
            try:
                history = ResponseHistory(
                    case_id=case_id,
                    user_id=user_id,
                    url=data.get("url", ""),
                    method=data.get("method", "GET").upper(),
                    status_code=result.get("status_code"),
                    response_time=result.get("response_time"),
                    response_size=result.get("response_size"),
                    request_headers=data.get("headers"),
                    response_headers=result.get("headers"),
                    response_body=result.get("body"),
                    environment_id=data.get("env_id"),
                )
                db.session.add(history)
                db.session.commit()
            except Exception:
                db.session.rollback()
        if result.get("success", True):
            return _success(data=result)
        else:
            return _error(
                result.get("status_code", 400),
                result.get("error", "请求执行失败"),
                errors=result.get("script_execution"),
            )
    except Exception as exc:
        logger.error("execute request failed", error=str(exc))
        return _error(500, _safe_error_msg(exc, "请求执行失败"))


@router.post("/api/v1/api-test/cases/{case_id}/run")
@release_session
def run_case(case_id: int, request: Request, user: User = Depends(_current_user)):
    """执行单个测试用例（支持前置脚本和后置断言，service 层按 user_id 过滤，越权 404）"""
    user_id = user.id
    env_id = query_int(request, "env_id", 0) or None

    try:
        result = execution_service.run_case(case_id, user_id, env_id)
        return _success(data=result)
    except NotFoundError as exc:
        return _error(404, _safe_error_msg(exc))
    except Exception as exc:
        logger.error("run case failed", error=str(exc))
        return _error(500, _safe_error_msg(exc, "执行用例失败"))


@router.post("/api/v1/api-test/collections/{collection_id}/run")
@release_session
def run_collection(
    collection_id: int,
    request: Request,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """批量执行集合中的所有用例，并生成测试报告

    P30-5: 支持 async=true 参数，后台执行并返回 run_id，前端可轮询 /runs/<id>/progress
    （service 层按 user_id 过滤集合，越权 404；与 v1 一致使用后台线程执行，不派发 Celery）
    """
    user_id = user.id
    data = data or {}
    env_id = data.get("env_id") if "env_id" in data else (query_int(request, "env_id", 0) or None)
    run_async = data.get("async", False)

    try:
        if run_async:
            # P30-5: 异步模式 — 先创建 run 记录，后台线程执行，立即返回 run_id
            run_id = execution_service.create_pending_run(collection_id, user_id, env_id)

            def _run_in_background():
                try:
                    execution_service.run_collection(collection_id, user_id, env_id, existing_run_id=run_id)
                except Exception as bg_exc:
                    logger.error("background collection run failed", run_id=run_id, error=str(bg_exc))

            threading.Thread(target=_run_in_background, daemon=True).start()
            return _success(data={"run_id": run_id, "status": "running"}, message="测试已在后台开始执行")
        else:
            result = execution_service.run_collection(collection_id, user_id, env_id)
            return _success(data=result, message="测试执行完成")
    except NotFoundError as exc:
        return _error(404, _safe_error_msg(exc))
    except ValidationError as exc:
        return _error(400, _safe_error_msg(exc))
    except PermissionError as exc:
        return _error(403, _safe_error_msg(exc))
    except Exception as exc:
        logger.error("run collection failed", error=str(exc), exc_info=True)
        return _error(500, _safe_error_msg(exc, "执行集合失败"))


@router.get("/api/v1/api-test/runs/{run_id}/progress")
@release_session
def get_run_progress(run_id: int, user: User = Depends(_current_user)):
    """获取测试执行进度（IDOR 修复：v1 无属主校验，补 run 属主过滤，越权/不存在 404）"""
    # IDOR 修复：执行记录必须属于当前用户
    run = db.session.scalar(select(TestRun).filter_by(id=run_id, triggered_user_id=user.id))
    if not run:
        return _error(404, "执行记录不存在")

    try:
        progress = execution_service.get_progress(run_id)
        if progress:
            return _success(data=progress)
        return _success(data={"current": 0, "total": 0, "passed": 0, "failed": 0, "status": "unknown"})
    except Exception as exc:
        return _error(500, _safe_error_msg(exc, "获取进度失败"))


# ==================== 用例版本历史 ====================

@router.get("/api/v1/api-test/cases/{case_id}/versions")
@release_session
def get_case_versions(case_id: int, request: Request, user: User = Depends(_current_user)):
    """
    获取用例的版本历史列表（IDOR 修复：v1 无属主校验，补用例属主过滤）

    查询参数:
        page: 页码 (默认 1)
        per_page: 每页数量 (默认 20)
    """
    # IDOR 修复：用例必须属于当前用户
    if not _get_owned_case(user.id, case_id):
        return _error(404, "用例不存在")

    page = query_int(request, "page", 1)
    per_page = query_int(request, "per_page", 20)

    try:
        result = case_service.get_versions(case_id, page, per_page)
        return _success(data=result)
    except AppError as e:
        return _error(e.code, e.message)


@router.get("/api/v1/api-test/versions/diff")
@release_session
def diff_versions(request: Request, user: User = Depends(_current_user)):
    """
    对比两个版本的差异（IDOR 修复：v1 无属主校验，补 版本→用例→user_id 过滤）

    查询参数:
        v1: 旧版本 ID (必填)
        v2: 新版本 ID (必填)

    注意：本路由必须注册在 /versions/{version_id} 之前，避免 "diff" 被路径参数吞掉
    """
    v1_id = query_int(request, "v1", 0)
    v2_id = query_int(request, "v2", 0)

    if not v1_id or not v2_id:
        return _error(400, "缺少 v1 或 v2 参数")

    # IDOR 修复：两个版本都必须属于当前用户的用例
    if not _get_owned_version(user.id, v1_id) or not _get_owned_version(user.id, v2_id):
        return _error(404, "版本不存在")

    try:
        result = case_service.diff_two_versions(v1_id, v2_id)
        return _success(data=result)
    except AppError as e:
        return _error(e.code, e.message)


@router.get("/api/v1/api-test/versions/{version_id}")
@release_session
def get_version_detail(version_id: int, user: User = Depends(_current_user)):
    """获取指定版本详情（IDOR 修复：v1 无属主校验，补 版本→用例→user_id 过滤）"""
    version = _get_owned_version(user.id, version_id)
    if not version:
        return _error(404, "版本不存在")
    return _success(data=version.to_dict())


# ==================== cURL 导入 ====================

def parse_curl(curl_command: str) -> dict:
    """
    解析 cURL 命令为结构化数据

    支持：
    - 多行 cURL（\\ 换行）
    - --data-raw、--data-binary、-d 等变体
    - --compressed 参数（忽略）
    - 单引号和双引号

    Args:
        curl_command: cURL 命令字符串

    Returns:
        dict: {method, url, headers, body}

    Raises:
        ValueError: 解析失败时返回具体错误位置
    """
    # 预处理：合并多行（去除 \ 换行）
    curl_command = curl_command.strip()
    if not curl_command:
        raise ValueError("cURL 命令为空")

    # 合续行：将 \ + 换行替换为空格
    curl_command = re.sub(r'\\\s*\n\s*', ' ', curl_command)

    # 去掉开头的 curl 命令
    if curl_command.startswith('curl '):
        curl_command = curl_command[5:]
    elif curl_command == 'curl':
        raise ValueError("cURL 命令缺少参数")

    # 使用 shlex 分词（正确处理引号）
    try:
        tokens = shlex.split(curl_command)
    except ValueError as e:
        raise ValueError(f"cURL 命令格式错误: {e}")

    method = 'GET'
    url = ''
    headers = {}
    body = ''
    data_parts = []

    i = 0
    while i < len(tokens):
        token = tokens[i]

        if token in ('-X', '--request'):
            i += 1
            if i >= len(tokens):
                raise ValueError(f"参数 {token} 缺少值")
            method = tokens[i].upper()

        elif token in ('-H', '--header'):
            i += 1
            if i >= len(tokens):
                raise ValueError(f"参数 {token} 缺少值")
            header_str = tokens[i]
            if ':' in header_str:
                key, value = header_str.split(':', 1)
                headers[key.strip()] = value.strip()

        elif token in ('-d', '--data', '--data-raw', '--data-binary', '--data-urlencode'):
            i += 1
            if i >= len(tokens):
                raise ValueError(f"参数 {token} 缺少值")
            data_parts.append(tokens[i])
            if method == 'GET':
                method = 'POST'

        elif token == '--compressed':
            pass  # 忽略

        elif token.startswith('-'):
            # 未知参数，跳过
            pass

        elif not url and not token.startswith('-'):
            url = token

        i += 1

    if data_parts:
        body = '&'.join(data_parts)

    if not url:
        raise ValueError("cURL 命令中未找到 URL")

    return {
        'method': method,
        'url': url,
        'headers': headers,
        'body': body,
    }


@router.post("/api/v1/api-test/import-curl")
@release_session
def import_curl(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """
    导入 cURL 命令

    请求体:
        curl: cURL 命令字符串（支持多行）

    返回:
        解析后的请求结构 {method, url, headers, body}
    """
    curl_command = (data or {}).get("curl", "")

    if not curl_command:
        return _error(400, "缺少 curl 参数")

    try:
        result = parse_curl(curl_command)
        return _success(data=result)
    except ValueError as e:
        return _error(400, f"cURL 解析失败: {e}")


# ==================== 场景编排 ====================

@router.post("/api/v1/api-test/execute-scenario")
@release_session
def execute_scenario(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """
    执行场景编排（多步骤链式请求）

    请求体:
        steps: 步骤定义列表
        env_id: 环境 ID（可选）
        base_url: 基础 URL（可选）
        variables: 初始变量（可选）

    返回:
        { total, passed, failed, duration, step_results, variables }

    SSRF 补齐：base_url 与各步骤最终 URL（变量替换 + base_url 拼接后）经
    validate_url_safety 校验（含未解析模板变量的 URL 留给 service 层发送前校验）
    """
    from ....services.scenario_executor import get_scenario_executor
    from ....utils.env_variables import replace_variables

    data = data or {}
    steps = data.get("steps", [])

    if not steps:
        return _error(400, "缺少步骤定义")

    # 获取环境变量
    env_vars = {}
    env_id = data.get("env_id")
    if env_id:
        env = db.session.scalar(select(Environment).filter_by(id=env_id))
        if env:
            env_vars = env.variables or {}

    # SSRF 补齐：路由级 URL 安全校验（复刻执行器的变量替换与 base_url 拼接逻辑）
    variables = dict(env_vars)
    variables.update(data.get("variables") or {})
    base_url = str(data.get("base_url") or "")
    try:
        resolved_base = replace_variables(base_url, variables) if base_url else ""
    except Exception:
        resolved_base = base_url
    if resolved_base:
        safe, reason = validate_url_safety(resolved_base)
        if not safe:
            return _error(400, f"base_url 安全校验失败: {reason}")
    for step in steps:
        if not isinstance(step, dict):
            continue
        raw_url = str(step.get("url") or "")
        if not raw_url or "{" in raw_url:
            # 未解析的模板变量无法静态校验，留给 service 层发送前 is_safe_url 兜底
            continue
        try:
            url = replace_variables(raw_url, variables)
        except Exception:
            url = raw_url
        if resolved_base and not url.startswith(("http://", "https://")):
            url = resolved_base.rstrip("/") + "/" + url.lstrip("/")
        safe, reason = validate_url_safety(url)
        if not safe:
            step_label = step.get("name") or step.get("id") or ""
            return _error(400, f"步骤 {step_label} URL 安全校验失败: {reason}")

    try:
        executor = get_scenario_executor()
        result = executor.execute_scenario(steps, {
            "env_vars": env_vars,
            "user_id": user.id,
            "base_url": data.get("base_url", ""),
            "variables": data.get("variables", {}),
        })
        return _success(data=result)
    except Exception as e:
        logger.error("场景执行失败", error=str(e))
        return _error(500, _safe_error_msg(e, "场景执行失败"))


# ==================== 响应历史 ====================

@router.get("/api/v1/api-test/history")
@release_session
def get_response_history(request: Request, user: User = Depends(_current_user)):
    """
    获取响应历史列表（沿用 v1 的 user_id 过滤）

    查询参数:
        case_id: 用例 ID（可选，不传则获取当前用户所有历史）
        limit: 数量限制（默认 50）
    """
    case_id = query_int(request, "case_id", 0) or None
    limit = query_int(request, "limit", 50)

    query = select(ResponseHistory).filter_by(user_id=user.id)
    if case_id:
        query = query.filter_by(case_id=case_id)

    histories = db.session.scalars(query.order_by(ResponseHistory.created_at.desc()).limit(limit)).all()
    return _success(data=[h.to_dict() for h in histories])


@router.post("/api/v1/api-test/history")
@release_session
def save_response_history(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """
    保存响应历史记录

    请求体: 响应历史数据
    """
    data = data or {}

    try:
        history = ResponseHistory(
            case_id=data.get("case_id"),
            user_id=user.id,
            url=data.get("url", ""),
            method=data.get("method", "GET"),
            status_code=data.get("status_code"),
            response_time=data.get("response_time"),
            response_size=data.get("response_size"),
            request_headers=data.get("request_headers"),
            request_body=data.get("request_body"),
            response_headers=data.get("response_headers"),
            response_body=data.get("response_body"),
            error=data.get("error"),
            environment_id=data.get("environment_id"),
        )
        db.session.add(history)
        db.session.commit()
        return _success(data=history.to_dict(), message="历史记录已保存", code=200)
    except Exception as e:
        db.session.rollback()
        return _error(500, _safe_error_msg(e, "保存失败"))


@router.get("/api/v1/api-test/history/trend")
@release_session
def get_response_history_trend(request: Request, user: User = Depends(_current_user)):
    """
    获取响应时间趋势数据（沿用 v1 的 user_id 过滤）

    查询参数:
        case_id: 用例 ID（必填）
        limit: 数据点数量（默认 30）

    注意：本路由必须注册在 /history/{history_id} 之前，避免 "trend" 被路径参数吞掉
    """
    case_id = query_int(request, "case_id", 0) or None
    limit = query_int(request, "limit", 30)

    if not case_id:
        return _error(400, "缺少 case_id 参数")

    histories = db.session.scalars(select(ResponseHistory).filter_by(
        user_id=user.id, case_id=case_id
    ).order_by(ResponseHistory.created_at.desc()).limit(limit)).all()

    trend = [{
        "timestamp": h.created_at.isoformat() if h.created_at else None,
        "response_time": h.response_time,
        "status_code": h.status_code,
    } for h in reversed(histories)]

    return _success(data=trend)


@router.get("/api/v1/api-test/history/{history_id}")
@release_session
def get_response_history_detail(history_id: int, user: User = Depends(_current_user)):
    """获取响应历史详情（沿用 v1 的 user_id 过滤，越权 404）"""
    history = db.session.scalar(select(ResponseHistory).filter_by(id=history_id, user_id=user.id))
    if not history:
        return _error(404, "历史记录不存在")

    return _success(data=history.to_detail_dict())


# ==================== 智能选测 ====================

@router.post("/api/v1/api-test/smart-select")
@release_session
def smart_test_select(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """智能测试选择 — 根据变更文件推荐测试用例（IDOR 修复：project_id 属主/组织校验）"""
    from ....services.ai.test_selector_service import get_test_selector_service

    data = data or {}
    changed_files = data.get("changed_files", [])
    if not changed_files:
        return _error(400, "请提供变更文件列表")

    project_id = data.get("project_id")
    tags = data.get("tags", [])
    max_cases = data.get("max_cases", 50)

    # IDOR 修复：v1 仅校验项目存在 + g.organization_id 隔离（403），
    # 平迁统一为可访问域校验（属主/组织成员），越权一律 404
    if project_id and not _ensure_project_accessible(user.id, project_id):
        return _error(404, "项目不存在")

    try:
        selector = get_test_selector_service()
        result = selector.select_tests(
            changed_files=changed_files,
            project_id=project_id,
            tags=tags if tags else None,
            max_cases=max_cases,
        )
        return _success(data=result, message="智能选测完成")
    except Exception as exc:
        logger.error("智能选测失败", error=str(exc))
        return _error(500, _safe_error_msg(exc, "智能选测失败"))


# ==================== AI 自愈 ====================

@router.post("/api/v1/api-test/heal-case")
@release_session
def heal_test_case(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """AI 用例自愈 — 为失败用例生成修复建议（IDOR 修复：v1 query.get 无属主校验，补用例属主过滤）"""
    from ....services.ai.healing_service import HealingService

    data = data or {}
    case_id = data.get("case_id")
    failure_info = data.get("failure_info", {})

    if not case_id:
        return _error(400, "缺少 case_id")

    # IDOR 修复：校验用例存在且属于当前用户（v1 仅校验存在，未校验属主）
    if not _get_owned_case(user.id, case_id):
        return _error(404, "用例不存在")

    try:
        service = HealingService()
        result = service.heal_case(case_id=case_id, failure_info=failure_info, user_id=user.id)
        return _success(data=result, message="AI 修复建议生成成功")
    except Exception as exc:
        logger.error("AI 自愈失败", case_id=case_id, error=str(exc))
        return _error(500, _safe_error_msg(exc, "AI 自愈失败"))


@router.post("/api/v1/api-test/apply-heal")
@release_session
def apply_heal_fix(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """应用 AI 自愈修复（IDOR 修复：v1 不校验用例存在性/属主，补用例属主过滤）"""
    from ....services.ai.healing_service import HealingService

    data = data or {}
    case_id = data.get("case_id")
    fixes = data.get("fixes", [])

    if not case_id:
        return _error(400, "缺少 case_id")
    if not fixes:
        return _error(400, "缺少修复项")

    # IDOR 修复：校验用例存在且属于当前用户（v1 直接进 service，越权可改他人用例）
    if not _get_owned_case(user.id, case_id):
        return _error(404, "用例不存在")

    try:
        service = HealingService()
        result = service.apply_fix(case_id=case_id, fixes=fixes, user_id=user.id)
        return _success(data=result, message="修复已应用")
    except Exception as exc:
        logger.error("应用修复失败", case_id=case_id, error=str(exc))
        return _error(500, f"应用修复失败: {str(exc)}")


# ==================== 标签 ====================

@router.get("/api/v1/api-test/tags/stats")
@release_session
def get_tag_stats(request: Request, user: User = Depends(_current_user)):
    """获取标签统计（IDOR 修复：指定 project_id 时校验可访问域，越权 404）"""
    from ....services.tag_manager_service import get_tag_manager_service

    project_id = query_int(request, "project_id", 0) or None

    # IDOR 修复：校验项目属于用户可访问域
    if project_id and not _ensure_project_accessible(user.id, project_id):
        return _error(404, "项目不存在")

    try:
        service = get_tag_manager_service()
        stats = service.get_tag_stats(project_id=project_id)
        return _success(data=stats)
    except Exception as exc:
        logger.error("获取标签统计失败", error=str(exc))
        return _error(500, f"获取标签统计失败: {str(exc)}")


@router.post("/api/v1/api-test/tags/filter")
@release_session
def filter_by_tags(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """按标签过滤用例（IDOR 修复：指定 project_id 时校验可访问域，越权 404）"""
    from ....services.tag_manager_service import get_tag_manager_service

    data = data or {}
    tags = data.get("tags", [])
    project_id = data.get("project_id")
    match_all = data.get("match_all", False)

    if not tags:
        return _error(400, "请提供标签列表")

    # IDOR 修复：校验项目属于用户可访问域
    if project_id and not _ensure_project_accessible(user.id, project_id):
        return _error(404, "项目不存在")

    try:
        service = get_tag_manager_service()
        cases = service.filter_by_tags(tags=tags, project_id=project_id, match_all=match_all)
        return _success(data=cases, message=f"找到 {len(cases)} 个匹配用例")
    except Exception as exc:
        logger.error("标签过滤失败", error=str(exc))
        return _error(500, f"标签过滤失败: {str(exc)}")


# ==================== Schema 校验 ====================

@router.post("/api/v1/api-test/validate-schema")
@release_session
def validate_response_schema(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """校验 API 响应是否符合 Schema"""
    from ....services.schema_validation_service import get_schema_validation_service

    data = data or {}
    schema = data.get("schema")
    response_body = data.get("response_body", "")
    status_code = data.get("status_code", 200)

    if not schema:
        return _error(400, "缺少 schema 定义")

    try:
        service = get_schema_validation_service()
        result = service.validate_response(
            schema=schema,
            response_body=response_body,
            status_code=status_code,
        )
        return _success(data=result)
    except Exception as exc:
        logger.error("Schema 校验失败", error=str(exc))
        return _error(500, f"Schema 校验失败: {str(exc)}")


@router.post("/api/v1/api-test/generate-schema")
@release_session
def generate_response_schema(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """从 API 响应自动生成 JSON Schema"""
    from ....services.schema_validation_service import get_schema_validation_service

    data = data or {}
    response_body = data.get("response_body", "")
    max_depth = data.get("max_depth", 5)

    try:
        service = get_schema_validation_service()
        schema = service.generate_schema_from_response(
            response_body=response_body,
            max_depth=max_depth,
        )
        return _success(data=schema, message="Schema 生成成功")
    except Exception as exc:
        logger.error("Schema 生成失败", error=str(exc))
        return _error(500, f"Schema 生成失败: {str(exc)}")


# ==================== HAR 导入 ====================

@router.post("/api/v1/api-test/import-har")
@release_session
def import_har(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """导入 HAR 文件生成测试用例（IDOR 修复：project_id 属主/组织校验）"""
    from ....services.har_import_service import get_har_import_service

    data = data or {}
    har_content = data.get("har_content", "")
    project_id = data.get("project_id")
    collection_id = data.get("collection_id")
    collection_name = data.get("collection_name", "HAR 导入")

    if not har_content:
        return _error(400, "缺少 HAR 内容")
    if not project_id:
        return _error(400, "缺少 project_id")

    # IDOR 修复：校验项目属于用户可访问域
    if not _ensure_project_accessible(user.id, project_id):
        return _error(404, "项目不存在")

    try:
        service = get_har_import_service()
        result = service.import_to_collection(
            har_content=har_content,
            project_id=project_id,
            collection_id=collection_id,
            collection_name=collection_name,
            user_id=user.id,
        )
        return _success(data=result, message=f'导入完成，共 {result["cases_count"]} 个用例')
    except ValidationError as exc:
        return _error(400, _safe_error_msg(exc))
    except Exception as exc:
        logger.error("HAR 导入失败", error=str(exc))
        return _error(500, f"HAR 导入失败: {str(exc)}")


@router.post("/api/v1/api-test/parse-har")
@release_session
def parse_har_preview(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """解析 HAR 文件预览（不导入）"""
    from ....services.har_import_service import get_har_import_service

    data = data or {}
    har_content = data.get("har_content", "")

    if not har_content:
        return _error(400, "缺少 HAR 内容")

    try:
        service = get_har_import_service()
        result = service.parse_har(har_content)
        return _success(data=result, message="HAR 解析成功")
    except ValidationError as exc:
        return _error(400, _safe_error_msg(exc))
    except Exception as exc:
        logger.error("HAR 解析失败", error=str(exc))
        return _error(500, f"HAR 解析失败: {str(exc)}")


# ==================== 变更检测 ====================

@router.post("/api/v1/api-test/detect-changes")
@release_session
def detect_api_changes(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """检测 API 响应结构变更（IDOR 修复：body.case_id 补用例属主过滤）"""
    from ....services.api_change_detection_service import get_api_change_detection_service

    data = data or {}
    case_id = data.get("case_id")
    response_body = data.get("response_body", "")
    status_code = data.get("status_code", 200)

    if not case_id:
        return _error(400, "缺少 case_id")

    # IDOR 修复：校验用例存在且属于当前用户
    if not _get_owned_case(user.id, case_id):
        return _error(404, "用例不存在")

    try:
        service = get_api_change_detection_service()
        result = service.detect_changes(
            case_id=case_id,
            response_body=response_body,
            status_code=status_code,
        )
        return _success(data=result)
    except Exception as exc:
        logger.error("变更检测失败", case_id=case_id, error=str(exc))
        return _error(500, f"变更检测失败: {str(exc)}")


# ==================== BDD ====================

@router.post("/api/v1/api-test/bdd/parse")
@release_session
def parse_bdd(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """解析 Gherkin/BDD 文本为测试用例结构（IDOR 修复：project_id 属主/组织校验）"""
    from ....services.bdd_parser_service import get_bdd_parser_service

    data = data or {}
    gherkin_text = data.get("gherkin", "")
    project_id = data.get("project_id")

    if not gherkin_text:
        return _error(400, "缺少 Gherkin 文本")

    # IDOR 修复：校验项目属于用户可访问域
    if project_id and not _ensure_project_accessible(user.id, project_id):
        return _error(404, "项目不存在")

    try:
        service = get_bdd_parser_service()
        result = service.convert_to_test_cases(gherkin_text, project_id=project_id)
        return _success(data=result, message=f'解析完成，共 {result["total"]} 个用例')
    except Exception as exc:
        logger.error("BDD 解析失败", error=str(exc))
        return _error(500, f"BDD 解析失败: {str(exc)}")


@router.post("/api/v1/api-test/bdd/import")
@release_session
def import_bdd(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """导入 Gherkin/BDD 文本为测试用例（IDOR 修复：project_id 属主/组织校验；补创建记录的 user_id）"""
    from ....services.bdd_parser_service import get_bdd_parser_service

    data = data or {}
    gherkin_text = data.get("gherkin", "")
    project_id = data.get("project_id")
    collection_id = data.get("collection_id")

    if not gherkin_text:
        return _error(400, "缺少 Gherkin 文本")
    if not project_id:
        return _error(400, "缺少 project_id")

    # IDOR 修复：校验项目属于用户可访问域
    if not _ensure_project_accessible(user.id, project_id):
        return _error(404, "项目不存在")

    # IDOR 修复：collection_id 归属校验（v1 未校验，可挂到他人集合）
    if collection_id and not _get_owned_collection(user.id, collection_id):
        return _error(404, "集合不存在")

    try:
        service = get_bdd_parser_service()
        result = service.convert_to_test_cases(gherkin_text, project_id=project_id)

        # 创建或使用已有集合
        # 修正：v1 创建 ApiTestCollection/ApiTestCase 时漏传 user_id（NOT NULL 列，
        # v1 实际必然 500），平迁时补 user_id=当前用户
        if not collection_id:
            collection = ApiTestCollection(
                name=result.get("feature_name", "BDD 导入")[:200],
                project_id=project_id,
                description=f'从 BDD/Gherkin 导入，共 {result["total"]} 个场景',
                user_id=user.id,
            )
            db.session.add(collection)
            db.session.flush()
            collection_id = collection.id

        created = 0
        for case_data in result.get("cases", []):
            case = ApiTestCase(
                name=case_data.get("name", "未命名")[:200],
                method=case_data.get("method", "GET"),
                url=case_data.get("url", ""),
                headers=case_data.get("headers", {}),
                body=case_data.get("body", ""),
                body_type=case_data.get("body_type", "json"),
                description=case_data.get("description", ""),
                assertions=case_data.get("assertions", []),
                collection_id=collection_id,
                project_id=project_id,
                tags=case_data.get("tags", []),
                user_id=user.id,
            )
            db.session.add(case)
            created += 1

        db.session.commit()

        return _success(
            data={"collection_id": collection_id, "cases_count": created},
            message=f"BDD 导入完成，共 {created} 个用例",
        )
    except Exception as exc:
        db.session.rollback()
        logger.error("BDD 导入失败", error=str(exc))
        return _error(500, f"BDD 导入失败: {str(exc)}")


# ==================== 成本估算 ====================

@router.get("/api/v1/api-test/collections/{collection_id}/estimate")
@release_session
def estimate_collection_cost(collection_id: int, user: User = Depends(_current_user)):
    """估算用例集执行成本（IDOR 修复：v1 无属主校验，补集合属主过滤）"""
    from ....services.test_cost_estimator import get_test_cost_estimator

    # IDOR 修复：校验集合存在且属于当前用户
    if not _get_owned_collection(user.id, collection_id):
        return _error(404, "集合不存在")

    try:
        estimator = get_test_cost_estimator()
        result = estimator.estimate_collection(collection_id)
        return _success(data=result)
    except Exception as exc:
        logger.error("成本估算失败", collection_id=collection_id, error=str(exc))
        return _error(500, f"成本估算失败: {str(exc)}")
