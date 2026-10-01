"""
质量门禁模块 - FastAPI 平迁（自 app/api/quality_gates.py，蓝图挂载前缀 /api/v1）

路径/方法/状态码/响应字段与原 v1 完全一致（/api/v1/quality-gates/...），
前端与 CI 脚本零改动。全部端点为同步 def，运行于 RequestContextMiddleware
push 的 app context 内，直接复用 数据库会话（app/database.py ContextVar 作用域） 与 services 层。

鉴权映射：
- @jwt_required() → Depends(_current_user)（内部复用 deps.get_current_user）
- 公开端点：无（本模块 7 个端点在 v1 全部有 @jwt_required）

IDOR / 权限（沿用 v1 的组织隔离设计，越权/不存在一律 404）：
- GET    /api/v1/quality-gates              ：filter_by_org_projects 按"当前组织下的项目"隔离列表
- GET/PUT/DELETE /api/v1/quality-gates/{id}：
  GET/POST /api/v1/quality-gates/{id}/evaluate、GET /{id}/evaluations
  → _get_gate_with_permission：门禁的项目必须属于当前用户（filter_by_owner_or_org），
  否则记 IDOR 日志并返回 404
- POST   /api/v1/quality-gates              ：与 v1 一致不做 project 归属校验
  （门禁按 created_by 落库；若 project 不可访问，后续读取/评估时会被组织隔离拦为 404）

评估同步 GitHub Check Run：与 v1 一致走 services.github_check_service，
外部调用在测试中 mock，路由本身零外呼。

其他必要修正：
- GET /api/v1/quality-gates/{id}/evaluations：v1 源文件使用 QualityGateEvaluation
  但未 import（NameError → v1 实际必然 500），平迁时补 import 使端点可用，
  响应结构（items/total/page/per_page 分页）与 v1 源码意图一致
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..deps import get_current_user, json_body, query_int, release_session
from ....core.logging import get_logger
from ....extensions import db
from ....models.project import Project
from ....models.quality_gate import QualityGate, QualityGateEvaluation
from ....models.test_run import TestRun
from ....models.user import User
from ....utils.org_filter import filter_by_org_projects, filter_by_owner_or_org
from sqlalchemy import select

logger = get_logger(__name__)

router = APIRouter(tags=["quality-gates"])


# ---------------------------------------------------------------------------
# 鉴权包装 / 响应构造 / 组织隔离
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


def _get_gate_with_permission(gate_id: int, user_id: int) -> tuple:
    """获取质量门禁并验证用户权限（通过组织隔离），返回 (gate, None) 或 (None, error)

    与 v1 一致：门禁的项目必须属于当前用户（filter_by_owner_or_org），
    否则记 IDOR 日志并返回 404。
    """
    gate = db.session.get(QualityGate, gate_id)
    if not gate:
        return None, _error(404, "质量门禁不存在")
    # 验证项目属于当前组织/属主
    query = filter_by_owner_or_org(select(Project), Project, user_id)
    project = db.session.scalar(query.filter_by(id=gate.project_id))
    if not project:
        logger.warning(
            "IDOR attempt blocked on quality_gate", user_id=user_id, gate_id=gate_id
        )
        return None, _error(404, "质量门禁不存在")
    return gate, None


# ==================== 质量门禁 CRUD ====================

@router.get("/api/v1/quality-gates")
@release_session
def get_quality_gates(request: Request, user: User = Depends(_current_user)):
    """获取质量门禁列表（组织隔离：仅当前组织/属主项目下的门禁）"""
    project_id = query_int(request, "project_id", 0) or None

    query = select(QualityGate)
    # 组织隔离
    query = filter_by_org_projects(query, QualityGate, "project_id")
    if project_id:
        query = query.filter_by(project_id=project_id)

    gates = db.session.scalars(query.order_by(QualityGate.created_at.desc())).all()
    return _success(data=[g.to_dict() for g in gates])


@router.post("/api/v1/quality-gates")
@release_session
def create_quality_gate(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """创建质量门禁规则（与 v1 一致：不做 project 归属校验，created_by=当前用户）"""
    user_id = user.id
    data = data or {}

    name = data.get("name")
    project_id = data.get("project_id")
    if not name or not project_id:
        return _error(400, "name and project_id are required")

    gate = QualityGate(
        project_id=project_id,
        name=name,
        description=data.get("description", ""),
        is_active=data.get("is_active", True),
        min_pass_rate=data.get("min_pass_rate", 100.0),
        max_p95_response_time=data.get("max_p95_response_time"),
        max_visual_diff_percentage=data.get("max_visual_diff_percentage"),
        created_by=user_id,
    )

    db.session.add(gate)
    db.session.commit()

    return _success(data=gate.to_dict(), message="质量门禁创建成功")


@router.get("/api/v1/quality-gates/{gate_id}")
@release_session
def get_quality_gate(gate_id: int, user: User = Depends(_current_user)):
    """获取质量门禁详情（组织隔离，越权 404）"""
    user_id = user.id
    gate, err = _get_gate_with_permission(gate_id, user_id)
    if err:
        return err
    return _success(data=gate.to_dict())


@router.put("/api/v1/quality-gates/{gate_id}")
@release_session
def update_quality_gate(
    gate_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """更新质量门禁规则（组织隔离，越权 404）"""
    user_id = user.id
    gate, err = _get_gate_with_permission(gate_id, user_id)
    if err:
        return err

    data = data or {}

    for field in [
        "name",
        "description",
        "is_active",
        "min_pass_rate",
        "max_p95_response_time",
        "max_visual_diff_percentage",
    ]:
        if field in data:
            setattr(gate, field, data[field])

    db.session.commit()
    return _success(data=gate.to_dict(), message="质量门禁更新成功")


@router.delete("/api/v1/quality-gates/{gate_id}")
@release_session
def delete_quality_gate(gate_id: int, user: User = Depends(_current_user)):
    """删除质量门禁规则（组织隔离，越权 404）"""
    user_id = user.id
    gate, err = _get_gate_with_permission(gate_id, user_id)
    if err:
        return err

    db.session.delete(gate)
    db.session.commit()
    return _success(message="质量门禁删除成功")


# ==================== 评估 ====================

@router.post("/api/v1/quality-gates/{gate_id}/evaluate")
@release_session
def evaluate_quality_gate(
    gate_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """评估质量门禁（组织隔离，越权 404；GitHub Check Run 同步走 service，测试中 mock）"""
    user_id = user.id
    gate, err = _get_gate_with_permission(gate_id, user_id)
    if err:
        return err

    data = data or {}
    test_run_id = data.get("test_run_id")
    if not test_run_id:
        return _error(400, "test_run_id is required")

    test_run = db.session.get(TestRun, test_run_id)
    if not test_run:
        return _error(404, "测试运行记录不存在")

    evaluation_details: Dict[str, Any] = {}
    passed = True

    # 检查通过率
    if gate.min_pass_rate is not None and test_run.total_cases > 0:
        pass_rate = (test_run.passed / test_run.total_cases) * 100
        evaluation_details["pass_rate"] = {
            "threshold": gate.min_pass_rate,
            "actual": round(pass_rate, 2),
            "passed": pass_rate >= gate.min_pass_rate,
        }
        if pass_rate < gate.min_pass_rate:
            passed = False

    # 检查 P95 响应时间
    if gate.max_p95_response_time is not None:
        p95 = None
        if test_run.results and isinstance(test_run.results, dict):
            p95 = test_run.results.get("p95_response_time")
        elif test_run.results and isinstance(test_run.results, list):
            for r in test_run.results:
                if isinstance(r, dict) and "p95_response_time" in r:
                    p95 = r["p95_response_time"]
                    break

        if p95 is not None:
            evaluation_details["p95_response_time"] = {
                "threshold": gate.max_p95_response_time,
                "actual": p95,
                "passed": p95 <= gate.max_p95_response_time,
            }
            if p95 > gate.max_p95_response_time:
                passed = False

    # 检查视觉差异
    if gate.max_visual_diff_percentage is not None:
        visual_diff = None
        if test_run.results and isinstance(test_run.results, dict):
            visual_diff = test_run.results.get("visual_diff_percentage")
        elif test_run.results and isinstance(test_run.results, list):
            for r in test_run.results:
                if isinstance(r, dict) and "visual_diff_percentage" in r:
                    visual_diff = r["visual_diff_percentage"]
                    break

        if visual_diff is not None:
            evaluation_details["visual_diff"] = {
                "threshold": gate.max_visual_diff_percentage,
                "actual": visual_diff,
                "passed": visual_diff <= gate.max_visual_diff_percentage,
            }
            if visual_diff > gate.max_visual_diff_percentage:
                passed = False

    if not evaluation_details:
        evaluation_details["note"] = "No checks configured"

    # 同步到 GitHub Check Run（与 v1 一致：可选、失败仅记日志不影响评估结果）
    if data.get("github_check_run_id"):
        try:
            from ....services.github_check_service import create_check_service
            from ....models.github_integration import GitHubIntegration

            integration = (
                db.session.scalar(select(GitHubIntegration).filter_by(
                    user_id=gate.created_by, is_active=True
                )))
            if integration:
                service = create_check_service(integration)
                conclusion = "success" if passed else "failure"
                status_text = "PASSED" if passed else "FAILED"

                summary = f"Quality Gate: {gate.name}\n\n"
                summary += f"Overall Status: {status_text}\n\n"
                for check_name, details in evaluation_details.items():
                    check_status = "PASS" if details.get("passed") else "FAIL"
                    summary += (
                        f'- {check_status} {check_name}: {details.get("actual")} '
                        f'(threshold: {details.get("threshold")})\n'
                    )

                service.update_check_run(
                    repo_full_name="",
                    check_run_id=data.get("github_check_run_id"),
                    status="completed",
                    conclusion=conclusion,
                    output_title=f"Quality Gate {status_text}",
                    output_summary=summary,
                )
        except Exception as e:
            logger.error(f"Failed to sync to GitHub Check Run: {e}")

    return _success(
        data={
            "passed": passed,
            "details": evaluation_details,
            "gate_id": gate_id,
            "test_run_id": test_run_id,
        },
        message="评估完成",
    )


@router.get("/api/v1/quality-gates/{gate_id}/evaluations")
@release_session
def get_quality_gate_evaluations(
    gate_id: int, request: Request, user: User = Depends(_current_user)
):
    """获取质量门禁评估历史（组织隔离，越权 404）

    修正：v1 源文件未 import QualityGateEvaluation（NameError，实际 500），
    平迁补 import，分页响应结构按 v1 源码意图复刻。
    """
    user_id = user.id
    gate, err = _get_gate_with_permission(gate_id, user_id)
    if err:
        return err

    page = query_int(request, "page", 1)
    per_page = query_int(request, "per_page", 20)

    evaluations = (
        db.session.scalars(select(QualityGateEvaluation).filter_by(quality_gate_id=gate_id).order_by(QualityGateEvaluation.created_at.desc())).all())
    total = len(evaluations)
    start = (page - 1) * per_page
    items = evaluations[start : start + per_page]

    return _success(
        data={
            "items": [e.to_dict() for e in items],
            "total": total,
            "page": page,
            "per_page": per_page,
        }
    )
