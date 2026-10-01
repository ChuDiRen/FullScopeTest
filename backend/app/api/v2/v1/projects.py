"""
项目管理模块 - FastAPI 平迁（自 app/api/projects.py，蓝图挂载前缀 /api/v1）

路径/方法/状态码/响应字段与原 v1 完全一致（/api/v1/projects），
前端与 CI 脚本零改动。全部端点为同步 def，运行于 RequestContextMiddleware
push 的 app context 内，直接复用 数据库会话（app/database.py ContextVar 作用域） 与 services 层。

鉴权映射：
- @jwt_required() → Depends(_current_user)（内部复用 deps.get_current_user）

IDOR/权限修复（v1 为租户中间件单组织上下文过滤，FastAPI 路径无 before_request
租户钩子，平迁为显式的"属主或组织成员"可访问域过滤，语义不放宽）：
- GET /api/v1/projects               ：仅返回 自建项目 + 所在组织项目
- GET/PUT/DELETE /api/v1/projects/{id}：按可访问域过滤，越权/不存在 → 404（v1 错误风格）
- PUT /api/v1/projects/{id}/pin      ：同上

组织上下文（创建项目时的 organization_id）：
复刻 app/middleware/tenant.py set_tenant_context 语义 —
用户仅属于一个组织时自动归属该组织；多组织时取 X-Organization-ID 头或
organization_id 查询参数并校验成员身份；不属于任何组织 → None（个人项目）。

会话生命周期：同步端点统一加 @release_session，防 NullPool 连接泄漏。
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import or_

from ..deps import get_current_user, json_body, query_int, query_str, release_session
from ....core.logging import get_logger
from ....extensions import db
from ....models.organization import OrganizationMember
from ....models.project import Project
from ....models.user import User
from ....services.cache_service import get_cache_service, projects_key, PROJECTS_TTL
from sqlalchemy import select
from ....database import paginate

logger = get_logger(__name__)

router = APIRouter(tags=["projects"])


# ---------------------------------------------------------------------------
# 鉴权包装 / 响应构造 / 访问域过滤
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


def _paginate_response(items, total: int, page: int, per_page: int, message: str = "success") -> JSONResponse:
    """等价 v1 paginate_response：data.items + data.pagination{total,page,per_page,pages}"""
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


def _user_org_ids(user_id: int) -> List[int]:
    """用户所属（活跃）组织 ID 列表"""
    return [
        m.organization_id
        for m in db.session.scalars(select(OrganizationMember).filter_by(user_id=user_id, is_active=True)).all()]


def _accessible_filter(query, user_id: int):
    """属主或组织成员过滤：自建项目 ∪ 所在组织的项目（IDOR 修复）"""
    org_ids = _user_org_ids(user_id)
    cond = Project.owner_id == user_id
    if org_ids:
        cond = or_(cond, Project.organization_id.in_(org_ids))
    return query.filter(cond)


def _current_org_context(request: Request, user_id: int) -> Optional[int]:
    """
    创建资源时的组织上下文，复刻 v1 租户中间件语义：
    - 仅一个组织 → 自动归属
    - 多组织 → X-Organization-ID 头或 organization_id 查询参数（校验成员身份，防越权）
    - 无组织 / 校验失败 → None
    """
    org_ids = _user_org_ids(user_id)
    if len(org_ids) == 1:
        return org_ids[0]
    if len(org_ids) > 1:
        raw = request.headers.get("X-Organization-ID") or request.query_params.get("organization_id")
        if raw:
            try:
                org_id = int(raw)
            except (TypeError, ValueError):
                return None
            return org_id if org_id in org_ids else None
    return None


def _invalidate_projects_cache(user_id: int) -> None:
    """失效项目列表缓存（与 v1 一致）"""
    cache = get_cache_service()
    if cache:
        try:
            cache.delete(projects_key(user_id))
        except Exception as exc:  # pragma: no cover
            logger.warning("invalidate projects cache failed", user_id=user_id, error=str(exc))


# ── 项目 CRUD ────────────────────────────────────────────────────────────────

@router.get("/api/v1/projects")
@release_session
def get_projects(request: Request, user: User = Depends(_current_user)):
    """
    获取项目列表（IDOR 修复：仅返回 自建项目 + 所在组织项目）

    查询参数:
        page: 页码 (默认 1)
        per_page: 每页数量 (默认 20, 上限 100)
        keyword: 搜索关键词
    """
    user_id = user.id
    page = query_int(request, "page", 1)
    per_page = min(query_int(request, "per_page", 20), 100)
    if per_page < 1:
        per_page = 1
    keyword = query_str(request, "keyword").strip()

    # 仅首页无搜索关键词时使用缓存
    cache = get_cache_service()
    if cache and page == 1 and not keyword:
        cached = cache.get(projects_key(user_id))
        if cached is not None:
            return _success(data=cached)

    query = _accessible_filter(select(Project), user_id)

    if keyword:
        query = query.filter(Project.name.ilike(f"%{keyword}%"))

    # 置顶项目排在最前，按置顶时间排序，然后按创建时间倒序
    pagination = paginate(
        query.order_by(
            Project.is_pinned.desc(),
            Project.pinned_at.desc().nullslast(),
            Project.created_at.desc(),
        ),
        page=page, per_page=per_page,
    )

    result = {
        "items": [p.to_dict() for p in pagination.items],
        "pagination": {
            "total": pagination.total,
            "page": page,
            "per_page": per_page,
            "pages": pagination.pages,
        },
    }

    # 写入缓存
    if cache and page == 1 and not keyword:
        cache.set(projects_key(user_id), result, ttl=PROJECTS_TTL)

    return _paginate_response(
        items=result["items"],
        total=pagination.total,
        page=page,
        per_page=per_page,
    )


@router.post("/api/v1/projects")
@release_session
def create_project(
    request: Request,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """
    创建项目

    请求体:
        name: 项目名称
        description: 项目描述 (可选)
    """
    user_id = user.id

    # 等价 v1 @validate_json('name')
    if "application/json" not in (request.headers.get("content-type") or "").lower():
        return _error(400, "请求必须是 JSON 格式")
    data = data if isinstance(data, dict) else {}
    if not data:
        return _error(400, "请求体不能为空")
    if "name" not in data:
        return _error(400, "缺少必需字段: name")

    org_id = _current_org_context(request, user_id)

    name = data["name"].strip()
    description = data.get("description", "").strip()

    # 验证名称长度
    if len(name) < 1 or len(name) > 100:
        return _error(400, "项目名称长度应为 1-100 个字符")

    # 检查同名项目
    existing = select(Project).filter_by(owner_id=user_id, name=name)
    if org_id:
        existing = existing.filter_by(organization_id=org_id)
    existing = db.session.scalar(existing)
    if existing:
        return _error(400, "项目名称已存在")

    project = Project(
        name=name,
        description=description,
        owner_id=user_id,
        organization_id=org_id,
    )

    db.session.add(project)
    db.session.commit()

    # 失效项目列表缓存
    _invalidate_projects_cache(user_id)

    return _success(
        data=project.to_dict(),
        message="创建成功",
        code=200,
    )


@router.get("/api/v1/projects/{project_id}")
@release_session
def get_project(project_id: int, user: User = Depends(_current_user)):
    """获取项目详情（IDOR 修复：可访问域过滤，越权 404）"""
    user_id = user.id
    project = db.session.scalar(_accessible_filter(select(Project), user_id).filter_by(id=project_id))

    if not project:
        return _error(404, "项目不存在")

    return _success(data=project.to_dict())


@router.put("/api/v1/projects/{project_id}")
@release_session
def update_project(
    project_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """更新项目（IDOR 修复：可访问域过滤，越权 404）"""
    user_id = user.id
    project = db.session.scalar(_accessible_filter(select(Project), user_id).filter_by(id=project_id))

    if not project:
        return _error(404, "项目不存在")

    data = data if isinstance(data, dict) else {}

    if "name" in data:
        name = data["name"].strip()
        if len(name) < 1 or len(name) > 100:
            return _error(400, "项目名称长度应为 1-100 个字符")

        # 检查同名项目
        existing = _accessible_filter(select(Project), user_id)
        existing = db.session.scalar(existing.filter(Project.name == name, Project.id != project_id))
        if existing:
            return _error(400, "项目名称已存在")

        project.name = name

    if "description" in data:
        project.description = data["description"].strip()

    if "settings" in data:
        project.settings = data["settings"]

    db.session.commit()

    # 失效项目列表缓存
    _invalidate_projects_cache(user_id)

    return _success(
        data=project.to_dict(),
        message="更新成功",
    )


@router.delete("/api/v1/projects/{project_id}")
@release_session
def delete_project(project_id: int, user: User = Depends(_current_user)):
    """删除项目（IDOR 修复：可访问域过滤，越权 404）"""
    user_id = user.id
    project = db.session.scalar(_accessible_filter(select(Project), user_id).filter_by(id=project_id))

    if not project:
        return _error(404, "项目不存在")

    db.session.delete(project)
    db.session.commit()

    # 失效项目列表缓存
    _invalidate_projects_cache(user_id)

    return _success(message="删除成功")


@router.put("/api/v1/projects/{project_id}/pin")
@release_session
def toggle_pin_project(project_id: int, user: User = Depends(_current_user)):
    """置顶/取消置顶项目（IDOR 修复：可访问域过滤，越权 404）"""
    user_id = user.id
    project = db.session.scalar(_accessible_filter(select(Project), user_id).filter_by(id=project_id))

    if not project:
        return _error(404, "项目不存在")

    project.is_pinned = not project.is_pinned
    project.pinned_at = datetime.now(timezone.utc).replace(tzinfo=None) if project.is_pinned else None
    db.session.commit()

    # 失效项目列表缓存
    _invalidate_projects_cache(user_id)

    action = "置顶" if project.is_pinned else "取消置顶"
    return _success(data=project.to_dict(), message=f"{action}成功")
