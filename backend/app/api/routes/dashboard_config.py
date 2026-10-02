"""
仪表盘配置模块 - FastAPI 平迁（自 app/api/dashboard_config.py，蓝图挂载前缀 /api/v1）

路径/方法/状态码/响应字段与原 v1 完全一致（/api/v1/dashboard/...）。

鉴权映射：
- @jwt_required() → Depends(_current_user)（内部复用 deps.get_current_user）
- 公开端点：无（dashboard_config 全部端点在 v1 均要求 JWT）

会话生命周期：
- 同步端点统一加 @release_session（deps.release_session），防 NullPool 连接泄漏
- 鉴权依赖包装为 async _current_user：在事件循环线程执行 get_current_user

IDOR 防护（按 user_id 归属过滤，与 v1 一致）：
- 全部端点均以 filter_by(user_id=user_id)（+ 当前组织）为访问域，
  仅能读写自己的仪表盘布局，无跨用户访问面。
"""

from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.api.v2.deps import get_current_user, json_body, release_session
from app.core.logging import get_logger
from app.extensions import db
from app.utils.org_filter import get_current_organization_id
from app.models.dashboard_widget import DashboardWidget, WIDGET_TYPES, create_default_widgets
from app.models.user import User
from sqlalchemy import delete, select

logger = get_logger(__name__)

router = APIRouter(tags=["dashboard-config"])


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


def _user_widgets_query(user_id: int, org_id):
    """当前用户（+ 当前组织）的仪表盘组件查询"""
    query = select(DashboardWidget).filter_by(user_id=user_id)
    if org_id:
        query = query.filter_by(organization_id=org_id)
    return query


# ==================== 仪表盘组件 ====================

@router.get("/api/v1/dashboard/widgets")
@release_session
def get_widgets(user: User = Depends(_current_user)):
    """获取当前用户的仪表盘组件配置"""
    user_id = user.id
    org_id = get_current_organization_id()

    query = _user_widgets_query(user_id, org_id)

    widgets = db.session.scalars(query.order_by(
        DashboardWidget.position_y, DashboardWidget.position_x
    )).all()

    # 如果用户没有配置，创建默认布局
    if not widgets and org_id:
        default_widgets = create_default_widgets(user_id, org_id)
        db.session.add_all(default_widgets)
        db.session.commit()
        widgets = default_widgets

    return _success(data=[w.to_dict() for w in widgets])


@router.put("/api/v1/dashboard/widgets")
@release_session
def update_widgets(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """批量更新仪表盘组件配置（整体布局保存，仅作用于当前用户自己的配置）"""
    user_id = user.id
    org_id = get_current_organization_id()
    data = data or {}
    widgets_data = data.get("widgets", [])

    if not widgets_data:
        return _error(400, "缺少 widgets 数据")

    # 删除旧配置
    db.session.execute(delete(DashboardWidget).where(_user_widgets_query(user_id, org_id).exists()))

    # 创建新配置
    for wd in widgets_data:
        widget = DashboardWidget(
            user_id=user_id,
            organization_id=org_id or 0,
            widget_type=wd.get("widget_type", ""),
            title=wd.get("title", ""),
            config=wd.get("config", {}),
            position_x=wd.get("position_x", 0),
            position_y=wd.get("position_y", 0),
            width=wd.get("width", 1),
            height=wd.get("height", 1),
            is_visible=wd.get("is_visible", True),
        )
        db.session.add(widget)

    db.session.commit()

    widgets = db.session.scalars(
        _user_widgets_query(user_id, org_id).order_by(
            DashboardWidget.position_y, DashboardWidget.position_x
        )
    ).all()

    return _success(data=[w.to_dict() for w in widgets], message="布局已保存")


@router.post("/api/v1/dashboard/widgets/reset")
@release_session
def reset_widgets(user: User = Depends(_current_user)):
    """恢复默认仪表盘布局（仅清除当前用户自己的配置）"""
    user_id = user.id
    org_id = get_current_organization_id()

    db.session.execute(delete(DashboardWidget).where(_user_widgets_query(user_id, org_id).exists()))

    if org_id:
        default_widgets = create_default_widgets(user_id, org_id)
        db.session.add_all(default_widgets)
        db.session.commit()
        return _success(data=[w.to_dict() for w in default_widgets], message="已恢复默认布局")

    return _success(data=[], message="已恢复默认布局")


@router.get("/api/v1/dashboard/widget-types")
@release_session
def get_widget_types(user: User = Depends(_current_user)):
    """获取可用的组件类型列表"""
    return _success(data=WIDGET_TYPES)
