"""定时报告 — 按天/周/月聚合测试统计并推送 webhook（APScheduler 真调度）"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.api.v2.deps import get_current_user, json_body, release_session
from app.extensions import db
from app.models.report_schedule import ReportSchedule
from app.models.user import User

router = APIRouter(prefix="/api/v1/report-schedules", tags=["report-schedules"])

VALID_FREQUENCIES = {"daily", "weekly", "monthly"}


def _owned_schedule(schedule_id: int, user: User) -> Optional[ReportSchedule]:
    return db.session.scalar(select(ReportSchedule).filter_by(id=schedule_id, user_id=user.id))


@router.get("")
@release_session
def list_schedules(user: User = Depends(get_current_user)):
    schedules = db.session.scalars(
        select(ReportSchedule).filter_by(user_id=user.id).order_by(ReportSchedule.id.desc())
    ).all()
    return {"code": 200, "data": [s.to_dict() for s in schedules]}


@router.post("")
@release_session
def create_schedule(data: Dict[str, Any] = Depends(json_body), user: User = Depends(get_current_user)):
    name = (data.get("name") or "").strip()
    if not name:
        return {"code": 400, "message": "name 不能为空"}
    frequency = (data.get("frequency") or "daily").strip().lower()
    if frequency not in VALID_FREQUENCIES:
        return {"code": 400, "message": f"frequency 必须是 {'/'.join(VALID_FREQUENCIES)}"}
    recipients = data.get("recipients")
    if recipients is not None and not isinstance(recipients, list):
        return {"code": 400, "message": "recipients 必须是数组"}
    project_id = data.get("project_id")
    if project_id is not None and not isinstance(project_id, int):
        return {"code": 400, "message": "project_id 必须是整数"}

    schedule = ReportSchedule(
        user_id=user.id,
        project_id=project_id,
        name=name,
        frequency=frequency,
        recipients=recipients or [],
        webhook_url=(data.get("webhook_url") or "").strip(),
        is_active=bool(data.get("is_active", True)),
    )
    db.session.add(schedule)
    db.session.commit()

    from app.scheduler import add_or_update_report_job
    if schedule.is_active:
        add_or_update_report_job(schedule)
    return {"code": 200, "data": schedule.to_dict(), "message": "创建成功"}


@router.put("/{schedule_id}")
@release_session
def update_schedule(
    schedule_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(get_current_user),
):
    schedule = _owned_schedule(schedule_id, user)
    if not schedule:
        return {"code": 404, "message": "调度不存在"}
    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            return {"code": 400, "message": "name 不能为空"}
        schedule.name = name
    if "frequency" in data:
        frequency = (data.get("frequency") or "").strip().lower()
        if frequency not in VALID_FREQUENCIES:
            return {"code": 400, "message": f"frequency 必须是 {'/'.join(VALID_FREQUENCIES)}"}
        schedule.frequency = frequency
    if "recipients" in data and isinstance(data.get("recipients"), list):
        schedule.recipients = data["recipients"]
    if "webhook_url" in data:
        schedule.webhook_url = (data.get("webhook_url") or "").strip()
    if "project_id" in data:
        schedule.project_id = data.get("project_id")
    if "is_active" in data:
        schedule.is_active = bool(data.get("is_active"))
    db.session.commit()

    from app.scheduler import add_or_update_report_job, remove_report_job
    if schedule.is_active:
        add_or_update_report_job(schedule)
    else:
        remove_report_job(schedule.id)
    return {"code": 200, "data": schedule.to_dict(), "message": "更新成功"}


@router.delete("/{schedule_id}")
@release_session
def delete_schedule(schedule_id: int, user: User = Depends(get_current_user)):
    schedule = _owned_schedule(schedule_id, user)
    if not schedule:
        return {"code": 404, "message": "调度不存在"}
    db.session.delete(schedule)
    db.session.commit()

    from app.scheduler import remove_report_job
    remove_report_job(schedule.id)
    return {"code": 200, "message": "删除成功"}


@router.post("/{schedule_id}/run")
@release_session
def run_now(schedule_id: int, user: User = Depends(get_current_user)):
    """立即执行一次统计并推送（同步执行，报告调度数据量小无需异步）"""
    from app.scheduler import execute_report_schedule

    schedule = _owned_schedule(schedule_id, user)
    if not schedule:
        return {"code": 404, "message": "调度不存在"}
    # execute 内部 session_teardown 会重置会话并使 ORM 实例分离，先取纯值
    target_id = schedule.id
    owner_id = user.id
    execute_report_schedule(target_id)
    fresh = db.session.scalar(
        select(ReportSchedule).filter_by(id=target_id, user_id=owner_id)
    )
    return {"code": 200, "data": fresh.to_dict() if fresh else None, "message": "已执行"}
