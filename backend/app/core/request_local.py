"""
请求本地上下文（零 Flask）

原 Flask 提供 thread-local 的全局 request 代理，服务层/工具层可以随时读取
IP、User-Agent、请求体等。ASGI（FastAPI）世界没有全局 request 对象，因此
这里提供一个按请求填充的 ContextVar 槽位：

- ASGI 中间件在请求入口调用 set_request_info()，请求结束后 reset
- 服务层/工具层通过 get_request_info() 读取；未设置时返回 None，
  等价于原 Flask 的"脱离请求上下文"路径（调用方各自降级处理）

键约定（中间件按需填充）：
    method / path / url / endpoint / client_ip / user_agent / headers /
    query_params / json
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any, Dict, Optional

_request_info: ContextVar[Optional[Dict[str, Any]]] = ContextVar(
    "request_info", default=None
)


def set_request_info(info: Dict[str, Any]) -> Token:
    """写入当前请求的摘要信息（由请求入口/中间件调用），返回 token 供 reset"""
    return _request_info.set(dict(info or {}))


def get_request_info() -> Optional[Dict[str, Any]]:
    """读取当前请求摘要信息；无请求时返回 None"""
    return _request_info.get()


def reset_request_info(token: Token) -> None:
    """请求结束时恢复上下文（由请求出口/中间件调用）"""
    _request_info.reset(token)
