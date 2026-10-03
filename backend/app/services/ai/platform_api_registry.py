"""
平台接口注册表 —— 把平台全部 API 变成智能体可调用的工具

设计（为什么不生成 300 个工具 schema）：
300+ 条路由逐一注册成独立工具会撑爆模型上下文（仅工具定义就要 15 万+ token），
DeepSeek 根本选不过来。这里用"目录检索 + 通用分发"两件套：

    search_platform_apis(keyword)  → 按关键字查接口目录（拿路径/参数定义）
    call_platform_api(method, path, ...) → 以当前用户身份进程内调用任意目录接口

目录从 FastAPI app 的 OpenAPI schema 自动提取（路由 100% 自洽，新增接口零维护），
进程内缓存。分发走 TestClient 同步 ASGI（copilot 路由是 sync def 线程池，无事件循环冲突），
复用全部中间件与鉴权依赖——JWT 以当前用户身份现场签发，属主过滤/越权 404 由
各路由自身保证，工具层不做任何权限放大。

安全边界：
- /api/v1/ai* 全排除（防智能体自递归烧 token）
- auth 令牌链路（登录/注册/刷新/找回）拉黑（智能体已持有身份，无业务价值且防滥用）
- path 模板必须精确命中目录（防编造路径）
"""

import re
import threading
from typing import Any, Dict, List, Optional

from ...core.logging import get_logger

logger = get_logger(__name__)

# AI 自身路由排除（防自递归）；健康检查类无调用价值
EXCLUDED_PREFIXES = ("/api/v1/ai", "/api/v1/copilot")
EXCLUDED_EXACT = {"/metrics", "/health", "/health/live", "/health/ready", "/api/v1/api-test/health"}

# 令牌链路拉黑（智能体已持身份，登录/注册/刷新/找回对它无意义）
DENIED_CALLS = {
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/register"),
    ("POST", "/api/v1/auth/refresh"),
    ("POST", "/api/v1/auth/logout"),
    ("POST", "/api/v1/auth/forgot-password"),
    ("POST", "/api/v1/auth/reset-password"),
    ("POST", "/api/v1/auth/sso/ldap/login"),
    ("POST", "/api/v1/auth/sso/oidc/callback"),
}

_HTTP_METHODS = ("get", "post", "put", "delete", "patch")
_BODY_PREVIEW_LIMIT = 3000

_catalog_cache: Optional[List[Dict[str, Any]]] = None
_catalog_lock = threading.Lock()


def _get_app():
    from ...fastapi_app import app

    return app


def get_catalog(force: bool = False) -> List[Dict[str, Any]]:
    """提取平台全部接口目录（进程内缓存；测试可用 force=True 重建）"""
    global _catalog_cache
    if _catalog_cache is not None and not force:
        return _catalog_cache
    with _catalog_lock:
        if _catalog_cache is not None and not force:
            return _catalog_cache

        schema = _get_app().openapi()
        entries: List[Dict[str, Any]] = []
        for path, methods in (schema.get("paths") or {}).items():
            if path in EXCLUDED_EXACT or any(path.startswith(p) for p in EXCLUDED_PREFIXES):
                continue
            for method, op in methods.items():
                if method not in _HTTP_METHODS:
                    continue
                params = [
                    {
                        "name": p.get("name", ""),
                        "in": p.get("in", "query"),
                        "required": bool(p.get("required")),
                        "type": (p.get("schema") or {}).get("type", "string"),
                    }
                    for p in op.get("parameters", [])
                ]
                request_body = op.get("requestBody") or {}
                entries.append(
                    {
                        "method": method.upper(),
                        "path": path,
                        # FastAPI 会按函数名自动生成英文 summary，中文 docstring 在 description 里
                        "summary": (op.get("summary") or "")[:120],
                        "description": (op.get("description") or "")[:150],
                        "tag": (op.get("tags") or [""])[0],
                        "params": params,
                        "body_required": bool(request_body.get("required")),
                    }
                )
        _catalog_cache = entries
        logger.info("平台接口目录已构建", count=len(entries))
        return _catalog_cache


def search_apis(keyword: str, limit: int = 15) -> List[Dict[str, Any]]:
    """按关键字检索接口目录（匹配 path / summary / description / tag）"""
    catalog = get_catalog()
    kw = (keyword or "").strip().lower()
    if not kw:
        return catalog[: max(1, min(int(limit), 30))]
    hits = [
        ep
        for ep in catalog
        if kw in ep["path"].lower()
        or kw in ep["summary"].lower()
        or kw in ep["description"].lower()
        or kw in ep["tag"].lower()
    ]
    return hits[: max(1, min(int(limit), 30))]


def resolve_api(method: str, path_template: str) -> Optional[Dict[str, Any]]:
    """精确匹配目录条目（method + path 模板）"""
    method = (method or "").upper()
    template = path_template if path_template.startswith("/") else "/" + path_template
    for ep in get_catalog():
        if ep["method"] == method and ep["path"] == template:
            return ep
    return None


def dispatch(
    user_id: int,
    method: str,
    path_template: str,
    *,
    path_values: Optional[Dict[str, Any]] = None,
    query: Optional[Dict[str, Any]] = None,
    body: Optional[Any] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    """
    以 user_id 身份进程内调用平台接口（完整中间件 + 鉴权链）。

    Returns: {"status": HTTP状态码, "content_type", "body"(截断)}
    Raises: ValueError（目录未命中 / 拉黑 / 占位符缺失 / 调用失败）
    """
    entry = resolve_api(method, path_template)
    if entry is None:
        raise ValueError(
            f"接口 {method.upper()} {path_template} 不在平台接口目录中，"
            "先用 search_platform_apis 查询正确的路径模板"
        )
    if (entry["method"], entry["path"]) in DENIED_CALLS:
        raise ValueError(f"接口 {entry['method']} {entry['path']} 属于令牌链路，已禁止智能体调用")

    real_path = entry["path"]
    for key, value in (path_values or {}).items():
        real_path = real_path.replace("{" + key + "}", str(value))
    leftover = re.findall(r"\{([^}]+)\}", real_path)
    if leftover:
        raise ValueError(f"path 中仍有未填充的占位符: {leftover}，请用 path_values 提供")

    from ...core.jwt import create_access_token
    from fastapi.testclient import TestClient

    headers = {"Authorization": f"Bearer {create_access_token(str(user_id))}"}
    try:
        client = TestClient(_get_app())
        resp = client.request(
            entry["method"],
            real_path,
            params=query or None,
            json=body if body is not None and entry["method"] in ("POST", "PUT", "PATCH", "DELETE") else None,
            headers=headers,
            timeout=timeout,
        )
    finally:
        try:
            client.close()
        except Exception:
            pass

    return {
        "status": resp.status_code,
        "content_type": resp.headers.get("Content-Type", ""),
        "body": resp.text[:_BODY_PREVIEW_LIMIT],
    }


def load_skill_doc(section: str = "") -> str:
    """
    读取平台能力 skills 文档；section 按 '## ' 标题前缀匹配返回单节，空返回全文。
    """
    from pathlib import Path

    skill_path = Path(__file__).parent / "skills" / "PLATFORM_API_SKILL.md"
    try:
        text = skill_path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"skills 文档读取失败: {exc}"

    section = (section or "").strip()
    if not section:
        return text

    blocks = re.split(r"(?=^## )", text, flags=re.M)
    for block in blocks:
        if block.lstrip().startswith("## ") and section.lower() in block.splitlines()[0].lower():
            return block.strip()
    return f"未找到章节「{section}」。可用章节：\n" + "\n".join(
        b.splitlines()[0] for b in blocks if b.lstrip().startswith("## ")
    )
