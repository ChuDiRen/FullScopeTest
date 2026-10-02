"""
品牌配置模块 - FastAPI 平迁（自 app/api/branding.py，蓝图挂载前缀 /api/v1）

路径/方法/状态码/响应字段与原 v1 完全一致（/api/v1/branding/...）。

鉴权映射：
- @jwt_required() → Depends(_current_user)（内部复用 deps.get_current_user）
- 公开端点（v1 即无鉴权，保持公开并注明）：
  * GET /api/v1/branding/config — 前端启动时获取品牌配置（无需认证）

会话生命周期：
- 同步端点统一加 @release_session（deps.release_session），防 NullPool 连接泄漏
- 鉴权依赖包装为 async _current_user：在事件循环线程执行 get_current_user

校验逻辑与 v1 完全一致（不放宽）：
- PUT 更新品牌配置需管理员（user.is_admin()），非管理员 → 403（error_response
  信封与 v1 一致，故不使用 deps.get_admin_user 依赖）
- 更新作用于"当前用户所在组织"的配置（无组织则为全局配置），无跨组织访问面
"""

from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.api.v2.deps import get_current_user, json_body, release_session
from app.core.logging import get_logger
from app.extensions import db
from app.models.branding_config import BrandingConfig
from app.models.organization import OrganizationMember
from app.models.user import User
from sqlalchemy import select

logger = get_logger(__name__)

router = APIRouter(tags=["branding"])

# 默认品牌配置（与 v1 一致）
DEFAULT_BRANDING = {
    "platform_name": "大熊AI测试平台",
    "logo_url": None,
    "favicon_url": None,
    "primary_color": "#5FA59B",
    "login_background_url": None,
    "footer_text": "",
    "custom_css": "",
}


# ---------------------------------------------------------------------------
# 鉴权包装 / 响应构造
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


def datetime_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat() + "Z"


def _get_user_org_id(user_id: int):
    """获取用户当前组织 ID"""
    membership = (
        db.session.scalar(select(OrganizationMember).filter_by(user_id=user_id, is_active=True)))
    return membership.organization_id if membership else None


# ==================== 品牌配置 ====================

@router.get("/api/v1/branding/config")
@release_session
def get_branding_config(request: Request):
    """
    获取品牌配置（公开端点：v1 无 @jwt_required，前端启动时调用，保持公开）

    优先返回组织级配置，回退到全局默认配置。
    """
    raw_org_id = request.query_params.get("org_id")
    org_id = None
    if raw_org_id not in (None, ""):
        try:
            org_id = int(raw_org_id)
        except (TypeError, ValueError):
            org_id = None

    config = None

    if org_id:
        config = db.session.scalar(select(BrandingConfig).filter_by(
            organization_id=org_id, is_active=True
        ))

    if not config:
        config = db.session.scalar(select(BrandingConfig).filter_by(
            organization_id=None, is_active=True
        ))

    if config:
        return _success(data=config.to_dict())

    return _success(data=DEFAULT_BRANDING)


@router.put("/api/v1/branding/config")
@release_session
def update_branding_config(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """更新品牌配置（管理员，校验逻辑与 v1 一致）"""
    user_id = user.id
    if not user.is_admin():
        return _error(403, "需要管理员权限")

    org_id = _get_user_org_id(user_id)
    data = data or {}

    config = db.session.scalar(select(BrandingConfig).filter_by(organization_id=org_id))
    if not config:
        config = BrandingConfig(organization_id=org_id)
        db.session.add(config)

    for field in ["platform_name", "logo_url", "favicon_url", "primary_color",
                  "login_background_url", "footer_text", "custom_css"]:
        if field in data:
            setattr(config, field, data[field])

    db.session.commit()
    return _success(data=config.to_dict(), message="品牌配置已更新")
