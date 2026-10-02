"""
视觉回归测试模块 - FastAPI 平迁（自 app/api/visual.py，蓝图挂载前缀 /api/v1）

路径/方法/状态码/响应字段与原 v1 完全一致（/api/v1/visual/...），
前端与 CI 脚本零改动。全部端点为同步 def，运行于 RequestContextMiddleware
push 的 app context 内，直接复用 数据库会话（app/database.py ContextVar 作用域）。

鉴权映射：
- @jwt_required() → Depends(_current_user)（内部复用 deps.get_current_user）
- 公开端点：无（visual 全部端点在 v1 均要求 JWT）

会话生命周期：
- 同步端点统一加 @release_session（deps.release_session），防 NullPool 连接泄漏
- 鉴权依赖包装为 async _current_user：在事件循环线程执行 get_current_user

IDOR 防护（沿用源文件 _verify_visual_permission 模式，越权/不存在 → 404）：
- GET    /visual/baselines/{test_case_id}            ：基准所属项目 owner 校验（源文件已修复，照搬）
- POST   /visual/baselines/{baseline_id}/approve     ：同上
- DELETE /visual/baselines/{baseline_id}             ：同上
- GET    /visual/diffs/{test_run_id}                 ：test_run → project owner 校验（源文件已有）
- GET    /visual/history/{test_case_id}              ：v1 无属主校验，平迁补齐——
          以该 test_case 的基准/差异记录反查项目归属，非本人项目 → 404
"""

from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import case, func

from app.api.v2.deps import get_current_user, query_int, query_str, release_session
from app.core.logging import get_logger
from app.extensions import db
from app.models.project import Project
from app.models.test_run import TestRun
from app.models.user import User
from app.models.visual_baseline import VisualBaseline
from app.models.visual_diff import VisualDiff
from sqlalchemy import select
from app.database import paginate

logger = get_logger(__name__)

router = APIRouter(tags=["visual"])


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
            "timestamp": datetime_now_iso(),
        },
    )


def datetime_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat() + "Z"


def _verify_visual_permission(visual_obj, user_id: int) -> bool:
    """验证视觉资源的用户权限（通过 Project.owner_id）——与 v1 完全一致"""
    project_id = getattr(visual_obj, "project_id", None)
    if not project_id:
        return False
    project = db.session.get(Project, project_id)
    return bool(project and project.owner_id == user_id)


def _check_test_case_permission(test_case_id: int, user_id: int) -> bool:
    """
    按 test_case_id 判定属主：以该用例的基准截图（优先）或视觉差异
    （→ test_run → project）反查项目归属。

    返回 False 表示"存在归属他人的资源"；返回 True 表示"无资源可判定（放空列表）"
    或"归属当前用户"。
    """
    baseline = (
        db.session.scalar(select(VisualBaseline).filter_by(test_case_id=test_case_id).order_by(VisualBaseline.id.asc())))
    if baseline:
        return _verify_visual_permission(baseline, user_id)

    diff = (
        db.session.scalar(select(VisualDiff).filter_by(test_case_id=test_case_id).order_by(VisualDiff.id.asc())))
    if diff:
        test_run = db.session.get(TestRun, diff.test_run_id)
        if test_run:
            project = db.session.get(Project, test_run.project_id)
            return bool(project and project.owner_id == user_id)
    return True


# ==================== 基准截图 ====================

@router.get("/api/v1/visual/baselines/{test_case_id}")
@release_session
def get_baselines(test_case_id: int, request: Request, user: User = Depends(_current_user)):
    """
    获取指定测试用例的所有基准截图

    查询参数:
        test_type: 测试类型过滤 (api/web/app，可选)
        step_index: 步骤索引过滤 (可选)
    """
    user_id = user.id
    test_type = query_str(request, "test_type").strip()
    step_index_raw = request.query_params.get("step_index")
    step_index: Optional[int] = None
    if step_index_raw not in (None, ""):
        try:
            step_index = int(step_index_raw)
        except (TypeError, ValueError):
            step_index = None

    query = select(VisualBaseline).filter_by(test_case_id=test_case_id)

    if test_type:
        query = query.filter_by(test_type=test_type)
    if step_index is not None:
        query = query.filter_by(step_index=step_index)

    baselines = db.session.scalars(query.order_by(VisualBaseline.step_index, VisualBaseline.version.desc())).all()

    if not baselines:
        return _success(data=[])

    # 属主校验：基准截图所属项目的 owner 必须是当前用户（IDOR，越权 404）
    if not _verify_visual_permission(baselines[0], user_id):
        logger.warning(
            "IDOR attempt blocked on visual baselines",
            user_id=user_id,
            test_case_id=test_case_id,
        )
        return _error(404, "基准截图不存在")

    return _success(data=[b.to_dict() for b in baselines])


@router.post("/api/v1/visual/baselines/{baseline_id}/approve")
@release_session
def approve_baseline(baseline_id: int, user: User = Depends(_current_user)):
    """
    批准基准截图

    将指定基准截图标记为已批准（状态设为 active），并记录批准人
    """
    from datetime import datetime, timezone

    user_id = user.id
    baseline = db.session.get(VisualBaseline, baseline_id)

    if not baseline:
        return _error(404, "基准截图不存在")

    if not _verify_visual_permission(baseline, user_id):
        logger.warning(
            "IDOR attempt blocked on visual baseline",
            user_id=user_id,
            baseline_id=baseline_id,
        )
        return _error(404, "基准截图不存在")

    baseline.approved_by = user_id
    baseline.approved_at = datetime.now(timezone.utc).replace(tzinfo=None)
    baseline.status = "active"
    db.session.commit()

    logger.info(
        "基准截图已批准",
        baseline_id=baseline_id,
        approved_by=user_id,
        test_case_id=baseline.test_case_id,
    )

    return _success(data=baseline.to_dict(), message="基准截图已批准")


@router.delete("/api/v1/visual/baselines/{baseline_id}")
@release_session
def delete_baseline(baseline_id: int, user: User = Depends(_current_user)):
    """
    删除基准截图（软删除，标记为 deprecated）

    同时删除物理文件
    """
    import os

    from app.core.runtime import get_config

    user_id = user.id
    baseline = db.session.get(VisualBaseline, baseline_id)

    if not baseline:
        return _error(404, "基准截图不存在")

    if not _verify_visual_permission(baseline, user_id):
        logger.warning(
            "IDOR attempt blocked on visual baseline delete",
            user_id=user_id,
            baseline_id=baseline_id,
        )
        return _error(404, "基准截图不存在")

    # 删除物理文件
    base_path = get_config().get(
        "SCREENSHOT_STORAGE_PATH",
        os.path.join(str(Path(__file__).resolve().parents[2]), "uploads", "screenshots"),
    )
    full_path = os.path.join(base_path, baseline.baseline_image_path)
    if os.path.exists(full_path):
        try:
            os.remove(full_path)
        except OSError as e:
            logger.warning("删除基准截图文件失败", path=full_path, error=str(e))

    # 软删除
    baseline.status = "deprecated"
    db.session.commit()

    logger.info(
        "基准截图已删除",
        baseline_id=baseline_id,
        test_case_id=baseline.test_case_id,
    )

    return _success(message="基准截图已删除")


# ==================== 视觉差异 ====================

@router.get("/api/v1/visual/diffs/{test_run_id}")
@release_session
def get_diffs(test_run_id: int, request: Request, user: User = Depends(_current_user)):
    """
    获取指定测试执行的视觉差异记录

    查询参数:
        test_case_id: 测试用例 ID 过滤 (可选)
        status: 状态过滤 (可选)
        page: 页码 (默认 1)
        per_page: 每页数量 (默认 20)
    """
    user_id = user.id
    # 验证 test_run 所属项目的 owner（IDOR，越权 404）
    test_run = db.session.get(TestRun, test_run_id)
    if test_run:
        project = db.session.get(Project, test_run.project_id)
        if not project or project.owner_id != user_id:
            logger.warning(
                "IDOR attempt blocked on visual diffs",
                user_id=user_id,
                test_run_id=test_run_id,
            )
            return _error(404, "测试运行记录不存在")

    test_case_id = query_int(request, "test_case_id", 0) or None
    status = query_str(request, "status").strip()
    page = query_int(request, "page", 1)
    per_page = query_int(request, "per_page", 20)

    query = select(VisualDiff).filter_by(test_run_id=test_run_id)

    if test_case_id:
        query = query.filter_by(test_case_id=test_case_id)
    if status:
        query = query.filter_by(status=status)

    pagination = paginate(
        query.order_by(VisualDiff.created_at.desc()),
        page=page, per_page=per_page,
    )

    return _paginate(
        items=[d.to_dict() for d in pagination.items],
        total=pagination.total,
        page=page,
        per_page=per_page,
    )


# ==================== 视觉历史 ====================

@router.get("/api/v1/visual/history/{test_case_id}")
@release_session
def get_visual_history(test_case_id: int, user: User = Depends(_current_user)):
    """
    获取某个测试用例的视觉回归历史时间线

    返回按时间排序的每一轮测试执行的视觉差异汇总，用于趋势折线图。

    IDOR 修复：v1 无属主校验，平迁补齐——该用例的视觉资源归属他人项目 → 404。
    """
    user_id = user.id
    if not _check_test_case_permission(test_case_id, user_id):
        logger.warning(
            "IDOR attempt blocked on visual history",
            user_id=user_id,
            test_case_id=test_case_id,
        )
        return _error(404, "测试运行记录不存在")

    # 按 test_run_id 分组，获取每轮执行的视觉差异摘要
    rows = (
        db.session.execute(select(VisualDiff.test_run_id,
            func.min(VisualDiff.created_at).label("run_time"),
            func.avg(VisualDiff.diff_percentage).label("avg_diff"),
            func.max(VisualDiff.diff_percentage).label("max_diff"),
            func.min(VisualDiff.diff_percentage).label("min_diff"),
            func.count(VisualDiff.id).label("step_count"),
            func.sum(case((VisualDiff.status == "visual_fail", 1), else_=0)).label("fail_count"),
            func.sum(case((VisualDiff.status == "visual_pass", 1), else_=0)).label("pass_count"),).filter(VisualDiff.test_case_id == test_case_id).group_by(VisualDiff.test_run_id).order_by(func.min(VisualDiff.created_at).desc())).all())

    history = []
    for row in rows:
        # 获取该轮执行中第一个 diff 的缩略图路径作为代表
        sample = db.session.scalar(
            select(VisualDiff)
            .filter_by(test_run_id=row.test_run_id, test_case_id=test_case_id)
            .order_by(VisualDiff.step_index.asc())
        )
        history.append({
            "test_run_id": row.test_run_id,
            "run_time": row.run_time.isoformat() if row.run_time else None,
            "avg_diff_percentage": round(float(row.avg_diff or 0), 2),
            "max_diff_percentage": round(float(row.max_diff or 0), 2),
            "min_diff_percentage": round(float(row.min_diff or 0), 2),
            "step_count": row.step_count,
            "fail_count": row.fail_count or 0,
            "pass_count": row.pass_count or 0,
            "sample_diff_image": sample.diff_image_path if sample else None,
            "sample_baseline_image": (
                sample.baseline.baseline_image_path
                if sample and sample.baseline else None
            ),
            "sample_current_image": sample.current_image_path if sample else None,
        })

    return _success(data=history)
