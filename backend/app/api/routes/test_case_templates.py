"""测试用例模板 — 系统内置 + 用户自定义（新建接口用例时快速套用）"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from app.api.v2.deps import get_current_user, json_body, release_session
from app.extensions import db
from app.models.test_case_template import TestCaseTemplate
from app.models.user import User

router = APIRouter(prefix="/api/v1/test-case-templates", tags=["test-case-templates"])


@router.get("")
@release_session
def list_templates(
    category: Optional[str] = Query(None),
    keyword: Optional[str] = Query(None),
    user: User = Depends(get_current_user),
):
    """内置模板（user_id NULL）+ 当前用户自定义模板"""
    query = select(TestCaseTemplate).filter(
        (TestCaseTemplate.user_id.is_(None)) | (TestCaseTemplate.user_id == user.id)
    )
    if category:
        query = query.filter(TestCaseTemplate.category == category)
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(TestCaseTemplate.name.ilike(like) | TestCaseTemplate.description.ilike(like))
    templates = db.session.scalars(query.order_by(
        TestCaseTemplate.user_id.is_(None).desc(), TestCaseTemplate.id
    )).all()
    return {"code": 200, "data": [t.to_dict() for t in templates]}


@router.get("/categories")
@release_session
def list_categories(user: User = Depends(get_current_user)):
    rows = db.session.scalars(select(TestCaseTemplate.category)).all()
    categories = sorted({r for r in rows if r})
    return {"code": 200, "data": categories}


@router.post("")
@release_session
def create_template(data: Dict[str, Any] = Depends(json_body), user: User = Depends(get_current_user)):
    name = (data.get("name") or "").strip()
    if not name:
        return {"code": 400, "message": "name 不能为空"}
    template = TestCaseTemplate(
        user_id=user.id,
        name=name,
        description=(data.get("description") or "").strip(),
        category=(data.get("category") or "通用").strip() or "通用",
        method=(data.get("method") or "GET").strip().upper()[:10],
        endpoint=(data.get("endpoint") or data.get("url_pattern") or "").strip()[:500],
        headers=(data.get("headers") or "{}").strip() or "{}",
        body=(data.get("body") or "").strip(),
        assertions=(data.get("assertions") or "").strip()[:500],
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
        return {"code": 404, "message": "模板不存在（内置模板不可修改）"}
    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            return {"code": 400, "message": "name 不能为空"}
        template.name = name
    for field in ("description", "category", "endpoint", "headers", "body", "assertions"):
        if field in data:
            setattr(template, field, (data.get(field) or "").strip() or ("{}" if field == "headers" else ""))
    if "method" in data:
        template.method = (data.get("method") or "GET").strip().upper()[:10]
    db.session.commit()
    return {"code": 200, "data": template.to_dict(), "message": "更新成功"}


@router.delete("/{template_id}")
@release_session
def delete_template(template_id: int, user: User = Depends(get_current_user)):
    template = _owned_template(template_id, user)
    if not template:
        return {"code": 404, "message": "模板不存在（内置模板不可删除）"}
    db.session.delete(template)
    db.session.commit()
    return {"code": 200, "message": "删除成功"}


def _owned_template(template_id: int, user: User) -> Optional[TestCaseTemplate]:
    """属主过滤：内置模板（user_id NULL）对所有人只读"""
    return db.session.scalar(select(TestCaseTemplate).filter_by(id=template_id, user_id=user.id))
