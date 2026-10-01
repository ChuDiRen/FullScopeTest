"""
工具函数模块（零 Flask）
"""


def get_current_user_id():
    """
    获取当前请求的用户 ID（由 RequestContextMiddleware 解码 JWT 后填入 request_local）

    Returns:
        int | None: 用户 ID；无请求上下文或未认证时返回 None
    """
    from .core.request_local import get_request_info

    info = get_request_info() or {}
    identity = info.get("jwt_identity")
    try:
        return int(identity) if identity is not None else None
    except (TypeError, ValueError):
        return None
