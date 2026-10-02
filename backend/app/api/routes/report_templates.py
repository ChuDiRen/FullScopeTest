"""报告模板 — 自定义测试报告展示模块与主题（属主隔离）"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.api.v2.deps import get_current_user, json_body, release_session
from app.extensions import db
from app.models.report_template import ReportTemplate
from app.models.user import User

router = APIRouter(prefix="/api/v1/report-templates", tags=["report-templates"])

ALLOWED_MODULE_IDS = {
    "summary", "pass_rate", "duration", "failed_cases",
    "trend", "env_info", "ai_analysis", "screenshots",
}


def _owned_template(template_id: int, user: User) -> Optional[ReportTemplate]:
    """属主过滤是铁律：按 id 取资源必须带 user_id（越权 404）"""
    return db.session.scalar(select(ReportTemplate).filter_by(id=template_id, user_id=user.id))


@router.get("")
@release_session
def list_templates(user: User = Depends(get_current_user)):
    templates = db.session.scalars(
        select(ReportTemplate).filter_by(user_id=user.id).order_by(ReportTemplate.updated_at.desc())
    ).all()
    return {"code": 200, "data": [t.to_dict() for t in templates]}


@router.post("")
@release_session
def create_template(data: Dict[str, Any] = Depends(json_body), user: User = Depends(get_current_user)):
    name = (data.get("name") or "").strip()
    if not name:
        return {"code": 400, "message": "name 不能为空"}
    modules = _normalize_modules(data.get("modules"))
    if modules is None:
        return {"code": 400, "message": "modules 格式不合法"}
    template = ReportTemplate(
        user_id=user.id,
        name=name,
        modules=modules,
        theme=(data.get("theme") or "default").strip() or "default",
    )
    db.session.add(template)
    db.session.commit()
    return {"code": 200, "data": template.to_dict(), "message": "创建成功"}


@router.put("/{template_id}")
@release_session
def update_template(
    template_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(get_current_user),
):
    template = _owned_template(template_id, user)
    if not template:
        return {"code": 404, "message": "模板不存在"}
    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            return {"code": 400, "message": "name 不能为空"}
        template.name = name
    if "modules" in data:
        modules = _normalize_modules(data.get("modules"))
        if modules is None:
            return {"code": 400, "message": "modules 格式不合法"}
        template.modules = modules
    if "theme" in data:
        template.theme = (data.get("theme") or "default").strip() or "default"
    db.session.commit()
    return {"code": 200, "data": template.to_dict(), "message": "更新成功"}


@router.delete("/{template_id}")
@release_session
def delete_template(template_id: int, user: User = Depends(get_current_user)):
    template = _owned_template(template_id, user)
    if not template:
        return {"code": 404, "message": "模板不存在"}
    db.session.delete(template)
    db.session.commit()
    return {"code": 200, "message": "删除成功"}


def _normalize_modules(raw) -> Optional[List[Dict[str, Any]]]:
    """模块列表清洗：仅允许白名单模块 id，保留 order 排序"""
    if raw is None:
        return []
    if not isinstance(raw, list):
        return None
    cleaned = []
    for m in raw:
        if not isinstance(m, dict):
            return None
        mid = m.get("id")
        if mid not in ALLOWED_MODULE_IDS:
            return None
        cleaned.append({
            "id": mid,
            "name": str(m.get("name") or mid),
            "enabled": bool(m.get("enabled", True)),
            "order": int(m.get("order") or 0),
        })
    cleaned.sort(key=lambda x: x["order"])
    return cleaned
