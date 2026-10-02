"""
测试计划模块 - FastAPI 平迁（自 app/api/test_plans.py，蓝图挂载前缀 /api/v1）

路径/方法/状态码/响应字段与原 v1 完全一致（/api/v1/test-plans/...、
/api/v1/test-plan-runs/...），前端与 CI 脚本零改动。全部端点为同步 def，
运行于 RequestContextMiddleware push 的 app context 内，直接复用
数据库会话（app/database.py ContextVar 作用域） 与 services 层（PlanService）。

鉴权映射：
- @jwt_required() → Depends(_current_user)（内部复用 deps.get_current_user）
- 公开端点：无（本模块 11 个端点在 v1 全部有 @jwt_required）
- @validate_json(...) → 路由内显式校验（缺少字段 → 400 '缺少必需字段: ...'）

IDOR 修复（v1 全部对象级操作无属主校验，平迁补齐，越权/不存在一律 404）：
- POST /api/v1/test-plans              ：补 project 可访问域校验（防向他人项目注入计划）
- GET  /api/v1/test-plans              ：补 project 可访问域校验（防枚举他人项目计划）
- GET/PUT/DELETE /api/v1/test-plans/{id}：补 TestPlan.user_id 属主过滤
- POST/GET /api/v1/test-plans/{id}/runs：补计划属主过滤
- GET  /api/v1/test-plan-runs/{id}、/{id}/case-results、/{id}/complete：
  补 轮次 → 计划 → user_id 属主过滤（越权 404）
- GET  /api/v1/test-plans/{id}/trend   ：补计划属主过滤
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.api.v2.deps import get_current_user, json_body, query_int, query_str, release_session
from app.core.logging import get_logger
from app.models.organization import OrganizationMember
from app.models.project import Project
from app.models.test_plan import TestPlan, TestPlanRun
from app.models.user import User
from app.services.plan_service import PlanService
from app.utils.exceptions import AppError
from sqlalchemy import select
from app.extensions import db

logger = get_logger(__name__)

# 初始化 Service 实例（与 v1 一致，路由只做参数处理与响应组装）
plan_service = PlanService()

router = APIRouter(tags=["test-plans"])


# ---------------------------------------------------------------------------
# 鉴权包装 / 响应构造 / 属主过滤
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


def _ensure_project_accessible(user_id: int, project_id: Any) -> bool:
    """指定 project_id 时校验其属于用户可访问域（自己创建的 + 所在组织的），越权 → False"""
    try:
        pid = int(project_id)
    except (TypeError, ValueError):
        return False
    org_ids = [
        om.organization_id
        for om in db.session.scalars(select(OrganizationMember).filter_by(user_id=user_id, is_active=True)).all()]
    owned = db.session.scalar(select(Project).filter_by(id=pid, owner_id=user_id))
    if owned:
        return True
    if org_ids:
        org_project = db.session.scalar(select(Project).filter(
            Project.id == pid, Project.organization_id.in_(org_ids)
        ))
        if org_project:
            return True
    return False


def _get_owned_plan(user_id: int, plan_id: int) -> Optional[TestPlan]:
    """按 id 取计划并做属主过滤：不存在/越权一律 None → 404（IDOR 修复）"""
    return db.session.scalar(select(TestPlan).filter_by(id=plan_id, user_id=user_id))


def _get_owned_run(user_id: int, run_id: int) -> Optional[TestPlanRun]:
    """按 id 取执行轮次并经 轮次→计划→user_id 做属主过滤：越权/不存在一律 None → 404"""
    run = db.session.scalar(select(TestPlanRun).filter_by(id=run_id))
    if not run:
        return None
    plan = db.session.scalar(select(TestPlan).filter_by(id=run.plan_id, user_id=user_id))
    if not plan:
        return None
    return run


def _ensure_json_fields(data: Dict[str, Any], required_fields: tuple):
    """等价 v1 @validate_json：空 body → 400 '请求体不能为空'，缺字段 → 400 '缺少必需字段: ...'"""
    if not data:
        return _error(400, "请求体不能为空")
    missing_fields = [field for field in required_fields if field not in data]
    if missing_fields:
        return _error(400, f'缺少必需字段: {", ".join(missing_fields)}')
    return None


# ==================== 计划 CRUD ====================

@router.post("/api/v1/test-plans")
@release_session
def create_test_plan(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """
    创建测试计划

    请求体:
        name: 计划名称 (必填)
        project_id: 项目 ID (必填)
        description: 描述
        include_cases: 用例列表 [{case_type, case_id}]
        tags: 标签
    """
    user_id = user.id
    data = data or {}

    err = _ensure_json_fields(data, ("name", "project_id"))
    if err:
        return err

    # IDOR 修复：校验项目属于用户可访问域（防向他人项目注入计划）
    if not _ensure_project_accessible(user_id, data["project_id"]):
        return _error(404, "项目不存在")

    try:
        plan = plan_service.create_plan(
            user_id=user_id,
            project_id=data["project_id"],
            name=data["name"],
            description=data.get("description", ""),
            include_cases=data.get("include_cases", []),
            tags=data.get("tags", []),
        )
        return _success(data=plan, message="测试计划创建成功", code=200)
    except AppError as e:
        return _error(e.code, e.message, errors=e.errors)


@router.get("/api/v1/test-plans")
@release_session
def list_test_plans(request: Request, user: User = Depends(_current_user)):
    """
    获取项目下的测试计划列表

    查询参数:
        project_id: 项目 ID (必填)
        page: 页码
        per_page: 每页数量
        status: 状态过滤
    """
    project_id = query_int(request, "project_id", 0)
    if not project_id:
        return _error(400, "缺少 project_id 参数")

    # IDOR 修复：校验项目属于用户可访问域（防枚举他人项目计划）
    if not _ensure_project_accessible(user.id, project_id):
        return _error(404, "项目不存在")

    page = query_int(request, "page", 1)
    per_page = query_int(request, "per_page", 20)
    status = query_str(request, "status") or None

    try:
        result = plan_service.get_plans(project_id, page, per_page, status)
        return _success(data=result)
    except AppError as e:
        return _error(e.code, e.message)


@router.get("/api/v1/test-plans/{plan_id}")
@release_session
def get_test_plan(plan_id: int, user: User = Depends(_current_user)):
    """获取测试计划详情（包含最近轮次；IDOR 修复：属主过滤，越权 404）"""
    # IDOR 修复：v1 无属主校验
    if not _get_owned_plan(user.id, plan_id):
        return _error(404, "测试计划不存在")

    try:
        plan = plan_service.get_plan(plan_id)
        return _success(data=plan)
    except AppError as e:
        return _error(e.code, e.message)


@router.put("/api/v1/test-plans/{plan_id}")
@release_session
def update_test_plan(
    plan_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """
    更新测试计划

    请求体（均可选）:
        name, description, include_cases, tags, status
    """
    # IDOR 修复：v1 无属主校验
    if not _get_owned_plan(user.id, plan_id):
        return _error(404, "测试计划不存在")

    data = data or {}
    try:
        plan = plan_service.update_plan(plan_id, **data)
        return _success(data=plan, message="更新成功")
    except AppError as e:
        return _error(e.code, e.message)


@router.delete("/api/v1/test-plans/{plan_id}")
@release_session
def delete_test_plan(plan_id: int, user: User = Depends(_current_user)):
    """删除测试计划（IDOR 修复：属主过滤，越权 404）"""
    # IDOR 修复：v1 无属主校验
    if not _get_owned_plan(user.id, plan_id):
        return _error(404, "测试计划不存在")

    try:
        plan_service.delete_plan(plan_id)
        return _success(message="测试计划已删除")
    except AppError as e:
        return _error(e.code, e.message)


# ==================== 执行轮次 ====================

@router.post("/api/v1/test-plans/{plan_id}/runs")
@release_session
def create_test_plan_run(
    plan_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """
    创建执行轮次

    请求体:
        environment_id: 环境 ID (可选)
        environment_name: 环境名称 (可选)
        notes: 备注 (可选)
    """
    user_id = user.id
    data = data or {}

    # IDOR 修复：v1 无计划属主校验
    if not _get_owned_plan(user_id, plan_id):
        return _error(404, "测试计划不存在")

    try:
        run = plan_service.create_run(
            plan_id=plan_id,
            user_id=user_id,
            environment_id=data.get("environment_id"),
            environment_name=data.get("environment_name", ""),
            notes=data.get("notes", ""),
        )
        return _success(data=run, message="执行轮次已创建", code=200)
    except AppError as e:
        return _error(e.code, e.message)


@router.get("/api/v1/test-plans/{plan_id}/runs")
@release_session
def list_test_plan_runs(plan_id: int, request: Request, user: User = Depends(_current_user)):
    """获取计划的执行轮次列表（IDOR 修复：属主过滤，越权 404）"""
    # IDOR 修复：v1 无属主校验
    if not _get_owned_plan(user.id, plan_id):
        return _error(404, "测试计划不存在")

    page = query_int(request, "page", 1)
    per_page = query_int(request, "per_page", 20)

    try:
        result = plan_service.get_runs(plan_id, page, per_page)
        return _success(data=result)
    except AppError as e:
        return _error(e.code, e.message)


@router.get("/api/v1/test-plan-runs/{run_id}")
@release_session
def get_test_plan_run(run_id: int, user: User = Depends(_current_user)):
    """获取执行轮次详情（包含用例结果；IDOR 修复：轮次→计划→user_id 过滤，越权 404）"""
    # IDOR 修复：v1 无属主校验
    if not _get_owned_run(user.id, run_id):
        return _error(404, "执行轮次不存在")

    try:
        run = plan_service.get_run(run_id)
        return _success(data=run)
    except AppError as e:
        return _error(e.code, e.message)


@router.patch("/api/v1/test-plan-runs/{run_id}/case-results")
@release_session
def update_case_result(
    run_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """
    更新用例执行结果

    请求体:
        case_type: 用例类型 (必填)
        case_id: 用例 ID (必填)
        status: 状态 passed/failed/skipped/error (必填)
        duration: 执行耗时
        error_message: 错误信息
        result_detail: 详细结果
        test_run_id: 关联的 TestRun ID
    """
    data = data or {}

    err = _ensure_json_fields(data, ("case_type", "case_id", "status"))
    if err:
        return err

    # IDOR 修复：v1 无轮次属主校验
    if not _get_owned_run(user.id, run_id):
        return _error(404, "执行轮次不存在")

    try:
        result = plan_service.update_case_result(
            run_id=run_id,
            case_type=data["case_type"],
            case_id=data["case_id"],
            status=data["status"],
            duration=data.get("duration"),
            error_message=data.get("error_message"),
            result_detail=data.get("result_detail"),
            test_run_id=data.get("test_run_id"),
        )
        return _success(data=result, message="结果已更新")
    except AppError as e:
        return _error(e.code, e.message)


@router.post("/api/v1/test-plan-runs/{run_id}/complete")
@release_session
def complete_test_plan_run(run_id: int, user: User = Depends(_current_user)):
    """标记执行轮次完成（IDOR 修复：轮次→计划→user_id 过滤，越权 404）"""
    # IDOR 修复：v1 无属主校验
    if not _get_owned_run(user.id, run_id):
        return _error(404, "执行轮次不存在")

    try:
        run = plan_service.complete_run(run_id)
        return _success(data=run, message="轮次已完成")
    except AppError as e:
        return _error(e.code, e.message)


# ==================== 趋势查询 ====================

@router.get("/api/v1/test-plans/{plan_id}/trend")
@release_session
def get_pass_rate_trend(plan_id: int, request: Request, user: User = Depends(_current_user)):
    """
    获取通过率趋势

    查询参数:
        limit: 返回的轮次数 (默认 20)
    """
    # IDOR 修复：v1 无属主校验
    if not _get_owned_plan(user.id, plan_id):
        return _error(404, "测试计划不存在")

    limit = query_int(request, "limit", 20)

    try:
        trend = plan_service.get_pass_rate_trend(plan_id, limit)
        return _success(data=trend)
    except AppError as e:
        return _error(e.code, e.message)
