"""
平台接口注册表单测（agent 的 search_platform_apis / call_platform_api / load_skill 底层）

目录来自测试 app 的真实 OpenAPI schema；dispatch 走进程内 TestClient（零外呼、零 mock），
属主过滤由路由层真实生效。
"""
import json
import uuid

import pytest

from app.services.ai import platform_api_registry as reg


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    import app.services.rate_limit_service as rate_limit_service
    import app.services.token_blacklist as token_blacklist

    monkeypatch.setattr(token_blacklist, "_get_redis", lambda: None)
    monkeypatch.setattr(rate_limit_service, "_get_redis", lambda: None, raising=False)
    yield


@pytest.fixture
def registry(app, monkeypatch):
    """把注册表指向测试 app 实例并重建目录缓存（隔离全局单例状态）"""
    monkeypatch.setattr(reg, "_get_app", lambda: app)
    reg.get_catalog(force=True)
    return reg


def _uname(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


@pytest.fixture
def make_user(app):
    from app.extensions import db
    from app.models.user import User

    def _make(prefix: str) -> int:
        user = User(
            username=_uname(prefix),
            email=f"{_uname(prefix)}@test.com",
            password_hash="h",
        )
        db.session.add(user)
        db.session.commit()
        return user.id

    return _make


def _make_scenario(user_id: int, name: str) -> int:
    from app.extensions import db
    from app.models.perf_test_scenario import PerfTestScenario

    scenario = PerfTestScenario(
        name=name,
        target_url="http://example.com",
        user_count=5,
        duration=30,
        status="pending",
        user_id=user_id,
    )
    db.session.add(scenario)
    db.session.commit()
    return scenario.id


# ---- 目录构建 ----


def test_catalog_built_from_openapi(registry):
    catalog = reg.get_catalog()
    assert len(catalog) > 100  # 平台 300+ 路由（OpenAPI 侧去 OPTIONS/HEAD）
    for ep in catalog:
        assert ep["method"] in ("GET", "POST", "PUT", "DELETE", "PATCH")
        assert ep["path"].startswith("/")
    assert not any(ep["path"].startswith("/api/v1/ai") for ep in catalog)
    assert not any(ep["path"].startswith("/api/v1/copilot") for ep in catalog)  # 防自递归
    assert not any(ep["path"] == "/metrics" for ep in catalog)


def test_resolve_exact_template(registry):
    entry = reg.resolve_api("GET", "/api/v1/perf-test/scenarios")
    assert entry is not None
    assert entry["method"] == "GET"
    assert reg.resolve_api("GET", "/api/v1/definitely-not-exist") is None


# ---- 检索 ----


def test_search_by_path_and_summary(registry):
    hits = reg.search_apis("scenarios")
    assert hits, "scenarios 应命中 perf-test 接口"
    assert any("/api/v1/perf-test/scenarios" == h["path"] for h in hits)

    hits_cn = reg.search_apis("场景")
    assert hits_cn, "中文摘要检索应命中（获取性能测试场景列表）"


def test_search_limit_and_empty(registry):
    assert len(reg.search_apis("", limit=5)) <= 5
    assert reg.search_apis("zzz-no-such-keyword-zzz") == []


# ---- 分发（进程内真实调用） ----


def test_dispatch_auth_me(registry, make_user):
    uid = make_user("me")
    out = reg.dispatch(uid, "GET", "/api/v1/auth/me")
    assert out["status"] == 200
    assert _find_username(out["body"], uid)


def _find_username(body: str, uid: int) -> bool:
    data = json.loads(body)
    username = (data.get("data") or {}).get("username") or (data.get("data") or {}).get("user", {}).get("username")
    if not username:
        return False
    from app.extensions import db
    from app.models.user import User

    user = db.session.get(User, uid)
    return username == user.username


def test_dispatch_scoped_list_ownership(registry, make_user):
    """属主过滤铁律：user1 的场景列表里没有 user2 的场景"""
    uid1 = make_user("owner")
    uid2 = make_user("other")
    name = _uname("scoped-scenario")
    _make_scenario(uid1, name)

    mine = reg.dispatch(uid1, "GET", "/api/v1/perf-test/scenarios")
    theirs = reg.dispatch(uid2, "GET", "/api/v1/perf-test/scenarios")
    assert mine["status"] == 200 and theirs["status"] == 200
    assert name in mine["body"]
    assert name not in theirs["body"]


def test_dispatch_unknown_path_rejected(registry, make_user):
    with pytest.raises(ValueError, match="不在平台接口目录"):
        reg.dispatch(make_user("u"), "GET", "/api/v1/not/in/catalog")


def test_dispatch_path_values_and_denied_auth(registry, make_user):
    uid = make_user("u")
    name = _uname("detail")
    scenario_id = _make_scenario(uid, name)
    out = reg.dispatch(
        uid, "GET", "/api/v1/perf-test/scenarios/{scenario_id}",
        path_values={"scenario_id": scenario_id},
    )
    assert out["status"] == 200
    assert name in out["body"]

    with pytest.raises(ValueError, match="令牌链路"):
        reg.dispatch(uid, "POST", "/api/v1/auth/login", body={"username": "x", "password": "y"})


def test_dispatch_unfilled_placeholder(registry, make_user):
    with pytest.raises(ValueError, match="占位符"):
        reg.dispatch(make_user("u"), "GET", "/api/v1/perf-test/scenarios/{scenario_id}")


# ---- skills 文档 ----


def test_load_skill_full_doc():
    doc = reg.load_skill_doc()
    assert "工具总览" in doc
    assert "call_platform_api" in doc
    assert "安全边界" in doc


def test_load_skill_section_and_miss():
    section = reg.load_skill_doc("典型工作流")
    assert section.startswith("## 典型工作流")
    assert "R1" in section

    miss = reg.load_skill_doc("不存在的章节xyz")
    assert "未找到章节" in miss
    assert "## 工具总览" in miss  # 列出可用章节


# ---- agent 工具层 ----


def _tool_map(user_id: int):
    from app.services.ai import agent_service

    return {t.name: t for t in agent_service._build_tools(user_id, {})}


def test_agent_tools_full_set(app):
    names = set(_tool_map(1).keys())
    assert {
        "search_platform_apis", "call_platform_api", "load_skill",
        "fetch_api_docs", "probe_api_endpoint",
        "create_performance_test", "generate_performance_script",
        "query_failed_web_tests", "query_recent_api_cases", "list_performance_scenarios",
    } <= names


def test_tool_search_and_call(app, make_user):
    uid = make_user("tool")
    tools = _tool_map(uid)

    search_out = json.loads(tools["search_platform_apis"].invoke({"keyword": "scenarios"}))
    assert search_out["status"] == "success"
    assert search_out["total_in_catalog"] > 100
    assert any(h["path"] == "/api/v1/perf-test/scenarios" for h in search_out["matches"])


def test_tool_call_platform_api_roundtrip(app, make_user):
    uid = make_user("tool")
    name = _uname("tool-scenario")
    scenario_id = _make_scenario(uid, name)
    tools = _tool_map(uid)

    out = json.loads(
        tools["call_platform_api"].invoke(
            {
                "method": "GET",
                "path": "/api/v1/perf-test/scenarios/{scenario_id}",
                "path_values": json.dumps({"scenario_id": scenario_id}),
                "query_json": "",
                "body_json": "",
            }
        )
    )
    assert out["status"] == "success"
    assert out["http_status"] == 200
    assert name in out["body"]


def test_tool_call_error_envelope(app, make_user):
    uid = make_user("tool")
    tools = _tool_map(uid)
    out = json.loads(
        tools["call_platform_api"].invoke({"method": "GET", "path": "/api/v1/no/such/api"})
    )
    assert out["status"] == "error"
    assert "不在平台接口目录" in out["message"]


def test_tool_load_skill(app):
    tools = _tool_map(1)
    out = tools["load_skill"].invoke({"section": "调用规范"})
    assert "call_platform_api" in out
