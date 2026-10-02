"""Flaky 测试检测 — 基于用例执行历史分析不稳定性"""

from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.api.v2.deps import get_current_user, release_session
from app.models.user import User
from app.services.ai.flaky_detector_service import get_flaky_detector_service

router = APIRouter(prefix="/api/v1/flaky-detector", tags=["flaky-detector"])


def _scoped_user(user: User):
    """非管理员限定自己的项目范围（项目级归属过滤与列表口径一致）"""
    return None if user.is_admin else user.id


@router.get("/analyze")
@release_session
def analyze(
    project_id: Optional[int] = Query(None),
    recent_runs: int = Query(20, ge=3, le=100),
    min_runs: int = Query(3, ge=1, le=50),
    user: User = Depends(get_current_user),
):
    """Flaky 用例列表（按 flaky_score 降序）"""
    svc = get_flaky_detector_service()
    results = svc.detect_flaky_tests(
        project_id=project_id,
        recent_runs=recent_runs,
        min_runs=min_runs,
    )
    # 转换为前端 FlakyCase 契约
    data = [
        {
            "case_id": r["case_id"],
            "case_name": r["case_name"],
            "stability_score": round(100 - r["flaky_score"], 1),
            "total_runs": r["run_count"],
            "flaky_count": r["status_changes"],
            "last_status": r["last_status"],
            "pattern": r.get("label", ""),
            "suggestion": _suggestion_for(r),
        }
        for r in results
    ]
    return {"code": 200, "data": data}


@router.get("/report")
@release_session
def report(
    project_id: Optional[int] = Query(None),
    top_n: int = Query(10, ge=1, le=50),
    user: User = Depends(get_current_user),
):
    """Flaky 汇总报告（疑似/确认分类 + Top N）"""
    return {"code": 200, "data": get_flaky_detector_service().get_flaky_report(
        project_id=project_id, top_n=top_n)}


def _suggestion_for(r: dict) -> str:
    if r["flaky_score"] > 60:
        return "高频不稳定：检查测试数据隔离与环境依赖，考虑加入重试或隔离执行"
    if r["flaky_score"] > 30:
        return "疑似不稳定：关注接口响应波动与断言超时设置"
    return "表现稳定"
