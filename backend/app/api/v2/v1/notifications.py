"""
通知配置模块 - FastAPI 平迁（自 app/api/notifications.py，蓝图挂载前缀 /api/v1）

路径/方法/状态码/响应字段与原 v1 完全一致（/api/v1/notifications/...）。

鉴权映射：
- @jwt_required() → Depends(_current_user)（内部复用 deps.get_current_user）
- 公开端点：无（notifications 全部端点在 v1 均要求 JWT）

会话生命周期：
- 同步端点统一加 @release_session（deps.release_session），防 NullPool 连接泄漏
- 鉴权依赖包装为 async _current_user：在事件循环线程执行 get_current_user

IDOR 防护（按 user_id 过滤，越权/不存在 → 404，与 v1 一致）：
- GET/POST /notifications/configs                ：仅当前用户的配置
- PUT/DELETE /notifications/configs/{config_id}  ：filter_by(id, user_id)，他人配置 404
- POST /notifications/configs/{config_id}/test   ：同上
"""

from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..deps import get_current_user, json_body, release_session
from ....core.logging import get_logger
from ....extensions import db
from ....models.notification_config import NotificationConfig
from ....models.user import User
from sqlalchemy import select

logger = get_logger(__name__)

router = APIRouter(tags=["notifications"])


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


# ==================== 通知配置 ====================

@router.get("/api/v1/notifications/configs")
@release_session
def get_notification_configs(user: User = Depends(_current_user)):
    """获取当前用户的通知配置列表"""
    user_id = user.id
    configs = (
        db.session.scalars(select(NotificationConfig).filter_by(user_id=user_id).order_by(NotificationConfig.created_at.desc())).all())
    return _success(data=[c.to_dict() for c in configs])


@router.post("/api/v1/notifications/configs")
@release_session
def create_notification_config(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """创建通知配置"""
    user_id = user.id
    data = data or {}

    name = (data.get("name") or "").strip()
    channel = (data.get("channel_type") or data.get("channel") or "").strip()
    webhook_url = (data.get("webhook_url") or "").strip()

    if not name:
        return _error(400, "name 不能为空")
    if not channel:
        return _error(400, "channel_type 不能为空")
    if not webhook_url:
        return _error(400, "webhook_url 不能为空")

    valid_channels = ("webhook", "dingtalk", "feishu", "slack")
    if channel not in valid_channels:
        return _error(400, f'channel_type 必须是 {"/".join(valid_channels)} 之一')

    config = NotificationConfig(
        user_id=user_id,
        name=name,
        channel=channel,
        webhook_url=webhook_url,
        token=data.get("token", ""),
        events=data.get("events", []),
        is_active=data.get("is_active", True),
    )
    db.session.add(config)
    db.session.commit()

    logger.info("通知配置已创建", config_id=config.id, channel=channel)
    return _success(data=config.to_dict(), message="创建成功", code=200)


@router.put("/api/v1/notifications/configs/{config_id}")
@release_session
def update_notification_config(
    config_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """更新通知配置（IDOR：仅本人配置可改，他人配置 404）"""
    user_id = user.id
    config = db.session.scalar(select(NotificationConfig).filter_by(id=config_id, user_id=user_id))
    if not config:
        return _error(404, "通知配置不存在")

    data = data or {}

    if "name" in data:
        config.name = data["name"]
    if "channel_type" in data or "channel" in data:
        channel = data.get("channel_type") or data.get("channel")
        if channel:
            config.channel = channel
    if "webhook_url" in data:
        config.webhook_url = data["webhook_url"]
    if "token" in data:
        config.token = data["token"]
    if "events" in data:
        config.events = data["events"]
    if "is_active" in data:
        config.is_active = data["is_active"]

    db.session.commit()
    return _success(data=config.to_dict(), message="更新成功")


@router.delete("/api/v1/notifications/configs/{config_id}")
@release_session
def delete_notification_config(config_id: int, user: User = Depends(_current_user)):
    """删除通知配置（IDOR：仅本人配置可删，他人配置 404）"""
    user_id = user.id
    config = db.session.scalar(select(NotificationConfig).filter_by(id=config_id, user_id=user_id))
    if not config:
        return _error(404, "通知配置不存在")

    db.session.delete(config)
    db.session.commit()
    return _success(message="删除成功")


@router.post("/api/v1/notifications/configs/{config_id}/test")
@release_session
def test_notification(config_id: int, user: User = Depends(_current_user)):
    """测试发送通知（IDOR：仅本人配置可测，他人配置 404）"""
    user_id = user.id
    config = db.session.scalar(select(NotificationConfig).filter_by(id=config_id, user_id=user_id))
    if not config:
        return _error(404, "通知配置不存在")

    from ....services.notification_service import send_notification

    result = send_notification(
        channel=config.channel,
        webhook_url=config.webhook_url,
        event="test",
        title="通知测试",
        content=f"这是一条来自 大熊AI测试平台 的测试通知（渠道: {config.channel}）",
        token=config.token,
    )

    if result.get("success"):
        return _success(message="测试通知发送成功")
    return _error(500, f'测试通知发送失败: {result.get("error", "未知错误")}')
