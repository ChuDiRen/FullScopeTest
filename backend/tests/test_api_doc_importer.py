"""
API 文档导入器单测（agent 的 fetch_api_docs / probe_api_endpoint 底层）

零真实外呼：requests.get / requests.request 全部 monkeypatch；
SSRF 用例走真实 is_safe_url（127.0.0.1 直接 IP 解析，无 DNS 外呼）。
"""
import json
import uuid

import pytest
import requests

from app.services.ai import api_doc_importer as mod

IMPORTER = "app.services.ai.api_doc_importer"

# ---- 迷你 FakeStore 风格 spec（覆盖：query 参数 / path 占位符 / POST 请求体） ----

FIXTURE_SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "FakeStoreAPI", "version": "v2.1.11"},
    "servers": [{"url": "https://api.example.com"}],
    "paths": {
        "/products": {
            "get": {
                "summary": "Retrieve a list of all available products.",
                "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer"}}],
            }
        },
        "/products/{id}": {
            "get": {
                "summary": "Retrieve details of a specific product by ID.",
                "parameters": [
                    {"name": "id", "in": "path", "required": True, "schema": {"type": "integer"}}
                ],
            }
        },
        "/auth/login": {
            "post": {
                "summary": "Authenticate a user.",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {"username": {"type": "string"}, "password": {"type": "string"}},
                            }
                        }
                    }
                },
            }
        },
    },
}

SPEC_TEXT = json.dumps(FIXTURE_SPEC)
DOCS_HTML = "<html><head><script src='/js/redoc.standalone.js'></script></head><body spec-url='/docs-data'></body></html>"
PLAIN_HTML = "<html><body>welcome to docs</body></html>"


class StubResp:
    def __init__(self, status_code=200, text="", content_type="application/json", url=""):
        self.status_code = status_code
        self.text = text
        self.headers = {"Content-Type": content_type}
        self.url = url
        self.content = text.encode("utf-8")
        self.encoding = "utf-8"

    def json(self):
        return json.loads(self.text)


def _fake_get(routes):
    """按前缀路由的 requests.get 替身"""

    def fake_get(url, **kwargs):
        for prefix, resp in routes.items():
            if url.startswith(prefix):
                resp.url = url
                return resp
        return StubResp(404, "not found", content_type="text/html", url=url)

    return fake_get


@pytest.fixture
def pass_safety(monkeypatch):
    """公网域名用例免真实 DNS 解析（SSRF 校验直接放行）"""
    monkeypatch.setattr(mod, "is_safe_url", lambda u: (True, ""))


# ---- fetch_content ----


def test_fetch_content_blocks_internal_url():
    """SSRF 铁律：内网地址直接拦截（真实 is_safe_url，无 DNS）"""
    with pytest.raises(ValueError, match="安全校验拦截"):
        mod.api_doc_importer.fetch_content("http://127.0.0.1:5211/openapi.json")


def test_fetch_content_success(monkeypatch, pass_safety):
    monkeypatch.setattr(
        mod.requests, "get", _fake_get({"https://docs.example.com": StubResp(200, "hello")})
    )
    out = mod.api_doc_importer.fetch_content("https://docs.example.com/docs")
    assert out["status"] == 200
    assert out["body"] == "hello"


# ---- discover_spec ----


def test_discover_spec_direct(pass_safety, monkeypatch):
    """URL 本身就是 spec → 直接命中，无需发现"""
    monkeypatch.setattr(
        mod.requests, "get", _fake_get({"https://docs.example.com": StubResp(200, SPEC_TEXT)})
    )
    out = mod.api_doc_importer.discover_spec("https://docs.example.com/openapi.json")
    assert out["discovered_via"] == "direct"
    assert json.loads(out["raw"])["info"]["title"] == "FakeStoreAPI"


def test_discover_spec_redoc_hint(pass_safety, monkeypatch):
    """Redoc 文档页 → 从 spec-url 属性挖出 /docs-data"""
    monkeypatch.setattr(
        mod.requests,
        "get",
        _fake_get(
            {
                "https://docs.example.com/docs-data": StubResp(200, SPEC_TEXT),
                "https://docs.example.com/docs": StubResp(200, DOCS_HTML, content_type="text/html"),
            }
        ),
    )
    out = mod.api_doc_importer.discover_spec("https://docs.example.com/docs")
    assert out["discovered_via"].startswith("html-hint")
    assert out["spec_url"].endswith("/docs-data")


def test_discover_spec_common_path_fallback(pass_safety, monkeypatch):
    """无提示的普通页面 → 常见路径探测命中 /openapi.json"""
    monkeypatch.setattr(
        mod.requests,
        "get",
        _fake_get(
            {
                "https://docs.example.com/openapi.json": StubResp(200, SPEC_TEXT),
                "https://docs.example.com": StubResp(200, PLAIN_HTML, content_type="text/html"),
            }
        ),
    )
    out = mod.api_doc_importer.discover_spec("https://docs.example.com/docs")
    assert out["discovered_via"] == "common-path: /openapi.json"


def test_discover_spec_not_found(pass_safety, monkeypatch):
    monkeypatch.setattr(
        mod.requests,
        "get",
        _fake_get({"https://docs.example.com": StubResp(200, PLAIN_HTML, content_type="text/html")}),
    )
    with pytest.raises(ValueError, match="无法从"):
        mod.api_doc_importer.discover_spec("https://docs.example.com/docs")


# ---- parse_inventory ----


def test_parse_inventory_endpoints_and_base_url():
    inventory = mod.api_doc_importer.parse_inventory(SPEC_TEXT)
    assert inventory["base_url"] == "https://api.example.com"
    assert inventory["endpoints_count"] == 3
    by_path = {ep["path"]: ep for ep in inventory["endpoints"]}
    assert by_path["/products"]["method"] == "GET"
    assert by_path["/products"]["params"][0]["name"] == "limit"
    assert by_path["/products/{id}"]["params"][0]["required"] is True
    assert by_path["/auth/login"]["method"] == "POST"
    assert by_path["/auth/login"]["request_body"]["properties"]["username"]["type"] == "string"


def test_parse_inventory_swagger2_host_base_url():
    """Swagger 2.0：host + basePath 推导基地址"""
    spec = {
        "swagger": "2.0",
        "info": {"title": "legacy", "version": "1.0"},
        "host": "api.old.com",
        "basePath": "/v2",
        "schemes": ["https"],
        "paths": {"/users": {"get": {"summary": "list users"}}},
    }
    inventory = mod.api_doc_importer.parse_inventory(json.dumps(spec))
    assert inventory["base_url"] == "https://api.old.com/v2"
    assert inventory["endpoints"][0]["path"] == "/users"


# ---- probe_endpoint ----


def test_probe_endpoint_post_body_and_latency(monkeypatch, pass_safety):
    calls = {}

    def fake_request(method, url, **kwargs):
        calls.update(method=method, url=url, params=kwargs.get("params"), json_body=kwargs.get("json"))
        return StubResp(200, json.dumps({"token": "abc.fake"}), url=url)

    monkeypatch.setattr(mod.requests, "request", fake_request)
    out = mod.api_doc_importer.probe_endpoint(
        "https://api.example.com",
        "post",
        "/auth/login",
        body={"username": "mor", "password": "x"},
    )
    assert out["status"] == 200
    assert "token" in out["body_preview"]
    assert out["latency_ms"] >= 0
    assert calls["method"] == "POST"
    assert calls["json_body"] == {"username": "mor", "password": "x"}


def test_probe_endpoint_substitutes_path_values(monkeypatch, pass_safety):
    seen = {}

    def fake_request(method, url, **kwargs):
        seen["url"] = url
        return StubResp(200, '{"id": 7}', url=url)

    monkeypatch.setattr(mod.requests, "request", fake_request)
    mod.api_doc_importer.probe_endpoint(
        "https://api.example.com", "GET", "/products/{id}", path_values={"id": 7}
    )
    assert seen["url"] == "https://api.example.com/products/7"


def test_probe_endpoint_unfilled_placeholder(pass_safety):
    with pytest.raises(ValueError, match="占位符"):
        mod.api_doc_importer.probe_endpoint("https://api.example.com", "GET", "/products/{id}")


def test_probe_endpoint_ssrf_blocked():
    with pytest.raises(ValueError, match="安全校验拦截"):
        mod.api_doc_importer.probe_endpoint("http://127.0.0.1:5211", "GET", "/products")


# ---- agent 工具层 ----


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    import app.services.rate_limit_service as rate_limit_service
    import app.services.token_blacklist as token_blacklist

    monkeypatch.setattr(token_blacklist, "_get_redis", lambda: None)
    monkeypatch.setattr(rate_limit_service, "_get_redis", lambda: None, raising=False)
    yield


def _build_tool_map():
    from app.services.ai import agent_service

    return {t.name: t for t in agent_service._build_tools(1, {})}


def test_agent_tools_include_doc_tools(app):
    tools = _build_tool_map()
    assert "fetch_api_docs" in tools
    assert "probe_api_endpoint" in tools
    assert len(tools) == 7


def test_tool_fetch_api_docs(app, monkeypatch, pass_safety):
    monkeypatch.setattr(
        mod.requests,
        "get",
        _fake_get(
            {
                "https://docs.example.com/docs-data": StubResp(200, SPEC_TEXT),
                "https://docs.example.com/docs": StubResp(200, DOCS_HTML, content_type="text/html"),
            }
        ),
    )
    out = json.loads(_build_tool_map()["fetch_api_docs"].invoke({"url": "https://docs.example.com/docs"}))
    assert out["status"] == "success"
    assert out["spec_url"].endswith("/docs-data")
    assert out["endpoints_count"] == 3
    assert out["endpoints_truncated"] is False


def test_tool_fetch_api_docs_network_error_returns_error_json(app, monkeypatch, pass_safety):
    def boom(url, **kwargs):
        raise requests.exceptions.ConnectionError("connection refused")

    monkeypatch.setattr(mod.requests, "get", boom)
    out = json.loads(_build_tool_map()["fetch_api_docs"].invoke({"url": "https://docs.example.com/docs"}))
    assert out["status"] == "error"
    assert "connection refused" in out["message"]


def test_tool_probe_api_endpoint(app, monkeypatch, pass_safety):
    monkeypatch.setattr(
        mod.requests,
        "request",
        lambda method, url, **kwargs: StubResp(200, '{"ok": true}', url=url),
    )
    out = json.loads(
        _build_tool_map()["probe_api_endpoint"].invoke(
            {
                "base_url": "https://api.example.com",
                "method": "GET",
                "path": "/products/{id}",
                "path_values": '{"id": 3}',
                "query_params": "",
                "body_json": "",
            }
        )
    )
    assert out["status"] == "success"
    assert out["http_status"] == 200
    assert out["body_preview"] == '{"ok": true}'


def test_tool_probe_api_endpoint_bad_json_arg(app, monkeypatch, pass_safety):
    out = json.loads(
        _build_tool_map()["probe_api_endpoint"].invoke(
            {"base_url": "https://api.example.com", "method": "POST", "path": "/auth/login",
             "path_values": "", "query_params": "", "body_json": "{bad json"}
        )
    )
    assert out["status"] == "error"
    assert "body_json" in out["message"]
