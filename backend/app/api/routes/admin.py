"""
管理员模块 - FastAPI 平迁（自 app/api/admin.py，v1 中 admin 蓝图单独注册于 /api/v1 前缀）

路径/方法/状态码/响应字段与原 v1 完全一致（/api/v1/admin/users），
前端与 CI 脚本零改动。全部端点为同步 def，运行于 RequestContextMiddleware
push 的 app context 内，直接复用 数据库会话（app/database.py ContextVar 作用域）。

鉴权映射：
- @jwt_required() + _require_admin() → Depends(_admin_user)
  （语义等价 deps.get_admin_user：非 admin 角色 → 403 '需要管理员权限'；
  包装为 async 在事件循环线程执行，session 随 app context 回收）

会话生命周期：同步端点统一加 @release_session，防 NullPool 连接泄漏。
"""

from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import or_

from app.api.v2.deps import get_current_user, json_body, query_int, query_str, release_session
from app.core.logging import get_logger
from app.extensions import db
from app.models.user import User
from app.utils.validators import validate_password_strength
from sqlalchemy import select
from app.database import paginate

logger = get_logger(__name__)

router = APIRouter(tags=["admin"])


# ---------------------------------------------------------------------------
# 鉴权包装 / 响应构造
# ---------------------------------------------------------------------------

async def _current_user(request: Request) -> User:
    """鉴权依赖：在事件循环线程内执行同步 get_current_user，session 随 app context 回收"""
    return get_current_user(request)


async def _admin_user(request: Request) -> User:
    """管理员鉴权依赖（等价 deps.get_admin_user + v1 _require_admin 校验链）"""
    user = get_current_user(request)
    if user.role != "admin":
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


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


# ── 用户管理 ──────────────────────────────────────────────────────────────────

@router.get("/api/v1/admin/users")
@release_session
def list_users(request: Request, admin: User = Depends(_admin_user)):
    """用户列表（仅 admin）"""
    page = query_int(request, "page", 1)
    per_page = query_int(request, "per_page", 20)
    search = query_str(request, "search").strip()
    role_filter = query_str(request, "role").strip()

    query = select(User)
    if search:
        query = query.filter(or_(User.username.ilike(f"%{search}%"), User.email.ilike(f"%{search}%")))
    if role_filter:
        query = query.filter(User.role == role_filter)
    query = query.order_by(User.created_at.desc())
    pagination = paginate(query, page=page, per_page=per_page)
    return _success(data={
        "items": [u.to_dict(include_sensitive=True) for u in pagination.items],
        "total": pagination.total, "page": page, "per_page": per_page, "pages": pagination.pages,
    })


@router.get("/api/v1/admin/users/{user_id}")
@release_session
def get_user(user_id: int, admin: User = Depends(_admin_user)):
    """用户详情（仅 admin）"""
    user = db.session.get(User, user_id)
    if not user:
        return _error(404, "用户不存在")
    return _success(data=user.to_dict(include_sensitive=True))


@router.patch("/api/v1/admin/users/{user_id}/role")
@release_session
def update_user_role(
    user_id: int,
    data: Dict[str, Any] = Depends(json_body),
    admin: User = Depends(_admin_user),
):
    """修改用户角色（仅 admin）"""
    user = db.session.get(User, user_id)
    if not user:
        return _error(404, "用户不存在")
    data = data if isinstance(data, dict) else {}
    new_role = (data.get("role") or "").strip()
    if new_role not in ("admin", "member", "viewer"):
        return _error(400, "无效的角色，可选: admin, member, viewer")
    if user.id == admin.id:
        return _error(400, "不能修改自己的角色")
    old_role = user.role
    user.role = new_role
    db.session.commit()
    return _success(message=f"角色已从 {old_role} 修改为 {new_role}")


@router.patch("/api/v1/admin/users/{user_id}/status")
@release_session
def update_user_status(
    user_id: int,
    data: Dict[str, Any] = Depends(json_body),
    admin: User = Depends(_admin_user),
):
    """启用/禁用用户（仅 admin）"""
    user = db.session.get(User, user_id)
    if not user:
        return _error(404, "用户不存在")
    data = data if isinstance(data, dict) else {}
    is_active = data.get("is_active")
    if is_active is None:
        return _error(400, "请提供 is_active 参数")
    if user.id == admin.id:
        return _error(400, "不能禁用自己的账号")
    user.is_active = bool(is_active)
    db.session.commit()
    return _success(message=f"用户已{'启用' if user.is_active else '禁用'}")


@router.post("/api/v1/admin/users/{user_id}/reset-password")
@release_session
def reset_user_password(
    user_id: int,
    data: Dict[str, Any] = Depends(json_body),
    admin: User = Depends(_admin_user),
):
    """重置用户密码（仅 admin）"""
    user = db.session.get(User, user_id)
    if not user:
        return _error(404, "用户不存在")
    data = data if isinstance(data, dict) else {}
    new_password = (data.get("password") or "").strip()
    is_valid, error_msg = validate_password_strength(new_password)
    if not is_valid:
        return _error(400, error_msg)
    from app.core.passwords import generate_password_hash

    user.password_hash = generate_password_hash(new_password)
    db.session.commit()
    return _success(message="密码已重置")
