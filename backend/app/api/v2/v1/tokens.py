"""
API Token 管理模块 - FastAPI 平迁（自 app/api/tokens.py，蓝图挂载前缀 /api/v1）

路径/方法/状态码/响应字段与原 v1 完全一致（/api/v1/tokens/...），
前端与 CI 脚本零改动。全部端点为同步 def，运行于 RequestContextMiddleware
push 的 app context 内，直接复用 数据库会话（app/database.py ContextVar 作用域） 与 services 层。

鉴权映射：
- @jwt_required() → Depends(_current_user)（内部复用 deps.get_current_user）
- 公开端点：无（本模块 4 个端点在 v1 全部有 @jwt_required）

Token 安全（与 v1 完全一致，不做任何弱化）：
- 生成走 app/services/token_service.create_token：secrets.token_urlsafe(32) 生成明文，
  sha256(token) 哈希后落库（ApiToken.token_hash），明文仅在创建响应中返回一次
- 列表/详情永远不返回明文 token（ApiToken.to_dict() 不含 token_hash/token 明文）
- 删除按 user_id 属主过滤，他人 Token → 404

与 v1 的一致性说明：
- POST /api/v1/tokens/validate 沿用 v1 的原样语义：需 JWT 登录，随后读取
  Authorization: Bearer 头内容并按 API Token 校验（v1 即如此设计，header 中是
  JWT 时 validate_token 按 sha256 查找必然未命中 → 401 'Token 无效'；该端点
  实际供携带 API Token 的调用方使用）。
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..deps import get_current_user, json_body, query_int, release_session
from ....extensions import db
from ....models.api_token import VALID_TOKEN_ACTIONS, ApiToken
from ....models.user import User
from ....services.token_service import check_token_permission, create_token, validate_token
from sqlalchemy import select
from sqlalchemy import func

router = APIRouter(tags=["tokens"])


# ---------------------------------------------------------------------------
# 鉴权包装 / 响应构造
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


def _convert_old_permissions(permissions: Any) -> Optional[list]:
    """将旧格式权限转换为新的 actions 列表（与 v1 一致）"""
    if not isinstance(permissions, list):
        return None
    actions = []
    for p in permissions:
        if p == "read-only":
            actions.append("read")
        elif p == "read-write":
            actions.extend(["read", "write", "execute"])
        else:
            return None
    return list(set(actions)) if actions else None


# ==================== Token CRUD ====================

@router.get("/api/v1/tokens")
@release_session
def get_tokens(request: Request, user: User = Depends(_current_user)):
    """
    获取当前用户的所有 API Token（按 user_id 属主过滤，不返回明文 token）

    查询参数:
        page: 页码 (默认 1)
        per_page: 每页数量 (默认 20)
    """
    user_id = user.id
    page = query_int(request, "page", 1)
    per_page = query_int(request, "per_page", 20)

    query = select(ApiToken).filter_by(user_id=user_id).order_by(ApiToken.created_at.desc())

    # 手动分页（与 v1 Flask-SQLAlchemy paginate 语义一致：error_out=False）
    total = db.session.scalar(select(func.count()).select_from(query.subquery()))
    items = db.session.scalars(query.offset((page - 1) * per_page).limit(per_page)).all()
    pages = (total + per_page - 1) // per_page

    return _success(
        data={
            "items": [t.to_dict() for t in items],
            "pagination": {
                "total": total,
                "page": page,
                "per_page": per_page,
                "pages": pages,
            },
        }
    )


@router.post("/api/v1/tokens")
@release_session
def create_token_api(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """
    创建新的 API Token（明文 token 仅创建时返回一次，服务端只存 sha256 哈希）

    请求体:
        name: Token 名称 (必填)
        actions: 允许的操作列表 (可选, 默认 ['read'])
            合法值: read, write, execute, delete
        project_ids: 项目 ID 白名单 (可选, 空列表表示不限制)
        permissions: 旧格式权限 (可选, 向后兼容)
            可选值: read-only, read-write
        expires_in_days: 有效期天数 (可选, 默认 null 表示不过期)
    """
    user_id = user.id
    data = data or {}

    # 等价 v1 @validate_json('name')
    if not data:
        return _error(400, "请求体不能为空")
    if "name" not in data:
        return _error(400, "缺少必需字段: name")

    name = str(data["name"]).strip()
    actions = data.get("actions")
    project_ids = data.get("project_ids", [])
    old_permissions = data.get("permissions")
    expires_in_days = data.get("expires_in_days")

    # 支持旧格式 → 新格式转换
    if actions is None and old_permissions:
        actions = _convert_old_permissions(old_permissions)
        if actions is None:
            return _error(400, "权限格式无效，可选值: read-only, read-write")
    elif actions is None:
        actions = ["read"]

    # 校验操作类型
    invalid_actions = set(actions) - VALID_TOKEN_ACTIONS
    if invalid_actions:
        return _error(
            400,
            f"无效的操作类型: {invalid_actions}",
            errors={"valid_actions": list(VALID_TOKEN_ACTIONS)},
        )

    # 校验 project_ids 格式
    if not isinstance(project_ids, list):
        return _error(400, "project_ids 必须为数组")
    for pid in project_ids:
        if not isinstance(pid, int):
            return _error(400, f"project_ids 中的值必须为整数: {pid}")

    try:
        api_token, token = create_token(
            user_id=user_id,
            name=name,
            actions=actions,
            project_ids=project_ids,
            expires_in_days=expires_in_days,
        )
    except ValueError as e:
        return _error(400, str(e))

    return _success(
        data={
            "id": api_token.id,
            "token": token,  # 仅在创建时返回明文
            "name": api_token.name,
            "actions": actions,
            "project_ids": project_ids,
            "permissions": api_token.permissions,
            "expires_at": api_token.expires_at.isoformat() if api_token.expires_at else None,
        },
        message="Token 创建成功",
        code=200,
    )


@router.delete("/api/v1/tokens/{token_id}")
@release_session
def delete_token(token_id: int, user: User = Depends(_current_user)):
    """删除 API Token（user_id 属主过滤，他人 Token → 404）"""
    user_id = user.id

    api_token = db.session.scalar(select(ApiToken).filter_by(id=token_id, user_id=user_id))
    if not api_token:
        return _error(404, "Token 不存在")

    db.session.delete(api_token)
    db.session.commit()

    return _success(message="Token 已删除")


@router.post("/api/v1/tokens/validate")
@release_session
def validate_token_api(
    request: Request,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """
    验证当前请求中 API Token 的权限

    请求体:
        action: 要检查的操作
        project_id: 要检查的项目 ID（可选）

    返回:
        Token 是否有权限执行指定操作

    说明：与 v1 一致，本端点需 JWT 登录；随后将 Authorization: Bearer 头的
    内容按 API Token（sha256 查找）校验权限。
    """
    data = data or {}

    # 获取当前 Token（通过 Authorization header，与 v1 一致）
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return _error(400, "缺少 Token")

    token = auth_header[7:]
    api_token = validate_token(token)
    if not api_token:
        return _error(401, "Token 无效")

    has_perm = check_token_permission(api_token, data.get("action", "read"), data.get("project_id"))
    return _success(
        data={
            "has_permission": has_perm,
            "token_name": api_token.name,
            "actions": api_token.get_actions(),
            "project_ids": api_token.project_ids or [],
        }
    )
