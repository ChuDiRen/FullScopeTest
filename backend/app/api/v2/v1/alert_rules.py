"""
性能告警规则模块 - FastAPI 平迁（自 app/api/alert_rules.py，蓝图挂载前缀 /api/v1）

路径/方法/状态码/响应字段与原 v1 完全一致（/api/v1/perf-test/alert-*）。

鉴权映射：
- @jwt_required() → Depends(_current_user)（内部复用 deps.get_current_user）
- 公开端点：无（alert_rules 全部端点在 v1 均要求 JWT）

会话生命周期：
- 同步端点统一加 @release_session（deps.release_session），防 NullPool 连接泄漏
- 鉴权依赖包装为 async _current_user：在事件循环线程执行 get_current_user

IDOR 防护（按 PerfTestScenario.user_id 归属，越权/不存在 → 404）：
- GET/PUT/DELETE /perf-test/alert-rules/{rule_id}、POST .../evaluate：
  源文件 _get_rule_with_permission 已有校验，照搬
- GET /perf-test/alert-rules：源文件已限定"全局规则 + 本人场景规则"，照搬
- POST /perf-test/alert-rules、PUT（改挂场景）：
  v1 未校验 scenario_id 归属（可把规则挂到他人场景再经 evaluate 读取其数据），
  平迁补齐——场景不存在或不属于当前用户 → 404
- GET /perf-test/alert-logs：v1 无属主过滤（可翻他人规则日志），
  平迁补齐——仅返回可见规则（全局规则 + 本人场景规则）的日志
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..deps import get_current_user, json_body, query_int, query_str, release_session
from ....core.logging import get_logger
from ....extensions import db
from ....models.perf_test_alert import PerformanceAlertLog, PerformanceAlertRule
from ....models.perf_test_scenario import PerfTestScenario
from ....models.user import User
from sqlalchemy import or_, select
from ....database import paginate

logger = get_logger(__name__)

router = APIRouter(tags=["alert-rules"])


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


def _get_rule_with_permission(rule_id: int, user_id: int):
    """获取告警规则并验证用户权限（通过 PerfTestScenario.user_id）——与 v1 一致"""
    rule = db.session.get(PerformanceAlertRule, rule_id)
    if not rule:
        return None, _error(404, "告警规则不存在")
    if rule.scenario_id:
        scenario = db.session.get(PerfTestScenario, rule.scenario_id)
        if not scenario or scenario.user_id != user_id:
            logger.warning(
                "IDOR attempt blocked on alert_rule",
                user_id=user_id,
                rule_id=rule_id,
            )
            return None, _error(404, "告警规则不存在")
    return rule, None


def _check_scenario_ownership(scenario_id: Optional[int], user_id: int) -> bool:
    """创建/改挂场景时校验场景归属（IDOR 修复：v1 未校验）"""
    if not scenario_id:
        return True
    scenario = db.session.get(PerfTestScenario, scenario_id)
    return bool(scenario and scenario.user_id == user_id)


def _visible_rule_ids(user_id: int) -> list:
    """当前用户可见的规则 ID：全局规则（scenario_id 为空）+ 本人场景的规则"""
    user_scenario_ids = [
        s.id for s in db.session.scalars(select(PerfTestScenario).filter_by(user_id=user_id)).all()]
    visible = db.session.scalars(select(PerformanceAlertRule).filter(
        or_(
            PerformanceAlertRule.scenario_id.is_(None),
            PerformanceAlertRule.scenario_id.in_(user_scenario_ids))
    )).all()
    return [r.id for r in visible]


# ==================== 告警规则 ====================

@router.get("/api/v1/perf-test/alert-rules")
@release_session
def get_alert_rules(request: Request, user: User = Depends(_current_user)):
    """获取告警规则列表（仅当前用户的规则 + 全局规则）"""
    user_id = user.id
    scenario_id = query_int(request, "scenario_id", 0) or None

    # 获取用户拥有的场景 ID 列表
    user_scenario_ids = [
        s.id for s in db.session.scalars(select(PerfTestScenario).filter_by(user_id=user_id)).all()]

    # 查询规则：全局规则（scenario_id 为空）或关联用户场景的规则
    query = select(PerformanceAlertRule).filter(
        or_(
            PerformanceAlertRule.scenario_id.is_(None),
            PerformanceAlertRule.scenario_id.in_(user_scenario_ids))
    )

    if scenario_id:
        query = query.filter(PerformanceAlertRule.scenario_id == scenario_id)

    rules = db.session.scalars(query.order_by(PerformanceAlertRule.created_at.desc())).all()
    return _success(data=[r.to_dict() for r in rules])


@router.post("/api/v1/perf-test/alert-rules")
@release_session
def create_alert_rule(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """
    创建告警规则

    IDOR 修复：scenario_id 指向他人场景 → 404（v1 未校验）。
    """
    user_id = user.id
    data = data or {}

    name = data.get("name")
    if not name:
        return _error(400, "name is required")

    scenario_id = data.get("scenario_id")
    if not _check_scenario_ownership(scenario_id, user_id):
        logger.warning(
            "IDOR attempt blocked on alert_rule create",
            user_id=user_id,
            scenario_id=scenario_id,
        )
        return _error(404, "关联场景不存在")

    rule = PerformanceAlertRule(
        name=name,
        description=data.get("description", ""),
        scenario_id=scenario_id,
        p95_threshold=data.get("p95_threshold"),
        p99_threshold=data.get("p99_threshold"),
        error_rate_threshold=data.get("error_rate_threshold"),
        rps_min_threshold=data.get("rps_min_threshold"),
        relative_p95_degradation=data.get("relative_p95_degradation"),
        relative_rps_degradation=data.get("relative_rps_degradation"),
        relative_error_rate_degradation=data.get("relative_error_rate_degradation"),
        notify_webhook=data.get("notify_webhook"),
        notify_email=data.get("notify_email"),
        enabled=data.get("enabled", True),
    )

    db.session.add(rule)
    db.session.commit()

    return _success(data=rule.to_dict(), message="告警规则创建成功")


@router.get("/api/v1/perf-test/alert-rules/{rule_id}")
@release_session
def get_alert_rule(rule_id: int, user: User = Depends(_current_user)):
    """获取告警规则详情（属主校验，越权 404）"""
    user_id = user.id
    rule, err = _get_rule_with_permission(rule_id, user_id)
    if err:
        return err
    return _success(data=rule.to_dict())


@router.put("/api/v1/perf-test/alert-rules/{rule_id}")
@release_session
def update_alert_rule(
    rule_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """
    更新告警规则（属主校验，越权 404）

    IDOR 修复：改挂 scenario_id 到他人场景 → 404（v1 未校验）。
    """
    user_id = user.id
    rule, err = _get_rule_with_permission(rule_id, user_id)
    if err:
        return err

    data = data or {}

    if "scenario_id" in data and not _check_scenario_ownership(data.get("scenario_id"), user_id):
        logger.warning(
            "IDOR attempt blocked on alert_rule update",
            user_id=user_id,
            scenario_id=data.get("scenario_id"),
        )
        return _error(404, "关联场景不存在")

    for field in ["name", "description", "scenario_id", "p95_threshold", "p99_threshold",
                  "error_rate_threshold", "rps_min_threshold", "relative_p95_degradation",
                  "relative_rps_degradation", "relative_error_rate_degradation",
                  "notify_webhook", "notify_email", "enabled"]:
        if field in data:
            setattr(rule, field, data[field])

    db.session.commit()
    return _success(data=rule.to_dict(), message="告警规则更新成功")


@router.delete("/api/v1/perf-test/alert-rules/{rule_id}")
@release_session
def delete_alert_rule(rule_id: int, user: User = Depends(_current_user)):
    """删除告警规则（属主校验，越权 404）"""
    user_id = user.id
    rule, err = _get_rule_with_permission(rule_id, user_id)
    if err:
        return err

    db.session.delete(rule)
    db.session.commit()
    return _success(message="告警规则删除成功")


@router.post("/api/v1/perf-test/alert-rules/{rule_id}/evaluate")
@release_session
def evaluate_alert_rule(
    rule_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """手动评估告警规则（指定测试结果 ID，属主校验，越权 404）"""
    from ....services.performance_alert_service import alert_service

    user_id = user.id
    rule, err = _get_rule_with_permission(rule_id, user_id)
    if err:
        return err

    data = data or {}
    test_result_id = data.get("test_result_id")
    if not test_result_id:
        return _error(400, "test_result_id is required")

    alerts = alert_service.evaluate_rules(test_result_id)
    return _success(data=alerts, message=f"评估完成，触发 {len(alerts)} 条告警")


# ==================== 告警日志 ====================

@router.get("/api/v1/perf-test/alert-logs")
@release_session
def get_alert_logs(request: Request, user: User = Depends(_current_user)):
    """
    获取告警日志

    IDOR 修复：v1 无属主过滤，平迁补齐——仅返回可见规则
    （全局规则 + 本人场景规则）的日志。
    """
    user_id = user.id
    page = query_int(request, "page", 1)
    per_page = query_int(request, "per_page", 20)
    rule_id = query_int(request, "rule_id", 0) or None
    alert_type = query_str(request, "alert_type").strip()

    visible_rule_ids = _visible_rule_ids(user_id)

    query = select(PerformanceAlertLog).filter(
        PerformanceAlertLog.rule_id.in_(visible_rule_ids)
    )

    if rule_id:
        query = query.filter_by(rule_id=rule_id)
    if alert_type:
        query = query.filter_by(alert_type=alert_type)

    pagination = paginate(
        query.order_by(PerformanceAlertLog.created_at.desc()),
        page=page, per_page=per_page,
    )

    return _paginate(
        items=[l.to_dict() for l in pagination.items],
        total=pagination.total,
        page=page,
        per_page=per_page,
    )
