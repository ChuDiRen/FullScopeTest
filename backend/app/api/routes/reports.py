"""
测试报告模块 - FastAPI 平迁（自 app/api/reports.py）

路径/方法/状态码/响应字段与原 v1 完全一致（蓝图挂载于 /api/v1 前缀），
前端与 CI 脚本零改动。全部端点为同步 def，运行于 RequestContextMiddleware
push 的 app context 内，直接复用 数据库会话（app/database.py ContextVar 作用域） 与 services 层。

鉴权映射：
- @jwt_required() → Depends(_current_user)（内部复用 deps.get_current_user）
- 公开端点：GET /api/v1/reports/health（v1 即无鉴权，保持公开）

文件下载/导出映射：
- Flask send_file(BytesIO)/make_response → fastapi Response(content=..., media_type=...,
  headers={"Content-Disposition": ...})，Content-Type 与下载文件名与 v1 一致

IDOR 修复（越权一律 404）：
- GET/DELETE /api/v1/test-runs/{run_id}        ：v1 service 层无属主校验，补可访问项目过滤
- /api/v1/test-runs/{run_id}/export/{excel,csv,pdf}：v1 服务层直接按 run_id 取数，补可访问项目过滤
- GET /api/v1/reports/export/excel、trend、trend/stats、team-metrics：project_id 过滤类端点校验 project 属主
- 其余端点沿用 v1 的"自有项目 + 所属组织项目"访问域过滤（_accessible_project_ids）

会话生命周期（同步端点在 anyio worker 线程执行的泄漏防护）：
Flask-SQLAlchemy 3.1 的 scoped_session registry 是 ThreadLocalRegistry（threading.local），
同步端点/同步依赖在 worker 线程创建的 session 只能由该线程自己 remove()；
RequestContextMiddleware 在事件循环线程 pop app context 时回收不到它们，
NullPool 连接随之泄漏（测试中表现为 sqlite 文件句柄被占、临时库无法删除）。
- 鉴权依赖包装为 async _current_user：在事件循环线程执行 get_current_user，
  其 session 由 teardown_appcontext 正常回收（与 async 的 v2 原生端点一致）
- 同步端点统一加 @_release_session：视图返回（commit 已完成）后释放本线程 session
"""

import functools
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import delete as sql_delete
from sqlalchemy import func, or_

from app.api.v2.deps import get_current_user, json_body, query_int, query_str
from app.core.logging import get_logger
from app.extensions import db
from app.utils.org_filter import get_current_organization_id
from app.models.api_test_case import ApiTestCase
from app.models.organization import OrganizationMember
from app.models.perf_test_scenario import PerfTestScenario
from app.models.project import Project
from app.models.test_report import TestReport
from app.models.test_run import TestRun
from app.models.user import User
from app.models.web_test_script import WebTestScript
from app.services.report_service import ReportService
from app.utils.exceptions import AppError, NotFoundError
from sqlalchemy import select
from app.database import paginate
from sqlalchemy import update

# ==================== 测试报告 HTML 渲染（自旧蓝图 app/api/reports.py 原样迁入） ====================


def generate_html_report(test_run):
    """生成 HTML 格式的测试报告"""
    pass_rate = round(test_run.passed / test_run.total_cases * 100, 2) if test_run.total_cases > 0 else 0
    
    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>测试报告 - {test_run.test_object_name or test_run.id}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; background: #f5f5f5; padding: 20px; }}
        .container {{ max-width: 1200px; margin: 0 auto; }}
        .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; border-radius: 10px; margin-bottom: 20px; }}
        .header h1 {{ font-size: 24px; margin-bottom: 10px; }}
        .header p {{ opacity: 0.8; }}
        .card {{ background: white; border-radius: 10px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
        .card h2 {{ font-size: 18px; color: #333; margin-bottom: 15px; border-bottom: 2px solid #667eea; padding-bottom: 10px; }}
        .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 15px; }}
        .stat-item {{ text-align: center; padding: 15px; background: #f8f9fa; border-radius: 8px; }}
        .stat-value {{ font-size: 28px; font-weight: bold; color: #333; }}
        .stat-label {{ font-size: 14px; color: #666; margin-top: 5px; }}
        .passed {{ color: #52c41a; }}
        .failed {{ color: #ff4d4f; }}
        .progress-bar {{ height: 20px; background: #e9ecef; border-radius: 10px; overflow: hidden; margin: 15px 0; }}
        .progress-fill {{ height: 100%; background: linear-gradient(90deg, #52c41a, #73d13d); border-radius: 10px; transition: width 0.5s; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th, td {{ padding: 12px; text-align: left; border-bottom: 1px solid #eee; }}
        th {{ background: #f8f9fa; font-weight: 600; }}
        .status-success {{ color: #52c41a; }}
        .status-failed {{ color: #ff4d4f; }}
        .footer {{ text-align: center; padding: 20px; color: #999; font-size: 14px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📊 测试报告</h1>
            <p>{test_run.test_object_name or f'测试执行 #{test_run.id}'}</p>
        </div>
        
        <div class="card">
            <h2>📈 测试概览</h2>
            <div class="stats">
                <div class="stat-item">
                    <div class="stat-value">{test_run.total_cases}</div>
                    <div class="stat-label">总用例数</div>
                </div>
                <div class="stat-item">
                    <div class="stat-value passed">{test_run.passed}</div>
                    <div class="stat-label">通过</div>
                </div>
                <div class="stat-item">
                    <div class="stat-value failed">{test_run.failed}</div>
                    <div class="stat-label">失败</div>
                </div>
                <div class="stat-item">
                    <div class="stat-value">{test_run.skipped}</div>
                    <div class="stat-label">跳过</div>
                </div>
                <div class="stat-item">
                    <div class="stat-value">{pass_rate}%</div>
                    <div class="stat-label">通过率</div>
                </div>
                <div class="stat-item">
                    <div class="stat-value">{test_run.duration or 0:.2f}s</div>
                    <div class="stat-label">执行时间</div>
                </div>
            </div>
            <div class="progress-bar">
                <div class="progress-fill" style="width: {pass_rate}%"></div>
            </div>
        </div>
        
        <div class="card">
            <h2>📋 执行信息</h2>
            <table>
                <tr><td><strong>测试类型</strong></td><td>{test_run.test_type}</td></tr>
                <tr><td><strong>执行状态</strong></td><td class="status-{'success' if test_run.status == 'success' else 'failed'}">{test_run.status}</td></tr>
                <tr><td><strong>测试环境</strong></td><td>{test_run.environment_name or '-'}</td></tr>
                <tr><td><strong>触发方式</strong></td><td>{test_run.triggered_by}</td></tr>
                <tr><td><strong>开始时间</strong></td><td>{test_run.started_at or '-'}</td></tr>
                <tr><td><strong>结束时间</strong></td><td>{test_run.finished_at or '-'}</td></tr>
            </table>
        </div>
        
        <div class="footer">
            <p>由 大熊AI测试平台 自动化测试平台生成 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        </div>
    </div>
</body>
</html>'''
    
    return html


# ==================== 测试报告 API ====================


logger = get_logger(__name__)

# 初始化 Service（与 v1 一致，路由只做参数处理与响应组装）
report_service = ReportService()

router = APIRouter(tags=["reports"])


# ---------------------------------------------------------------------------
# 会话生命周期与鉴权包装
# ---------------------------------------------------------------------------

async def _current_user(request: Request) -> User:
    """鉴权依赖：在事件循环线程内执行同步 get_current_user，session 随 app context 回收"""
    return get_current_user(request)


def _release_session(fn):
    """同步端点包装：视图返回后释放本 worker 线程的 scoped session（防 NullPool 连接泄漏）"""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        finally:
            try:
                db.session.remove()
            except Exception:  # pragma: no cover
                pass
    return wrapper


# ---------------------------------------------------------------------------
# 响应构造（等价 app/utils/response.py 的 JSON 结构）
# ---------------------------------------------------------------------------

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


def _paginate(items, total: int, page: int, per_page: int, message: str = "success") -> JSONResponse:
    """等价 v1 paginate_response"""
    return JSONResponse(
        status_code=200,
        content={
            "code": 200,
            "message": message,
            "data": {
                "items": items,
                "pagination": {
                    "total": total,
                    "page": page,
                    "per_page": per_page,
                    "pages": (total + per_page - 1) // per_page,
                },
            },
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
        },
    )


# ---------------------------------------------------------------------------
# 属主过滤（IDOR 修复核心）
# ---------------------------------------------------------------------------

def _accessible_project_ids(user_id: int) -> list:
    """获取用户可访问的所有项目 ID（自己创建的 + 所在组织的），等价 v1 _get_accessible_project_ids"""
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


def _get_accessible_run(user_id: int, run_id: int) -> Optional[TestRun]:
    """按 id 取执行记录并做属主过滤：不存在/越权一律返回 None → 404"""
    return (
        db.session.scalar(select(TestRun).join(Project, TestRun.project_id == Project.id).filter(
            TestRun.id == run_id,
            Project.id.in_(_accessible_project_ids(user_id)),
        )))


def _ensure_project_accessible(user_id: int, project_id: Optional[int]) -> bool:
    """指定 project_id 时校验其属于用户可访问域"""
    if not project_id:
        return True
    return project_id in _accessible_project_ids(user_id)


# ==================== 测试执行记录 ====================

@router.get("/api/v1/test-runs")
@_release_session
def get_test_runs(request: Request, user: User = Depends(_current_user)):
    """
    获取测试执行记录列表

    查询参数:
        project_id: 项目 ID
        test_type: 测试类型 (api/web/performance)
        status: 状态 (pending/running/success/failed/cancelled)
        page: 页码
        per_page: 每页数量
    """
    project_id = query_int(request, "project_id", 0) or None
    test_type = query_str(request, "test_type") or None
    status = query_str(request, "status") or None
    page = query_int(request, "page", 1)
    per_page = query_int(request, "per_page", 20)

    try:
        result = report_service.get_test_runs(
            user.id, project_id, test_type, status, page, per_page
        )
        return _paginate(
            items=result["items"],
            total=result["total"],
            page=result["page"],
            per_page=result["per_page"],
        )
    except Exception as exc:
        logger.error("get test runs failed", error=str(exc))
        return _error(500, f"获取执行记录失败: {exc}")


@router.post("/api/v1/test-runs")
@_release_session
def create_test_run(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """创建测试执行记录"""
    project_id = data.get("project_id")
    if not project_id:
        return _error(400, "项目 ID 不能为空")

    # 验证项目权限（属主过滤）
    project = db.session.scalar(select(Project).filter_by(id=project_id, owner_id=user.id))
    if not project:
        return _error(404, "项目不存在")

    test_run = TestRun(
        project_id=project_id,
        test_type=data.get("test_type", "api"),
        test_object_id=data.get("test_object_id"),
        test_object_name=data.get("test_object_name"),
        status="pending",
        total_cases=data.get("total_cases", 0),
        environment_id=data.get("environment_id"),
        environment_name=data.get("environment_name"),
        triggered_by=data.get("triggered_by", "manual"),
        triggered_user_id=user.id,
    )

    db.session.add(test_run)
    db.session.commit()

    return _success(data=test_run.to_dict(), message="创建成功", code=200)


@router.get("/api/v1/test-runs/{run_id}")
@_release_session
def get_test_run(run_id: int, user: User = Depends(_current_user)):
    """获取测试执行记录详情（IDOR 修复：v1 service 层无属主校验）"""
    test_run = _get_accessible_run(user.id, run_id)
    if not test_run:
        return _error(404, "测试记录不存在")
    return _success(data=test_run.to_dict())


@router.put("/api/v1/test-runs/{run_id}")
@_release_session
def update_test_run(
    run_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """更新测试执行记录"""
    test_run = _get_accessible_run(user.id, run_id)
    if not test_run:
        return _error(404, "测试记录不存在")

    # 更新字段
    for field in ["status", "total_cases", "passed", "failed", "skipped", "error",
                  "duration", "started_at", "finished_at", "results", "report_path",
                  "allure_report_path", "error_message"]:
        if field in data:
            value = data[field]
            # 处理日期时间字段
            if field in ("started_at", "finished_at") and value:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            setattr(test_run, field, value)

    db.session.commit()

    return _success(data=test_run.to_dict(), message="更新成功")


@router.delete("/api/v1/test-runs/{run_id}")
@_release_session
def delete_test_run(run_id: int, user: User = Depends(_current_user)):
    """删除测试执行记录（IDOR 修复：v1 service 层无属主校验）"""
    if not _get_accessible_run(user.id, run_id):
        return _error(404, "测试记录不存在")

    try:
        report_service.delete_test_run(run_id)
        return _success(message="删除成功")
    except NotFoundError as exc:
        return _error(404, str(exc))
    except Exception as exc:
        logger.error("delete test run failed", error=str(exc))
        return _error(500, f"删除执行记录失败: {exc}")


# ==================== 报告统计 ====================

@router.get("/api/v1/reports/statistics")
@_release_session
def get_report_statistics(request: Request, user: User = Depends(_current_user)):
    """
    获取测试报告统计数据

    查询参数:
        project_id: 项目 ID (可选)
        days: 统计天数 (默认 7)
    """
    project_id = query_int(request, "project_id", 0) or None
    days = query_int(request, "days", 7)

    # 计算时间范围
    end_date = datetime.now(timezone.utc).replace(tzinfo=None)
    start_date = end_date - timedelta(days=days)

    # 构建基础查询（组织级过滤，而非仅项目所有者）
    org_id = get_current_organization_id()
    accessible_ids = _accessible_project_ids(user.id)

    base_query = (
        select(TestRun)
        .join(Project, TestRun.project_id == Project.id)
        .filter(TestRun.created_at >= start_date)
    )

    if org_id:
        base_query = base_query.filter(Project.organization_id == org_id)
    else:
        base_query = base_query.filter(Project.id.in_(accessible_ids))

    if project_id:
        base_query = base_query.filter(TestRun.project_id == project_id)

    # 总体统计
    total_runs = db.session.scalar(select(func.count()).select_from(base_query.subquery()))
    success_runs = db.session.scalar(
        select(func.count()).select_from(base_query.filter(TestRun.status == "success").subquery())
    )
    failed_runs = db.session.scalar(
        select(func.count()).select_from(base_query.filter(TestRun.status == "failed").subquery())
    )
    running_runs = db.session.scalar(
        select(func.count()).select_from(base_query.filter(TestRun.status == "running").subquery())
    )

    # 按测试类型统计
    type_stats = (
        select(
            TestRun.test_type,
            func.count(TestRun.id).label("count"),
            func.sum(TestRun.passed).label("passed"),
            func.sum(TestRun.failed).label("failed"),
        )
        .join(Project, TestRun.project_id == Project.id)
        .filter(TestRun.created_at >= start_date)
    )

    if org_id:
        type_stats = type_stats.filter(Project.organization_id == org_id)
    else:
        type_stats = type_stats.filter(Project.id.in_(accessible_ids))

    if project_id:
        type_stats = type_stats.filter(TestRun.project_id == project_id)

    type_stats = db.session.execute(type_stats.group_by(TestRun.test_type)).all()

    # 每日趋势统计
    daily_stats = (
        select(
            func.date(TestRun.created_at).label("date"),
            func.sum(TestRun.passed).label("passed"),
            func.sum(TestRun.failed).label("failed"),
            func.count(TestRun.id).label("total"),
        )
        .join(Project, TestRun.project_id == Project.id)
        .filter(TestRun.created_at >= start_date)
    )

    if org_id:
        daily_stats = daily_stats.filter(Project.organization_id == org_id)
    else:
        daily_stats = daily_stats.filter(Project.id.in_(accessible_ids))

    if project_id:
        daily_stats = daily_stats.filter(TestRun.project_id == project_id)

    daily_stats = db.session.execute(
        daily_stats.group_by(func.date(TestRun.created_at)).order_by(func.date(TestRun.created_at))
    ).all()

    # 构建完整的日期范围
    daily_stats_dict = {
        str(stat.date): {
            "passed": stat.passed or 0,
            "failed": stat.failed or 0,
            "total": stat.total or 0,
        }
        for stat in daily_stats
    }

    daily_trend = []
    for i in range(days - 1, -1, -1):
        date_str = (end_date - timedelta(days=i)).strftime("%Y-%m-%d")
        daily_trend.append({
            "date": date_str,
            "passed": daily_stats_dict.get(date_str, {}).get("passed", 0),
            "failed": daily_stats_dict.get(date_str, {}).get("failed", 0),
            "total": daily_stats_dict.get(date_str, {}).get("total", 0),
        })

    return _success(data={
        "summary": {
            "total_runs": total_runs,
            "success_runs": success_runs,
            "failed_runs": failed_runs,
            "running_runs": running_runs,
            "success_rate": round(success_runs / total_runs * 100, 2) if total_runs > 0 else 0,
        },
        "by_type": [
            {
                "type": stat.test_type,
                "count": stat.count,
                "passed": stat.passed or 0,
                "failed": stat.failed or 0,
            }
            for stat in type_stats
        ],
        "daily_trend": daily_trend,
    })


@router.get("/api/v1/reports/dashboard")
@_release_session
def get_dashboard_stats(request: Request, user: User = Depends(_current_user)):
    """
    获取仪表盘统计数据

    查询参数:
        project_id: 项目 ID (可选，指定后只统计该项目)
    """
    user_id = user.id
    project_id = query_int(request, "project_id", 0) or None

    # 获取用户可访问的所有项目 ID（自己创建的 + 所在组织的）
    org_ids = [
        om.organization_id
        for om in db.session.scalars(select(OrganizationMember).filter_by(user_id=user_id, is_active=True)).all()]
    owned_project_ids = [p.id for p in db.session.scalars(select(Project).filter_by(owner_id=user_id)).all()]
    org_project_ids = (
        [p.id for p in db.session.scalars(select(Project).filter(Project.organization_id.in_(org_ids))).all()]
        if org_ids
        else []
    )
    all_project_ids = list(set(owned_project_ids + org_project_ids))

    # 如果指定了 project_id，只统计该项目（需验证归属）
    if project_id:
        if project_id not in all_project_ids:
            return _error(403, "无权访问该项目")
        scope_filter = lambda model: model.project_id == project_id
        run_scope = TestRun.project_id == project_id
    else:
        scope_filter = lambda model: (
            or_(model.project_id.in_(all_project_ids), model.user_id == user_id)
            if all_project_ids
            else model.user_id == user_id
        )
        run_scope = (
            or_(TestRun.project_id.in_(all_project_ids), TestRun.triggered_user_id == user_id)
            if all_project_ids
            else TestRun.triggered_user_id == user_id
        )

    # API 测试统计
    # API 测试统计
    api_total = db.session.scalar(
        select(func.count()).select_from(select(ApiTestCase).filter(scope_filter(ApiTestCase)).subquery())
    )
    api_passed = db.session.scalar(
        select(func.count()).select_from(select(ApiTestCase).filter(
            scope_filter(ApiTestCase), ApiTestCase.last_status == "passed"
        ).subquery())
    )
    api_failed = db.session.scalar(
        select(func.count()).select_from(select(ApiTestCase).filter(
            scope_filter(ApiTestCase), ApiTestCase.last_status == "failed"
        ).subquery())
    )

    # Web 测试统计
    web_total = db.session.scalar(
        select(func.count()).select_from(select(WebTestScript).filter(scope_filter(WebTestScript)).subquery())
    )
    web_passed = db.session.scalar(
        select(func.count()).select_from(select(WebTestScript).filter(
            scope_filter(WebTestScript), WebTestScript.status == "passed"
        ).subquery())
    )
    web_failed = db.session.scalar(
        select(func.count()).select_from(select(WebTestScript).filter(
            scope_filter(WebTestScript), WebTestScript.status == "failed"
        ).subquery())
    )

    # 性能测试统计
    perf_total = db.session.scalar(
        select(func.count()).select_from(select(PerfTestScenario).filter(scope_filter(PerfTestScenario)).subquery())
    )
    perf_running = db.session.scalar(
        select(func.count()).select_from(select(PerfTestScenario).filter(
            scope_filter(PerfTestScenario), PerfTestScenario.status == "running"
        ).subquery())
    )

    # 最近执行记录
    recent_runs = db.session.scalars(
        select(TestRun).filter(run_scope).order_by(TestRun.created_at.desc()).limit(10)
    ).all()

    return _success(data={
        "api_tests": {"total": api_total, "passed": api_passed, "failed": api_failed},
        "web_tests": {"total": web_total, "passed": web_passed, "failed": web_failed},
        "perf_tests": {"total": perf_total, "running": perf_running},
        "recent_runs": [r.to_dict() for r in recent_runs],
    })


# ==================== 报告导出 ====================

@router.get("/api/v1/reports/{run_id}/export")
@_release_session
def export_report(run_id: int, request: Request, user: User = Depends(_current_user)):
    """
    导出测试报告

    查询参数:
        format: 导出格式 (json/html)
    """
    export_format = query_str(request, "format", "json") or "json"

    test_run = _get_accessible_run(user.id, run_id)
    if not test_run:
        return _error(404, "测试记录不存在")

    if export_format == "json":
        # 导出 JSON 格式
        report_data = {
            "report": test_run.to_dict(),
            "generated_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
            "generated_by": "大熊AI测试平台",
        }
        return _success(data=report_data)

    if export_format == "html":
        # 生成 HTML 报告（generate_html_report 已随旧蓝图删除原样迁入本模块）
        html_content = generate_html_report(test_run)
        download_name = f"report_{run_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        return Response(
            content=html_content,
            media_type="text/html",
            headers={"Content-Disposition": f"attachment; filename={download_name}"},
        )

    return _error(400, "不支持的导出格式")


# ==================== 测试报告 API ====================

@router.get("/api/v1/test-reports")
@_release_session
def get_test_reports(request: Request, user: User = Depends(_current_user)):
    """
    获取测试报告列表

    查询参数:
        project_id: 项目 ID
        test_type: 测试类型 (api/web/performance)
        page: 页码
        per_page: 每页数量
    """
    project_id = query_int(request, "project_id", 0) or None
    test_type = query_str(request, "test_type") or None
    page = query_int(request, "page", 1)
    per_page = query_int(request, "per_page", 20)

    # 构建查询
    query = select(TestReport).join(TestRun).join(Project)

    if project_id:
        query = query.filter(TestReport.project_id == project_id)

    if test_type:
        query = query.filter(TestReport.test_type == test_type)

    # 只查询用户有权限的项目（自己创建的 + 所在组织的）
    query = query.filter(Project.id.in_(_accessible_project_ids(user.id)))

    # 排序
    query = query.order_by(TestReport.created_at.desc())

    # 分页
    pagination = paginate(query, page=page, per_page=per_page)

    return _paginate(
        items=[report.to_dict() for report in pagination.items],
        total=pagination.total,
        page=page,
        per_page=per_page,
    )


@router.get("/api/v1/test-reports/{report_id}")
@_release_session
def get_test_report(report_id: int, user: User = Depends(_current_user)):
    """获取测试报告详情"""
    accessible_ids = _accessible_project_ids(user.id)
    report = db.session.scalar(
        select(TestReport).join(TestRun).join(Project)
        .filter(
            TestReport.id == report_id,
            Project.id.in_(accessible_ids),
        )
    )

    if not report:
        return _error(404, "报告不存在")

    return _success(data=report.to_detail_dict())


@router.get("/api/v1/test-reports/{report_id}/html")
@_release_session
def get_test_report_html(report_id: int, user: User = Depends(_current_user)):
    """获取测试报告 HTML"""
    accessible_ids = _accessible_project_ids(user.id)
    report = db.session.scalar(
        select(TestReport).join(TestRun).join(Project)
        .filter(
            TestReport.id == report_id,
            Project.id.in_(accessible_ids),
        )
    )

    if not report:
        return _error(404, "报告不存在")

    # 如果没有 HTML 报告，生成一个
    if not report.report_html:
        results = report.report_data.get("results", []) if report.report_data else []

        def _render_body(body, limit=2000):
            try:
                if isinstance(body, (dict, list)):
                    text = json.dumps(body, ensure_ascii=False, indent=2)
                else:
                    text = str(body) if body is not None else "-"
            except Exception:
                text = str(body) if body is not None else "-"
            return text if len(text) <= limit else text[:limit] + "..."

        def _render_attachments(attachments):
            if not attachments:
                return "-"
            lines = []
            for att in attachments:
                if not isinstance(att, dict):
                    lines.append(str(att))
                    continue
                name = att.get("name") or "attachment"
                att_type = att.get("type") or "text"
                lines.append(f"{name} ({att_type})")
            return "<br>".join(lines)

        results_rows = "".join([f'''
                <tr style="border-bottom:1px solid #f0f0f0;">
                    <td style="padding:10px 12px;font-size:13px;">{result.get('name', '')}</td>
                    <td style="padding:10px 12px;font-size:13px;color:{'#52c41a' if result.get('passed') else '#ff4d4f'};font-weight:600;">
                        {'✓ 通过' if result.get('passed') else '✗ 失败'}
                    </td>
                    <td style="padding:10px 12px;font-size:13px;">{result.get('status_code', '-')}</td>
                    <td style="padding:10px 12px;font-size:13px;">{result.get('response_time', 0)}</td>
                    <td style="padding:10px 12px;font-size:13px;max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{_render_body(result.get('response_body'))}</td>
                    <td style="padding:10px 12px;font-size:13px;color:#ff4d4f;">{result.get('error') or '-'}</td>
                </tr>
                ''' for result in results])

        # 计算成功率
        total = report.summary.get("total", 0)
        passed = report.summary.get("passed", 0)
        failed = report.summary.get("failed", 0)
        success_rate = round(passed / total * 100, 1) if total > 0 else 0
        duration = report.summary.get("duration", 0)

        # 全内联样式 HTML 报告模板（DOMPurify 安全）
        html = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>{report.title}</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;margin:0;padding:20px;background:#f0f2f5;">
<div style="max-width:1100px;margin:0 auto;background:#fff;padding:32px;border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,.08);">
<h1 style="font-size:22px;color:#1a1a1a;margin:0 0 24px;padding-bottom:16px;border-bottom:3px solid #52c41a;">{report.title}</h1>
<div style="display:grid;grid-template-columns:repeat(5,1fr);gap:16px;margin-bottom:28px;">
<div style="background:#f6ffed;padding:16px;border-radius:8px;border-left:4px solid #52c41a;">
<div style="font-size:12px;color:#8c8c8c;margin-bottom:6px;">总用例数</div>
<div style="font-size:28px;font-weight:700;color:#262626;">{total}</div></div>
<div style="background:#f6ffed;padding:16px;border-radius:8px;border-left:4px solid #52c41a;">
<div style="font-size:12px;color:#8c8c8c;margin-bottom:6px;">通过数</div>
<div style="font-size:28px;font-weight:700;color:#52c41a;">{passed}</div></div>
<div style="background:#fff2f0;padding:16px;border-radius:8px;border-left:4px solid #ff4d4f;">
<div style="font-size:12px;color:#8c8c8c;margin-bottom:6px;">失败数</div>
<div style="font-size:28px;font-weight:700;color:#ff4d4f;">{failed}</div></div>
<div style="background:#fff7e6;padding:16px;border-radius:8px;border-left:4px solid #faad14;">
<div style="font-size:12px;color:#8c8c8c;margin-bottom:6px;">成功率</div>
<div style="font-size:28px;font-weight:700;color:{'#52c41a' if success_rate >= 80 else '#faad14' if success_rate >= 60 else '#ff4d4f'};">{success_rate}%</div></div>
<div style="background:#f0f5ff;padding:16px;border-radius:8px;border-left:4px solid #1890ff;">
<div style="font-size:12px;color:#8c8c8c;margin-bottom:6px;">执行耗时</div>
<div style="font-size:28px;font-weight:700;color:#262626;">{duration}s</div></div>
</div>
<h2 style="font-size:16px;color:#1a1a1a;margin:0 0 12px;">测试结果详情</h2>
<table style="width:100%;border-collapse:collapse;margin-bottom:20px;">
<thead><tr style="background:#fafafa;">
<th style="padding:12px;text-align:left;border-bottom:1px solid #f0f0f0;font-size:13px;color:#8c8c8c;">用例名称</th>
<th style="padding:12px;text-align:left;border-bottom:1px solid #f0f0f0;font-size:13px;color:#8c8c8c;">状态</th>
<th style="padding:12px;text-align:left;border-bottom:1px solid #f0f0f0;font-size:13px;color:#8c8c8c;">状态码</th>
<th style="padding:12px;text-align:left;border-bottom:1px solid #f0f0f0;font-size:13px;color:#8c8c8c;">耗时(ms)</th>
<th style="padding:12px;text-align:left;border-bottom:1px solid #f0f0f0;font-size:13px;color:#8c8c8c;">响应数据</th>
<th style="padding:12px;text-align:left;border-bottom:1px solid #f0f0f0;font-size:13px;color:#8c8c8c;">错误/异常</th>
</tr></thead>
<tbody>{results_rows}</tbody>
</table>
<div style="margin-top:24px;padding:16px;background:#fafafa;border-radius:8px;">
<span style="font-size:12px;color:#8c8c8c;">生成时间: {report.created_at.strftime('%Y-%m-%d %H:%M:%S')}</span>
</div></div></body></html>"""
        return Response(content=html, status_code=200, media_type="text/html; charset=utf-8")

    return Response(
        content=report.report_html, status_code=200, media_type="text/html; charset=utf-8"
    )


@router.delete("/api/v1/test-reports/{report_id}")
@_release_session
def delete_test_report(report_id: int, user: User = Depends(_current_user)):
    """删除测试报告"""
    # 先检查权限（属主过滤）
    report = (
        db.session.scalar(select(TestReport).join(Project, TestReport.project_id == Project.id).filter(
            TestReport.id == report_id,
            Project.id.in_(_accessible_project_ids(user.id)),
        )))

    if not report:
        return _error(404, "报告不存在或无权访问")

    try:
        # 先清理关联 TestRun 的外键引用，避免悬空引用
        db.session.execute(
            update(TestRun)
            .filter(TestRun.report_id == report_id)
            .values(report_id=None)
        )

        # 使用原始 SQL DELETE，绕过 ORM 的关系处理
        stmt = sql_delete(TestReport).where(TestReport.id == report_id)
        db.session.execute(stmt)
        db.session.commit()

        return _success(message="删除成功")
    except Exception as e:
        db.session.rollback()
        return _error(500, f"删除失败: {e}")


# ==================== 报告导出 ====================

@router.get("/api/v1/test-runs/{run_id}/export/excel")
@_release_session
def export_test_run_excel(run_id: int, user: User = Depends(_current_user)):
    """导出测试执行报告为 Excel 格式（IDOR 修复：v1 服务层无属主校验）"""
    if not _get_accessible_run(user.id, run_id):
        return _error(404, "测试记录不存在")

    from app.services.import_export_service import export_test_report_excel

    try:
        excel_bytes = export_test_report_excel(run_id)
        if excel_bytes is None:
            return _error(500, "openpyxl 未安装，无法导出 Excel")
        return Response(
            content=excel_bytes,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename=test_report_{run_id}.xlsx"},
        )
    except AppError as e:
        return _error(e.code, e.message)


@router.get("/api/v1/test-runs/{run_id}/export/csv")
@_release_session
def export_test_run_csv(run_id: int, user: User = Depends(_current_user)):
    """导出测试执行报告为 CSV 格式（IDOR 修复：v1 服务层无属主校验）"""
    if not _get_accessible_run(user.id, run_id):
        return _error(404, "测试记录不存在")

    from app.services.import_export_service import export_test_report_csv

    try:
        csv_content = export_test_report_csv(run_id)
        return Response(
            content=csv_content,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename=test_report_{run_id}.csv"},
        )
    except AppError as e:
        return _error(e.code, e.message)


@router.get("/api/v1/test-runs/{run_id}/export/pdf")
@_release_session
def export_test_run_pdf(run_id: int, user: User = Depends(_current_user)):
    """导出测试执行报告为 PDF 格式（IDOR 修复：v1 服务层无属主校验）"""
    if not _get_accessible_run(user.id, run_id):
        return _error(404, "测试记录不存在")

    from app.services.export_service import generate_pdf_report

    try:
        pdf_bytes = generate_pdf_report(run_id)
        if pdf_bytes is None:
            return _error(500, "ReportLab 未安装，无法生成 PDF")
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename=test_report_{run_id}.pdf"},
        )
    except AppError as e:
        return _error(e.code, e.message)


@router.get("/api/v1/reports/export/excel")
@_release_session
def export_report_excel_range(request: Request, user: User = Depends(_current_user)):
    """
    按范围导出 Excel 报告

    查询参数:
        project_id: 项目 ID（可选）
        days: 时间范围天数（可选）
        test_type: 测试类型过滤（可选）
    """
    project_id = query_int(request, "project_id", 0) or None
    days = query_int(request, "days", 0) or None
    test_type = query_str(request, "test_type") or None

    # IDOR 修复：指定 project_id 时校验其属于用户可访问域
    if not _ensure_project_accessible(user.id, project_id):
        return _error(404, "项目不存在")

    from app.services.export_service import generate_enhanced_excel

    try:
        excel_bytes = generate_enhanced_excel(
            project_id=project_id, days=days, test_type=test_type,
        )
        if excel_bytes is None:
            return _error(500, "openpyxl 未安装")
        return Response(
            content=excel_bytes,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=test_report.xlsx"},
        )
    except AppError as e:
        return _error(e.code, e.message)


# ==================== 质量趋势分析 ====================

@router.get("/api/v1/reports/trend")
@_release_session
def get_quality_trend(request: Request, user: User = Depends(_current_user)):
    """
    获取质量趋势数据

    查询参数:
        project_id: 项目 ID（可选，不传则全组织）
        days: 时间范围天数（7/30/90，默认 30）
        granularity: 聚合粒度（day/week/month，默认 week）

    返回:
        [{date, api, web, perf, total_runs, total_passed, total_failed}]
    """
    from app.services.trend_service import get_pass_rate_trend

    project_id = query_int(request, "project_id", 0) or None
    days = query_int(request, "days", 30)
    granularity = query_str(request, "granularity", "week") or "week"

    if days not in (7, 30, 90):
        return _error(400, "days 只能为 7, 30 或 90")
    if granularity not in ("day", "week", "month"):
        return _error(400, "granularity 只能为 day, week, month")

    # IDOR 修复：指定 project_id 时校验其属于用户可访问域
    if not _ensure_project_accessible(user.id, project_id):
        return _error(404, "项目不存在")

    # 不指定项目时限定为可访问项目域，防止跨租户统计泄露
    scope_project_ids = None if project_id else _accessible_project_ids(user.id)

    try:
        trend = get_pass_rate_trend(project_id, days, granularity,
                                    project_ids=scope_project_ids)
        return _success(data=trend)
    except Exception as exc:
        logger.error("get quality trend failed", error=str(exc))
        return _error(500, f"获取趋势数据失败: {exc}")


@router.get("/api/v1/reports/trend/stats")
@_release_session
def get_trend_stats(request: Request, user: User = Depends(_current_user)):
    """
    获取趋势统计汇总数据

    查询参数:
        project_id: 项目 ID（可选）
        days: 统计范围天数（默认 30）

    返回:
        {period_days, total_runs, pass_rate, by_type, daily}
    """
    from app.services.trend_service import get_dashboard_stats

    project_id = query_int(request, "project_id", 0) or None
    days = query_int(request, "days", 30)

    # IDOR 修复：指定 project_id 时校验其属于用户可访问域
    if not _ensure_project_accessible(user.id, project_id):
        return _error(404, "项目不存在")

    # 不指定项目时限定为可访问项目域，防止跨租户统计泄露
    scope_project_ids = None if project_id else _accessible_project_ids(user.id)

    try:
        stats = get_dashboard_stats(project_id, days, project_ids=scope_project_ids)
        return _success(data=stats)
    except Exception as exc:
        logger.error("get trend stats failed", error=str(exc))
        return _error(500, f"获取趋势统计失败: {exc}")


# ==================== 团队效能度量 ====================

@router.get("/api/v1/reports/team-metrics")
@_release_session
def get_team_metrics(request: Request, user: User = Depends(_current_user)):
    """
    获取团队效能度量数据

    查询参数:
        project_id: 项目 ID（可选）
        days: 统计范围天数（默认 30）

    返回:
        {period_days, summary, members: [{user_id, username, cases_created, ...}]}
    """
    from app.services.team_metrics_service import get_team_metrics as _get_team_metrics

    project_id = query_int(request, "project_id", 0) or None
    days = query_int(request, "days", 30)

    # IDOR 修复：指定 project_id 时校验其属于用户可访问域
    if not _ensure_project_accessible(user.id, project_id):
        return _error(404, "项目不存在")

    # 组织隔离：无 project_id 时统计仅限当前用户所在组织，防止跨组织泄露
    from app.core.runtime import ctx as _ctx
    organization_id = _ctx.get_organization_id()

    # fail-closed：既无项目域也无组织上下文时，没有可统计的团队范围，
    # 直接返回空指标（不能回落到全局统计，否则跨租户泄露）
    if project_id is None and organization_id is None:
        from app.services.team_metrics_service import empty_team_metrics
        return _success(data=empty_team_metrics(days))

    try:
        metrics = _get_team_metrics(project_id, days, organization_id)
        return _success(data=metrics)
    except Exception as exc:
        logger.error("get team metrics failed", error=str(exc))
        return _error(500, f"获取团队效能数据失败: {exc}")


@router.get("/api/v1/reports/percentiles")
@_release_session
def get_response_percentiles(request: Request, user: User = Depends(_current_user)):
    """
    获取 API 响应时间分位数统计

    查询参数:
        project_id: 项目 ID（可选）
        days: 统计天数（默认 7）

    返回:
        {p50, p90, p95, p99, avg, min, max, total_requests}
    """
    project_id = query_int(request, "project_id", 0) or None
    days = query_int(request, "days", 7)

    end_date = datetime.now(timezone.utc).replace(tzinfo=None)
    start_date = end_date - timedelta(days=days)

    # 查询 API 测试执行结果（属主过滤）
    query = (
        select(TestRun)
        .join(Project, TestRun.project_id == Project.id)
        .filter(
            Project.id.in_(_accessible_project_ids(user.id)),
            TestRun.test_type == "api",
            TestRun.created_at >= start_date)
    )

    if project_id:
        query = query.filter(TestRun.project_id == project_id)

    runs = db.session.scalars(query).all()

    # 收集所有响应时间
    response_times = []
    for run in runs:
        if run.results and isinstance(run.results, list):
            for result in run.results:
                if isinstance(result, dict) and "response_time" in result:
                    rt = result["response_time"]
                    if isinstance(rt, (int, float)) and rt > 0:
                        response_times.append(rt)

    if not response_times:
        return _success(data={
            "p50": 0, "p90": 0, "p95": 0, "p99": 0,
            "avg": 0, "min": 0, "max": 0,
            "total_requests": 0,
        })

    response_times.sort()
    total = len(response_times)

    def percentile(data, p):
        k = (len(data) - 1) * (p / 100)
        f = int(k)
        c = f + 1 if f + 1 < len(data) else f
        d = k - f
        return data[f] + d * (data[c] - data[f])

    return _success(data={
        "p50": round(percentile(response_times, 50), 2),
        "p90": round(percentile(response_times, 90), 2),
        "p95": round(percentile(response_times, 95), 2),
        "p99": round(percentile(response_times, 99), 2),
        "avg": round(sum(response_times) / total, 2),
        "min": round(min(response_times), 2),
        "max": round(max(response_times), 2),
        "total_requests": total,
    })
