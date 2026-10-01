"""
组织管理模块 - FastAPI 平迁（自 app/api/organizations.py，蓝图挂载前缀 /api/v1）

路径/方法/状态码/响应字段与原 v1 完全一致（/api/v1/organizations、/api/v1/roles），
前端与 CI 脚本零改动。全部端点为同步 def，运行于 RequestContextMiddleware
push 的 app context 内，直接复用 数据库会话（app/database.py ContextVar 作用域） 与 services 层。

鉴权映射：
- @jwt_required() → Depends(_current_user)（内部复用 deps.get_current_user）

敏感操作校验链（与 v1 完全一致，不放宽）：
- 成员邀请：邀请者必须为组织活跃成员，且有效角色为 admin/manager/owner
- 移除成员：仅 admin/owner；禁止移除组织最后一个管理员
- 角色变更：仅 admin；禁止降级组织最后一个管理员；角色名合法性校验
- 自定义角色 CRUD：仅组织 admin

IDOR/权限修复（平迁核对项）：
- 所有 org_id 参数化端点在 v1 即有 membership（is_active）校验，平迁保持
  （非成员访问任何组织操作 → 403，与 v1 错误风格一致）；未做放宽。

会话生命周期：同步端点统一加 @release_session，防 NullPool 连接泄漏。
"""

import json as _json
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..deps import get_current_user, json_body, release_session
from ....core.logging import get_logger
from ....extensions import db
from ....models.organization import Organization, OrganizationMember
from ....models.role import VALID_ROLES
from ....models.user import User
from ....services import permission_service
from sqlalchemy import select
from sqlalchemy import func

logger = get_logger(__name__)

router = APIRouter(tags=["organizations"])


# ---------------------------------------------------------------------------
# 鉴权包装 / 响应构造 / 校验辅助
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


def _is_json_request(request: Request) -> bool:
    return "application/json" in (request.headers.get("content-type") or "").lower()


def _require_json_fields(request: Request, data: Optional[Dict[str, Any]], *required_fields) -> Optional[JSONResponse]:
    """
    等价 v1 @validate_json 校验链（消息与 v1 逐字一致）：
    非 JSON Content-Type → 400 '请求必须是 JSON 格式'；
    空 body → 400 '请求体不能为空'；
    缺字段 → 400 '缺少必需字段: xxx'。
    返回 None 表示通过。
    """
    if not _is_json_request(request):
        return _error(400, "请求必须是 JSON 格式")
    if not data:
        return _error(400, "请求体不能为空")
    missing = [f for f in required_fields if f not in data]
    if missing:
        return _error(400, f"缺少必需字段: {', '.join(missing)}")
    return None


def _get_membership(org_id: int, user_id: int) -> Optional[OrganizationMember]:
    """当前用户在组织中的活跃成员关系（v1 各端点统一的身份校验入口）"""
    return db.session.scalar(select(OrganizationMember).filter_by(
        organization_id=org_id, user_id=user_id, is_active=True,
    ))


# ── 组织 CRUD ────────────────────────────────────────────────────────────────

@router.post("/api/v1/organizations")
@release_session
def create_organization(
    request: Request,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """创建组织"""
    err = _require_json_fields(request, data, "name")
    if err:
        return err

    user_id = user.id
    name = data["name"].strip()
    slug = data.get("slug", "").strip()
    description = data.get("description", "").strip()

    if len(name) < 1 or len(name) > 100:
        return _error(400, "组织名称长度应为 1-100 个字符")

    if not slug:
        slug = name.lower().replace(" ", "-")

    existing = db.session.scalar(select(Organization).filter_by(slug=slug))
    if existing:
        return _error(400, "组织 slug 已存在")

    org = Organization(
        name=name,
        slug=slug,
        description=description,
        owner_id=user_id,
    )
    db.session.add(org)
    db.session.flush()

    # 创建者默认为 admin 角色（RBAC 兼容）
    member = OrganizationMember(
        organization_id=org.id,
        user_id=user_id,
        role="admin",
    )
    db.session.add(member)
    db.session.commit()

    return _success(data=org.to_dict(), message="组织创建成功", code=200)


@router.get("/api/v1/organizations/me")
@release_session
def get_my_organizations(user: User = Depends(_current_user)):
    """获取当前用户的组织列表"""
    user_id = user.id
    memberships = db.session.scalars(select(OrganizationMember).filter_by(user_id=user_id)).all()
    org_ids = [m.organization_id for m in memberships]
    orgs = db.session.scalars(select(Organization).filter(Organization.id.in_(org_ids))).all()
    return _success(data=[o.to_dict() for o in orgs])


# ── 成员管理 ──────────────────────────────────────────────────────────────────

@router.post("/api/v1/organizations/{org_id}/members")
@release_session
def invite_member(
    org_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """邀请成员（需要 project:manage 权限，v1 校验链原样平迁）"""
    user_id = user.id
    org = db.session.get(Organization, org_id)
    if not org:
        return _error(404, "组织不存在")

    membership = _get_membership(org_id, user_id)
    if not membership:
        return _error(403, "无权限")

    # 检查邀请者是否有 manage 权限（admin 或 manager 角色）
    inviter_role = membership.get_effective_role_name()
    if inviter_role not in ("admin", "manager", "owner"):
        return _error(403, "无权限邀请成员")

    data = data if isinstance(data, dict) else {}
    target_user_id = data.get("user_id")
    role = data.get("role", "tester")

    if not target_user_id:
        return _error(400, "缺少 user_id")

    # 验证角色是否合法
    if role not in VALID_ROLES and role not in ("owner", "member"):
        return _error(400, "无效的角色", errors={"valid_roles": VALID_ROLES})

    existing = db.session.scalar(select(OrganizationMember).filter_by(
        organization_id=org_id, user_id=target_user_id
    ))
    if existing:
        return _error(400, "用户已在组织中")

    member = OrganizationMember(
        organization_id=org_id,
        user_id=target_user_id,
        role=role,
        invited_by=user_id,
    )
    db.session.add(member)
    db.session.commit()

    return _success(data=member.to_dict(), message="成员邀请成功", code=200)


@router.delete("/api/v1/organizations/{org_id}/members/{target_user_id}")
@release_session
def remove_member(
    org_id: int,
    target_user_id: int,
    user: User = Depends(_current_user),
):
    """删除成员（需要 manage 权限或为组织所有者，v1 校验链原样平迁）"""
    user_id = user.id
    membership = _get_membership(org_id, user_id)
    if not membership:
        return _error(403, "无权限")

    # admin 角色或 owner 可移除成员
    inviter_role = membership.get_effective_role_name()
    if inviter_role not in ("admin", "owner"):
        return _error(403, "无权限")

    target = db.session.scalar(select(OrganizationMember).filter_by(
        organization_id=org_id, user_id=target_user_id
    ))
    if not target:
        return _error(404, "成员不存在")

    if target.role == "owner" or target.get_effective_role_name() == "admin":
        # 不能删除最后一个 admin
        admin_count = db.session.scalar(select(func.count()).select_from(select(OrganizationMember).filter_by(
            organization_id=org_id, is_active=True,
        ).filter(OrganizationMember.role.in_(["owner", "admin"])).subquery()))
        if admin_count <= 1:
            return _error(400, "不能删除组织唯一的管理员")

    db.session.delete(target)
    db.session.commit()

    return _success(message="成员已移除")


@router.patch("/api/v1/organizations/{org_id}/members/{target_user_id}/role")
@release_session
def update_member_role(
    org_id: int,
    target_user_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """修改成员角色（仅 admin 可操作，v1 校验链原样平迁）"""
    user_id = user.id
    membership = _get_membership(org_id, user_id)
    if not membership:
        return _error(403, "无权限")

    inviter_role = membership.get_effective_role_name()
    if inviter_role != "admin":
        return _error(403, "仅管理员可修改角色")

    target = db.session.scalar(select(OrganizationMember).filter_by(
        organization_id=org_id, user_id=target_user_id
    ))
    if not target:
        return _error(404, "成员不存在")

    data = data if isinstance(data, dict) else {}
    new_role = data.get("role")
    if new_role not in VALID_ROLES:
        return _error(400, "无效的角色", errors={"valid_roles": VALID_ROLES})

    # 不允许降级最后一个 admin
    if target.role in ("owner", "admin") and new_role != "admin":
        admin_count = db.session.scalar(select(func.count()).select_from(select(OrganizationMember).filter_by(
            organization_id=org_id, is_active=True,
        ).filter(OrganizationMember.role.in_(["owner", "admin"])).subquery()))
        if admin_count <= 1:
            return _error(400, "不能降级组织唯一的管理员")

    target.role = new_role
    db.session.commit()

    return _success(data=target.to_dict(), message="角色修改成功")


# ── 成员权限查询 ─────────────────────────────────────────────────────────────

@router.get("/api/v1/organizations/{org_id}/my-permissions")
@release_session
def get_my_permissions(org_id: int, user: User = Depends(_current_user)):
    """获取当前用户在指定组织中的权限信息"""
    user_id = user.id
    membership = _get_membership(org_id, user_id)
    if not membership:
        return _error(403, "不属于该组织")

    permissions = permission_service.get_user_permissions(user_id, org_id)
    role_name = permission_service.get_user_role_name(user_id, org_id)

    return _success(data={
        "role": role_name,
        "original_role": membership.role,
        "permissions": permissions,
    })


@router.get("/api/v1/organizations/{org_id}/members/{target_user_id}/permissions")
@release_session
def get_member_permissions(
    org_id: int,
    target_user_id: int,
    user: User = Depends(_current_user),
):
    """获取指定成员在组织中的权限信息（需要 manage 权限）"""
    user_id = user.id
    membership = _get_membership(org_id, user_id)
    if not membership:
        return _error(403, "无权限")

    inviter_role = membership.get_effective_role_name()
    if inviter_role not in ("admin", "manager", "owner"):
        return _error(403, "需要管理员或经理权限")

    target = db.session.scalar(select(OrganizationMember).filter_by(
        organization_id=org_id, user_id=target_user_id, is_active=True,
    ))
    if not target:
        return _error(404, "成员不存在")

    permissions = target.get_permissions()
    return _success(data={
        "user_id": target_user_id,
        "role": target.get_effective_role_name(),
        "original_role": target.role,
        "permissions": permissions,
    })


# ── 角色管理 ──────────────────────────────────────────────────────────────────

@router.get("/api/v1/organizations/{org_id}/roles")
@release_session
def list_roles(org_id: int, user: User = Depends(_current_user)):
    """获取组织可用的角色列表（系统角色 + 自定义角色）"""
    user_id = user.id
    membership = _get_membership(org_id, user_id)
    if not membership:
        return _error(403, "不属于该组织")

    roles = permission_service.get_organization_roles(org_id)
    return _success(data=roles)


@router.post("/api/v1/organizations/{org_id}/roles")
@release_session
def create_role(
    org_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """创建自定义角色（仅 admin）"""
    user_id = user.id
    membership = _get_membership(org_id, user_id)
    if not membership or membership.get_effective_role_name() != "admin":
        return _error(403, "仅管理员可创建角色")

    data = data if isinstance(data, dict) else {}
    name = (data.get("name") or "").strip()
    display_name = (data.get("display_name") or "").strip()
    permissions = data.get("permissions", {})
    description = data.get("description", "")

    if not name:
        return _error(400, "缺少角色标识")
    if not display_name:
        return _error(400, "缺少角色显示名称")

    try:
        role = permission_service.create_custom_role(
            organization_id=org_id,
            name=name,
            display_name=display_name,
            permissions=permissions,
            description=description,
        )
    except ValueError as e:
        return _error(400, str(e))

    return _success(data=role.to_dict(), message="角色创建成功", code=200)


@router.put("/api/v1/organizations/{org_id}/roles/{role_id}")
@release_session
def update_role(
    org_id: int,
    role_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """更新自定义角色（仅 admin）"""
    user_id = user.id
    membership = _get_membership(org_id, user_id)
    if not membership or membership.get_effective_role_name() != "admin":
        return _error(403, "仅管理员可修改角色")

    data = data if isinstance(data, dict) else {}

    try:
        role = permission_service.update_custom_role(
            role_id=role_id,
            organization_id=org_id,
            display_name=data.get("display_name"),
            permissions=data.get("permissions"),
            description=data.get("description"),
        )
    except ValueError as e:
        return _error(400, str(e))

    return _success(data=role.to_dict(), message="角色更新成功")


@router.delete("/api/v1/organizations/{org_id}/roles/{role_id}")
@release_session
def delete_role(
    org_id: int,
    role_id: int,
    user: User = Depends(_current_user),
):
    """删除自定义角色（仅 admin，软删除）"""
    user_id = user.id
    membership = _get_membership(org_id, user_id)
    if not membership or membership.get_effective_role_name() != "admin":
        return _error(403, "仅管理员可删除角色")

    try:
        permission_service.delete_custom_role(role_id=role_id, organization_id=org_id)
    except ValueError as e:
        return _error(400, str(e))

    return _success(message="角色已删除")


@router.get("/api/v1/roles/system")
@release_session
def list_system_roles(user: User = Depends(_current_user)):
    """获取所有系统角色定义"""
    roles = permission_service.get_system_roles()
    return _success(data=roles)
