"""
API 文档导入器 —— 从文档 URL 自动发现并解析 OpenAPI/Swagger 规范 + 接口探针

流水线（对应智能体 fetch_api_docs / probe_api_endpoint 工具）：

    文档 URL → 内容嗅探（本身是 spec?）→ HTML 提取 spec 地址（Redoc spec-url /
    SwaggerUIBundle url / Redoc.init）→ 常见 spec 路径探测（/openapi.json 等）
    → 解析为端点清单（复用 SwaggerCaseGeneratorService 的 $ref 展开能力）

发现/解析/探针全部是确定性代码，不消耗模型 token；端点清单交给 LLM 做
业务场景映射。所有外发请求过 url_safety.is_safe_url（SSRF 铁律，与
perf_test 路由同一道闸），重定向后的最终 URL 也要复查。
"""

import json
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests

from ...utils.url_safety import is_safe_url
from .swagger_case_generator import SwaggerCaseGeneratorService
from ...core.logging import get_logger

logger = get_logger(__name__)

DEFAULT_TIMEOUT = 10
SPEC_MAX_BYTES = 5 * 1024 * 1024
PROBE_BODY_PREVIEW = 800

# 文档页里常见的 spec 声明方式（Redoc / Swagger UI / 通用嵌入）
_SPEC_HINT_PATTERNS = [
    re.compile(r"""spec-url\s*=\s*['"]([^'"]+)['"]"""),              # Redoc: <redoc spec-url=''>
    re.compile(r"""Redoc\.init\(\s*['"]([^'"]+)['"]"""),              # Redoc.init('spec', ...)
    re.compile(r"""url\s*:\s*['"]([^'"]+\.(?:json|yaml|yml))['"]"""),  # SwaggerUIBundle({url: '...'})
    re.compile(r"""['"]([^'"]*(?:openapi|swagger|api-docs)[^'"]*\.(?:json|yaml|yml))['"]""", re.I),
]

# 无提示时按序探测的常见 spec 路径
COMMON_SPEC_PATHS = [
    "/openapi.json",
    "/swagger.json",
    "/v3/api-docs",
    "/v2/swagger.json",
    "/api-docs",
    "/docs-data",
    "/swagger/doc.json",
    "/api/swagger.json",
    "/openapi.yaml",
]

_UA = {"User-Agent": "FullScopeTest-DocImporter/1.0"}
_NET_ERRORS = (ValueError, requests.exceptions.RequestException)


def _looks_like_spec(text: str) -> bool:
    """内容嗅探：JSON/YAML 且带 openapi/swagger 标识与 paths 定义"""
    if not text:
        return False
    sample = text.lstrip()[:200].lower()
    if sample.startswith(("{", "[")):
        try:
            data = json.loads(text[:SPEC_MAX_BYTES])
        except (json.JSONDecodeError, ValueError):
            return False
        return isinstance(data, dict) and "paths" in data and ("openapi" in data or "swagger" in data)
    head = text[:2000].lower()
    return ("openapi:" in head or "swagger:" in head) and "paths:" in head


def _spec_or_none(text: str) -> Optional[str]:
    return text if _looks_like_spec(text) else None


class ApiDocImporterService:
    """API 文档抓取/发现/解析/探针（无 AI 调用，纯确定性逻辑）"""

    def __init__(self):
        self._case_gen = SwaggerCaseGeneratorService()

    # ---- 抓取 ----

    def fetch_content(self, url: str, *, timeout: int = DEFAULT_TIMEOUT,
                      max_bytes: int = SPEC_MAX_BYTES) -> Dict[str, Any]:
        """SSRF 防护下的 GET 抓取，返回 {status, content_type, body, final_url}"""
        safe, reason = is_safe_url(url)
        if not safe:
            raise ValueError(f"目标地址被安全校验拦截: {reason}")
        resp = requests.get(url, timeout=timeout, headers=_UA, allow_redirects=True)
        # 重定向后的最终地址复查（防 302 跳内网）
        final_url = resp.url or url
        if final_url != url:
            safe, reason = is_safe_url(final_url)
            if not safe:
                raise ValueError(f"重定向目标被安全校验拦截: {reason}")
        body = resp.content[:max_bytes].decode(resp.encoding or "utf-8", errors="replace")
        return {
            "status": resp.status_code,
            "content_type": resp.headers.get("Content-Type", ""),
            "body": body,
            "final_url": final_url,
        }

    # ---- spec 发现 ----

    def discover_spec(self, page_url: str, *, timeout: int = DEFAULT_TIMEOUT) -> Dict[str, Any]:
        """
        从文档 URL 发现 OpenAPI/Swagger 规范。

        顺序：URL 本身是 spec → HTML 提示 → 常见路径探测。
        Returns: {"spec_url", "format", "raw", "discovered_via"}
        Raises: ValueError（发现不了 / 安全拦截 / 网络失败）
        """
        page = self.fetch_content(page_url, timeout=timeout)
        if page["status"] != 200:
            raise ValueError(f"文档地址返回 HTTP {page['status']}: {page_url}")

        direct = _spec_or_none(page["body"])
        if direct is not None:
            return {
                "spec_url": page["final_url"],
                "format": self._sniff_format(direct),
                "raw": direct,
                "discovered_via": "direct",
            }

        origin = f"{urlparse(page['final_url']).scheme}://{urlparse(page['final_url']).netloc}"

        # 1) HTML 提示（Redoc / Swagger UI）
        for pattern in _SPEC_HINT_PATTERNS:
            match = pattern.search(page["body"])
            if not match:
                continue
            candidate = urljoin(page["final_url"], match.group(1))
            try:
                fetched = self.fetch_content(candidate, timeout=timeout)
            except _NET_ERRORS as exc:
                logger.warning("spec 候选地址抓取失败", url=candidate, error=str(exc))
                continue
            spec_text = _spec_or_none(fetched["body"])
            if spec_text is not None and fetched["status"] == 200:
                return {
                    "spec_url": fetched["final_url"],
                    "format": self._sniff_format(spec_text),
                    "raw": spec_text,
                    "discovered_via": f"html-hint: {match.group(0)[:60]}",
                }

        # 2) 常见路径探测
        for path in COMMON_SPEC_PATHS:
            candidate = origin + path
            try:
                fetched = self.fetch_content(candidate, timeout=min(timeout, 5))
            except _NET_ERRORS:
                continue
            spec_text = _spec_or_none(fetched["body"])
            if spec_text is not None and fetched["status"] == 200:
                return {
                    "spec_url": fetched["final_url"],
                    "format": self._sniff_format(spec_text),
                    "raw": spec_text,
                    "discovered_via": f"common-path: {path}",
                }

        raise ValueError(
            f"无法从 {page_url} 发现 OpenAPI/Swagger 规范：页面无 spec 声明，"
            f"常见路径（{', '.join(COMMON_SPEC_PATHS[:5])}…）也未命中。"
            "请确认这是 API 文档页，或直接提供 openapi.json / swagger.json 地址。"
        )

    @staticmethod
    def _sniff_format(text: str) -> str:
        return "yaml" if text.lstrip().startswith(("openapi:", "swagger:")) else "json"

    # ---- 解析 ----

    def parse_inventory(self, raw: str) -> Dict[str, Any]:
        """spec 原文 → 端点清单（压缩格式，供 LLM 消费）"""
        fmt = self._sniff_format(raw)
        spec = self._case_gen.parse_swagger(raw, fmt)

        info = spec.get("info", {})
        endpoints = self._case_gen.extract_endpoints(spec)

        return {
            "spec_info": {
                "title": info.get("title", "Unknown API"),
                "version": str(info.get("version", "unknown")),
                "description": (info.get("description") or "")[:300],
            },
            "base_url": self._derive_base_url(spec),
            "endpoints_count": len(endpoints),
            "endpoints": [self._compact_endpoint(ep) for ep in endpoints[:80]],
        }

    @staticmethod
    def _derive_base_url(spec: Dict[str, Any]) -> str:
        """从 spec 推导探针基地址：OAS3 servers / Swagger2 host+basePath"""
        servers = spec.get("servers") or []
        if servers and isinstance(servers, list) and servers[0].get("url"):
            return str(servers[0]["url"]).rstrip("/")
        host = spec.get("host")
        if host:
            schemes = spec.get("schemes") or ["https"]
            scheme = schemes[0] if schemes else "https"
            return f"{scheme}://{host}{str(spec.get('basePath') or '').rstrip('/')}"
        return ""

    @staticmethod
    def _compact_endpoint(ep: Dict[str, Any]) -> Dict[str, Any]:
        params = [
            {
                "name": p.get("name", ""),
                "in": p.get("in", "query"),
                "required": bool(p.get("required")),
                "type": p.get("type", "string"),
            }
            for p in ep.get("parameters", [])
        ]
        body = ep.get("request_body")
        return {
            "method": ep["method"],
            "path": ep["path"],
            "summary": (ep.get("summary") or ep.get("description") or "")[:150],
            "params": params,
            "request_body": body.get("schema") if body else None,
            "responses": sorted(ep.get("responses", {}).keys())[:6],
        }

    # ---- 探针 ----

    def probe_endpoint(
        self,
        base_url: str,
        method: str,
        path: str,
        *,
        path_values: Optional[Dict[str, Any]] = None,
        query_params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        body: Optional[Any] = None,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> Dict[str, Any]:
        """
        对目标接口发一次真实请求，验证可用性与响应结构（压测前探活）。

        Returns: {status, latency_ms, content_type, body_preview}
        """
        method = (method or "GET").upper()
        if method not in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
            raise ValueError(f"不支持的 HTTP 方法: {method}")

        # {param} 占位符填充
        for key, value in (path_values or {}).items():
            path = path.replace("{" + key + "}", str(value))
        leftover = re.findall(r"\{([^}]+)\}", path)
        if leftover:
            raise ValueError(f"path 中仍有未填充的占位符: {leftover}，请用 path_values 提供")

        if not path.startswith("/"):
            path = "/" + path
        url = base_url.rstrip("/") + path

        safe, reason = is_safe_url(url)
        if not safe:
            raise ValueError(f"目标地址被安全校验拦截: {reason}")

        started = time.monotonic()
        resp = requests.request(
            method,
            url,
            params=query_params or None,
            headers={**_UA, **(headers or {})},
            json=body if method in ("POST", "PUT", "PATCH", "DELETE") else None,
            timeout=timeout,
            allow_redirects=True,
        )
        latency_ms = int((time.monotonic() - started) * 1000)

        final_url = resp.url or url
        safe, reason = is_safe_url(final_url)
        if not safe:
            raise ValueError(f"重定向目标被安全校验拦截: {reason}")

        return {
            "status": resp.status_code,
            "latency_ms": latency_ms,
            "content_type": resp.headers.get("Content-Type", ""),
            "body_preview": resp.text[:PROBE_BODY_PREVIEW],
        }


# 模块级单例
api_doc_importer = ApiDocImporterService()
