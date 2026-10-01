"""
AI 五模块 v1 平迁路由测试（FastAPI 实现，自 Flask 蓝图平迁）

覆盖模块与源蓝图：
- app/api/ai_stats.py            → app/api/v2/v1/ai_stats.py
- app/api/prompt_versions.py     → app/api/v2/v1/prompt_versions.py
- app/api/ai_prompt_versions.py  → app/api/v2/v1/ai_prompt_versions.py（独有 /stats 路由）
- app/api/ai_copilot.py          → app/api/v2/v1/ai_copilot.py
- app/api/semantic_dedup.py      → app/api/v2/v1/semantic_dedup.py

覆盖场景：
1. 未登录访问五模块端点 → 401（参数化，五模块各抽代表端点）
2. Prompt 版本 创建 → 列表 → 激活 → A/B 选择 → 统计刷新 → 停用 全链路
3. IDOR：他人创建的 Prompt 版本 / 他人项目（语义去重）→ 404
4. Copilot 聊天 mock AI 客户端（零真实外呼、零 API key 依赖）→ 结构正确
5. AI 统计按属主（AIInvocationLog.user_id）过滤、形状与 v1 一致
"""
from sqlalchemy import func

import gc
import uuid

import pytest
from sqlalchemy import select
from app.extensions import db
from sqlalchemy import update


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    """本机 Redis 挂死：把 token 黑名单/限流服务的 _get_redis 打桩为 None。

    token_blacklist 与 rate_limit_service 对 Redis 不可用均有文档化的降级路径
    （黑名单放行、token 版本视为有效、限流放行），打桩后 JWT 签发/校验全程零 socket。
    deps.py 与两个服务均为调用期惰性取连接，patch 模块属性即可生效。
    """
    import app.services.rate_limit_service as rate_limit_service
    import app.services.token_blacklist as token_blacklist

    monkeypatch.setattr(token_blacklist, "_get_redis", lambda: None)
    monkeypatch.setattr(rate_limit_service, "_get_redis", lambda: None, raising=False)
    yield


def _uname(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------------------
# 数据工厂
# ---------------------------------------------------------------------------


def _make_project(app, owner_id: int) -> int:
    from app.models.project import Project
    project = Project(
        name=f"proj_{owner_id}_{uuid.uuid4().hex[:8]}",
        owner_id=owner_id,
    )
    db.session.add(project)
    db.session.commit()
    return project.id


def _make_invocation(app, user_id: int, **extra) -> int:
    from app.models.ai_invocation_log import AIInvocationLog
    total_tokens = extra.pop("total_tokens", 0)
    log = AIInvocationLog(
        user_id=user_id,
        feature=extra.pop("feature", "copilot"),
        prompt="test prompt",
        model_name="deepseek-chat",
        success=extra.pop("success", True),
        prompt_tokens=extra.pop("prompt_tokens", total_tokens // 2),
        completion_tokens=extra.pop("completion_tokens", total_tokens // 3),
        cost_estimate=extra.pop("cost_estimate", 0.01),
        total_tokens=total_tokens,
        **extra,
    )
    db.session.add(log)
    db.session.commit()
    return log.id


def _next_version(app, feature: str) -> int:
    from app.models.prompt_version import PromptVersion
    from sqlalchemy import func, select
    current = db.session.scalar(
        select(func.max(PromptVersion.version)).filter_by(feature=feature)
    )
    return (current or 0) + 1


# ---------------------------------------------------------------------------
# 1) 未登录 401（五模块各抽端点，参数化）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        # ai_stats
        ("get", "/api/v1/ai/stats/overview"),
        ("get", "/api/v1/ai/stats/success-rate-trend"),
        ("get", "/api/v1/ai/stats/latency-trend"),
        ("get", "/api/v1/ai/stats/token-consumption"),
        ("get", "/api/v1/ai/stats/prompt-versions-comparison"),
        # prompt_versions
        ("get", "/api/v1/ai/prompt-versions"),
        ("post", "/api/v1/ai/prompt-versions"),
        ("get", "/api/v1/ai/prompt-versions/1"),
        ("put", "/api/v1/ai/prompt-versions/1"),
        ("delete", "/api/v1/ai/prompt-versions/1"),
        ("post", "/api/v1/ai/prompt-versions/select"),
        ("post", "/api/v1/ai/prompt-versions/refresh-stats"),
        # ai_prompt_versions（独有 stats 路由）
        ("get", "/api/v1/ai/prompt-versions/1/stats"),
        # ai_copilot
        ("post", "/api/v1/copilot/chat"),
        # semantic_dedup
        ("post", "/api/v1/ai/find-duplicates"),
    ],
)
def test_unauthenticated_returns_401(v2_client, method, path):
    resp = v2_client.request(method.upper(), path, json={})
    assert resp.status_code == 401, resp.text
    body = resp.json()
    assert body["code"] == 401


# ---------------------------------------------------------------------------
# 2) Prompt 版本 创建 → 列表 → 激活（全链路）
# ---------------------------------------------------------------------------


def test_prompt_version_create_list_activate(v2_client, make_user, auth_headers, app):
    uid = make_user(_uname("pvown"))
    headers = auth_headers(uid)
    base = "/api/v1/ai/prompt-versions"

    v_expected = _next_version(app, "copilot")

    # ---- 创建（v1 信封：code=201，HTTP 201）----
    resp = v2_client.post(
        base,
        headers=headers,
        json={
            "feature": "copilot",
            "name": "baseline",
            "system_prompt": "you are a tester",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 200
    assert body["message"] == "Prompt 版本创建成功"
    pv1 = body["data"]
    assert pv1["version"] == v_expected
    assert pv1["is_active"] is False
    assert pv1["system_prompt"] == "you are a tester"
    assert pv1["created_by"] == uid
    assert pv1["id"] > 0

    # ---- 创建第二个版本（同 feature 自增；直接以激活态创建）----
    resp = v2_client.post(
        base,
        headers=headers,
        json={
            "feature": "copilot",
            "name": "experiment-a",
            "system_prompt": "you are a better tester",
            "is_active": True,
            "traffic_weight": 0.5,
        },
    )
    assert resp.status_code == 200, resp.text
    pv2 = resp.json()["data"]
    assert pv2["version"] == v_expected + 1
    assert pv2["is_active"] is True
    ids = {pv1["id"], pv2["id"]}

    # ---- 必填校验（400，与 v1 文案一致）----
    for payload, msg in [
        ({"name": "x", "system_prompt": "s"}, "feature is required"),
        ({"feature": "copilot", "system_prompt": "s"}, "name is required"),
        ({"feature": "copilot", "name": "x"}, "system_prompt is required"),
    ]:
        resp = v2_client.post(base, headers=headers, json=payload)
        assert resp.status_code == 400, resp.text
        assert resp.json()["message"] == msg

    # ---- feature 白名单校验 ----
    resp = v2_client.post(
        base,
        headers=headers,
        json={"feature": "nope", "name": "x", "system_prompt": "s"},
    )
    assert resp.status_code == 400, resp.text
    assert "feature must be one of" in resp.json()["message"]

    # ---- 列表 ----
    resp = v2_client.get(f"{base}?feature=copilot", headers=headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["pagination"]["per_page"] == 20
    got_ids = {item["id"] for item in data["items"]}
    assert ids <= got_ids
    for item in data["items"]:
        assert item["feature"] == "copilot"

    # ---- is_active 过滤 ----
    resp = v2_client.get(f"{base}?feature=copilot&is_active=true", headers=headers)
    items = resp.json()["data"]["items"]
    assert pv2["id"] in {item["id"] for item in items}
    assert all(item["is_active"] for item in items)

    # ---- 激活 baseline（PUT is_active=true）----
    resp = v2_client.put(
        f"{base}/{pv1['id']}", headers=headers, json={"is_active": True}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["message"] == "Prompt 版本更新成功"
    assert resp.json()["data"]["is_active"] is True

    # ---- 详情确认已激活 ----
    resp = v2_client.get(f"{base}/{pv1['id']}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["data"]["is_active"] is True

    # ---- A/B 选择（激活版本池中随机，返回其一）----
    resp = v2_client.post(f"{base}/select", headers=headers, json={"feature": "copilot"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["id"] in ids

    # select 缺 feature → 400
    resp = v2_client.post(f"{base}/select", headers=headers, json={})
    assert resp.status_code == 400
    assert resp.json()["message"] == "feature is required"

    # select 无激活版本 → 404
    resp = v2_client.post(
        f"{base}/select", headers=headers, json={"feature": "swagger_gen"}
    )
    assert resp.status_code == 404, resp.text

    # ---- 版本详细统计（ai_prompt_versions 模块独有路由）----
    resp = v2_client.get(f"{base}/{pv1['id']}/stats", headers=headers)
    assert resp.status_code == 200, resp.text
    stats = resp.json()["data"]
    assert stats["id"] == pv1["id"]
    assert stats["recent_trend"] == []

    # 有调用日志后，趋势进入 stats
    _make_invocation(
        app,
        uid,
        feature="copilot",
        success=True,
        latency_ms=120,
        total_tokens=80,
        cost_estimate=0.02,
        prompt_version_id=pv1["id"],
    )
    resp = v2_client.get(f"{base}/{pv1['id']}/stats", headers=headers)
    trend = resp.json()["data"]["recent_trend"]
    assert len(trend) == 1
    assert trend[0]["success"] is True
    assert trend[0]["latency_ms"] == 120
    assert trend[0]["total_tokens"] == 80

    # ---- 统计刷新 ----
    resp = v2_client.post(f"{base}/refresh-stats?feature=copilot", headers=headers)
    assert resp.status_code == 200, resp.text
    refreshed = resp.json()["data"]["refreshed_count"]
    assert refreshed >= 2

    # 刷新后版本聚合统计正确（1 次成功调用）
    resp = v2_client.get(f"{base}/{pv1['id']}", headers=headers)
    refreshed_pv = resp.json()["data"]
    assert refreshed_pv["total_invocations"] == 1
    assert refreshed_pv["success_count"] == 1

    # ---- 停用（DELETE = 软删除）----
    resp = v2_client.delete(f"{base}/{pv1['id']}", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["message"] == "Prompt 版本已停用"

    resp = v2_client.get(f"{base}/{pv1['id']}", headers=headers)
    deactivated = resp.json()["data"]
    assert deactivated["is_active"] is False
    assert deactivated["deactivated_at"] is not None

    # 删除不存在的版本 → 404
    resp = v2_client.delete(f"{base}/99999999", headers=headers)
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# 3) IDOR：他人资源 404
# ---------------------------------------------------------------------------


def test_prompt_version_other_user_404(v2_client, make_user, auth_headers):
    uid_a = make_user(_uname("pvua"))
    uid_b = make_user(_uname("pvub"))
    headers_a = auth_headers(uid_a)
    headers_b = auth_headers(uid_b)
    base = "/api/v1/ai/prompt-versions"

    # A 创建并激活自己的版本
    resp = v2_client.post(
        base,
        headers=headers_a,
        json={
            "feature": "script_gen",
            "name": "a-private",
            "system_prompt": "a secret prompt",
            "is_active": True,
        },
    )
    assert resp.status_code == 200, resp.text
    pv_a = resp.json()["data"]

    # B 直接访问 A 的版本：读/改/停/统计 全部 404
    resp = v2_client.get(f"{base}/{pv_a['id']}", headers=headers_b)
    assert resp.status_code == 404, resp.text
    assert resp.json()["message"] == "Prompt 版本不存在"

    resp = v2_client.put(
        f"{base}/{pv_a['id']}", headers=headers_b, json={"name": "hijacked"}
    )
    assert resp.status_code == 404, resp.text

    resp = v2_client.delete(f"{base}/{pv_a['id']}", headers=headers_b)
    assert resp.status_code == 404, resp.text

    resp = v2_client.get(f"{base}/{pv_a['id']}/stats", headers=headers_b)
    assert resp.status_code == 404, resp.text

    # B 的列表不含 A 的版本
    resp = v2_client.get(f"{base}?feature=script_gen", headers=headers_b)
    items = resp.json()["data"]["items"]
    assert pv_a["id"] not in {item["id"] for item in items}

    # B 对 A 独占的 feature 做 A/B 选择 → 404（A 的激活版本对 B 不可见）
    resp = v2_client.post(
        f"{base}/select", headers=headers_b, json={"feature": "script_gen"}
    )
    assert resp.status_code == 404, resp.text

    # A 自己访问一切正常（对照）
    resp = v2_client.get(f"{base}/{pv_a['id']}", headers=headers_a)
    assert resp.status_code == 200
    assert resp.json()["data"]["system_prompt"] == "a secret prompt"


def test_find_duplicates_other_project_404(v2_client, make_user, auth_headers, app):
    uid_a = make_user(_uname("dua"))
    uid_b = make_user(_uname("dub"))
    headers_b = auth_headers(uid_b)

    project_a = _make_project(app, uid_a)

    # B 扫描 A 的项目 → 404（v1 无任何归属校验，平迁修复）
    resp = v2_client.post(
        "/api/v1/ai/find-duplicates",
        headers=headers_b,
        json={"project_id": project_a},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["message"] == "项目不存在"

    # 属主可进后续流程（用 mock 验证 200，见下）


def test_find_duplicates_mock_ai_200(v2_client, make_user, auth_headers, app, monkeypatch):
    import app.api.v2.v1.semantic_dedup as dedup_mod

    uid = make_user(_uname("duok"))
    headers = auth_headers(uid)
    project_id = _make_project(app, uid)

    captured = {}

    def fake_find_duplicates(project_id, *, threshold, case_type, config, limit):
        captured.update(
            project_id=project_id,
            threshold=threshold,
            case_type=case_type,
            limit=limit,
            config=config,
        )
        return {
            "total_cases": 2,
            "duplicate_pairs": [
                {
                    "case_a": {"id": 1, "name": "login", "description": "", "method": "GET", "url": "/login"},
                    "case_b": {"id": 2, "name": "login copy", "description": "", "method": "GET", "url": "/login"},
                    "similarity": 0.93,
                }
            ],
            "summary": {"total_pairs_checked": 1, "duplicate_count": 1, "method": "tfidf"},
        }

    monkeypatch.setattr(dedup_mod, "find_duplicates", fake_find_duplicates)

    resp = v2_client.post(
        "/api/v1/ai/find-duplicates",
        headers=headers,
        json={"project_id": project_id, "threshold": 0.9, "case_type": "web", "limit": 50},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 200
    assert body["message"] == "发现 1 组重复用例"
    assert body["data"]["total_cases"] == 2
    assert body["data"]["duplicate_pairs"][0]["similarity"] == 0.93
    assert body["data"]["summary"]["method"] == "tfidf"

    # 参数透传正确（零真实 AI 外呼：find_duplicates 已被替换）
    assert captured["project_id"] == project_id
    assert captured["threshold"] == 0.9
    assert captured["case_type"] == "web"
    assert captured["limit"] == 50
    assert "AI_ASSISTANT_BASE_URL" in captured["config"]

    # 前端 embedding 覆盖配置透传
    resp = v2_client.post(
        "/api/v1/ai/find-duplicates",
        headers=headers,
        json={
            "project_id": project_id,
            "embedding_base_url": " http://emb.local/v1 ",
            "embedding_api_key": "sk-test",
            "embedding_model": "test-embedding",
        },
    )
    assert resp.status_code == 200, resp.text
    assert captured["config"]["AI_EMBEDDING_BASE_URL"] == "http://emb.local/v1"
    assert captured["config"]["AI_EMBEDDING_API_KEY"] == "sk-test"
    assert captured["config"]["AI_EMBEDDING_MODEL"] == "test-embedding"


def test_find_duplicates_validation_400(v2_client, make_user, auth_headers, app):
    uid = make_user(_uname("duv"))
    headers = auth_headers(uid)
    project_id = _make_project(app, uid)

    cases = [
        ({}, "project_id is required"),
        ({"project_id": project_id, "threshold": 1.5}, "threshold must be a number between 0.0 and 1.0"),
        ({"project_id": project_id, "threshold": "0.9"}, "threshold must be a number between 0.0 and 1.0"),
        ({"project_id": project_id, "case_type": "ui"}, 'case_type must be "api" or "web"'),
        ({"project_id": project_id, "limit": 0}, "limit must be a positive integer"),
        ({"project_id": project_id, "limit": "500"}, "limit must be a positive integer"),
    ]
    for payload, msg in cases:
        resp = v2_client.post("/api/v1/ai/find-duplicates", headers=headers, json=payload)
        assert resp.status_code == 400, f"{payload}: {resp.text}"
        assert resp.json()["message"] == msg


# ---------------------------------------------------------------------------
# 4) Copilot（mock AI 客户端，零真实外呼）
# ---------------------------------------------------------------------------


def test_copilot_chat_mock_ai(v2_client, make_user, auth_headers, monkeypatch):
    import app.api.v2.v1.ai_copilot as copilot_mod

    uid = make_user(_uname("cpu"))
    headers = auth_headers(uid)

    captured = {}

    def fake_process_copilot_chat(messages, user_id, runtime_config):
        captured.update(messages=messages, user_id=user_id, config=runtime_config)
        return {"role": "assistant", "content": "mocked-reply"}

    monkeypatch.setattr(copilot_mod, "process_copilot_chat", fake_process_copilot_chat)

    resp = v2_client.post(
        "/api/v1/copilot/chat",
        headers=headers,
        json={"messages": [{"role": "user", "content": "帮我创建一个压测"}]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 200
    assert body["data"] == {"role": "assistant", "content": "mocked-reply"}

    # 透传参数正确
    assert captured["user_id"] == uid
    assert captured["messages"] == [{"role": "user", "content": "帮我创建一个压测"}]
    assert captured["config"]["AI_ASSISTANT_ENABLED"] is not None

    # 前端覆盖配置进入 runtime_config
    resp = v2_client.post(
        "/api/v1/copilot/chat",
        headers=headers,
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "base_url": " http://llm.local/v1 ",
            "model": " test-model ",
            "api_key": "sk-test",
            "vision_base_url": " http://vision.local ",
            "vision_model": " vision-model ",
            "vision_api_key": "sk-vision",
        },
    )
    assert resp.status_code == 200, resp.text
    cfg = captured["config"]
    assert cfg["AI_ASSISTANT_BASE_URL"] == "http://llm.local/v1"
    assert cfg["AI_ASSISTANT_MODEL"] == "test-model"
    assert cfg["AI_ASSISTANT_API_KEY"] == "sk-test"
    assert cfg["AI_VISION_BASE_URL"] == "http://vision.local"
    assert cfg["AI_VISION_MODEL"] == "vision-model"
    assert cfg["AI_VISION_API_KEY"] == "sk-vision"


def test_copilot_chat_empty_messages_400(v2_client, make_user, auth_headers):
    uid = make_user(_uname("cpe"))
    headers = auth_headers(uid)

    resp = v2_client.post("/api/v1/copilot/chat", headers=headers, json={"messages": []})
    assert resp.status_code == 400, resp.text
    assert resp.json()["message"] == "messages is required"


def test_copilot_chat_ai_failure_500(v2_client, make_user, auth_headers, monkeypatch):
    import app.api.v2.v1.ai_copilot as copilot_mod

    uid = make_user(_uname("cpf"))
    headers = auth_headers(uid)

    def boom(messages, user_id, runtime_config):
        raise RuntimeError("llm connection refused")

    monkeypatch.setattr(copilot_mod, "process_copilot_chat", boom)
    monkeypatch.setenv("APP_ENV", "testing")  # 非 production：透传异常信息（与 v1 一致）

    resp = v2_client.post(
        "/api/v1/copilot/chat",
        headers=headers,
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 500, resp.text
    assert "llm connection refused" in resp.json()["message"]


# ---------------------------------------------------------------------------
# 5) AI 统计：属主过滤 + 形状与 v1 一致
# ---------------------------------------------------------------------------


def test_ai_stats_overview_scoped_by_user(v2_client, make_user, auth_headers, app):
    uid_a = make_user(_uname("sta"))
    uid_b = make_user(_uname("stb"))
    headers_a = auth_headers(uid_a)
    headers_c = auth_headers(make_user(_uname("stc")))

    # A：2 次调用（1 成功 1 失败），B：1 次成功
    _make_invocation(app, uid_a, feature="copilot", success=True, latency_ms=100, total_tokens=120)
    _make_invocation(
        app, uid_a, feature="copilot", success=False, latency_ms=300,
        prompt_tokens=30, completion_tokens=20, total_tokens=50, cost_estimate=0.02,
    )
    _make_invocation(app, uid_b, feature="script_gen", success=True, latency_ms=50, total_tokens=999)

    resp = v2_client.get("/api/v1/ai/stats/overview", headers=headers_a)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 200
    data = body["data"]
    # 字段与 v1 完全一致
    assert set(data.keys()) == {
        "total_invocations",
        "success_rate",
        "total_tokens",
        "total_cost",
        "avg_latency_ms",
        "features",
    }
    # 只聚合自己的日志（IDOR 修复：v1 泄露全平台数据）
    assert data["total_invocations"] == 2
    assert data["success_rate"] == 50.0
    assert data["total_tokens"] == 170
    assert data["avg_latency_ms"] == 200.0
    assert data["features"] == {"copilot": 2}

    # B 只看到自己的
    resp = v2_client.get("/api/v1/ai/stats/overview", headers=auth_headers(uid_b))
    data = resp.json()["data"]
    assert data["total_invocations"] == 1
    assert data["features"] == {"script_gen": 1}

    # 无调用用户 → 全零形状
    resp = v2_client.get("/api/v1/ai/stats/overview", headers=headers_c)
    data = resp.json()["data"]
    assert data["total_invocations"] == 0
    assert data["success_rate"] == 0.0
    assert data["total_tokens"] == 0
    assert data["total_cost"] == 0.0
    assert data["avg_latency_ms"] == 0
    assert data["features"] == {}


def test_ai_stats_trends_shape(v2_client, make_user, auth_headers, app):
    uid = make_user(_uname("stt"))
    headers = auth_headers(uid)
    other = auth_headers(make_user(_uname("sto")))

    _make_invocation(app, uid, feature="copilot", success=True, latency_ms=100, total_tokens=120)
    _make_invocation(
        app, uid, feature="copilot", success=False, latency_ms=300,
        prompt_tokens=30, completion_tokens=20, total_tokens=50, cost_estimate=0.02,
    )

    # ---- 成功率趋势 ----
    resp = v2_client.get("/api/v1/ai/stats/success-rate-trend?days=7", headers=headers)
    assert resp.status_code == 200, resp.text
    trend = resp.json()["data"]
    assert isinstance(trend, list) and len(trend) == 1
    row = trend[0]
    assert set(row.keys()) == {"date", "total", "success", "success_rate"}
    assert row["total"] == 2
    assert row["success"] == 1
    assert row["success_rate"] == 50.0

    # feature 过滤不匹配 → 空列表
    resp = v2_client.get(
        "/api/v1/ai/stats/success-rate-trend?days=7&feature=script_gen", headers=headers
    )
    assert resp.json()["data"] == []

    # ---- 延迟趋势 ----
    resp = v2_client.get("/api/v1/ai/stats/latency-trend?days=7", headers=headers)
    rows = resp.json()["data"]
    assert len(rows) == 1
    assert set(rows[0].keys()) == {"date", "avg_latency_ms", "avg_tokens"}
    assert rows[0]["avg_latency_ms"] == 200.0
    assert rows[0]["avg_tokens"] == 85.0

    # ---- token 消耗 ----
    resp = v2_client.get("/api/v1/ai/stats/token-consumption?days=7", headers=headers)
    rows = resp.json()["data"]
    assert len(rows) == 1
    assert set(rows[0].keys()) == {
        "date", "prompt_tokens", "completion_tokens", "total_tokens", "cost",
    }
    assert rows[0]["prompt_tokens"] == 90
    assert rows[0]["completion_tokens"] == 60
    assert rows[0]["total_tokens"] == 170
    assert rows[0]["cost"] == 0.03  # 0.01 + 0.02

    # 他人日志不进入我的趋势（IDOR 修复对照）
    _make_invocation(app, 10**9, feature="copilot", success=True, total_tokens=4321)
    resp = v2_client.get("/api/v1/ai/stats/token-consumption?days=7", headers=headers)
    assert resp.json()["data"][0]["total_tokens"] == 170
    resp = v2_client.get("/api/v1/ai/stats/token-consumption?days=7", headers=other)
    data = resp.json()["data"]
    assert all(r["total_tokens"] != 170 for r in data)


def test_ai_stats_prompt_versions_comparison_scoped(v2_client, make_user, auth_headers):
    uid_a = make_user(_uname("cpa"))
    uid_b = make_user(_uname("cpb"))
    headers_a = auth_headers(uid_a)
    headers_b = auth_headers(uid_b)
    base = "/api/v1/ai/prompt-versions"

    # A、B 各创建一个 dedup 版本
    resp = v2_client.post(
        base,
        headers=headers_a,
        json={"feature": "dedup", "name": "a-ver", "system_prompt": "a"},
    )
    pv_a = resp.json()["data"]
    resp = v2_client.post(
        base,
        headers=headers_b,
        json={"feature": "dedup", "name": "b-ver", "system_prompt": "b"},
    )
    pv_b = resp.json()["data"]

    # A 的对比视图：只含自己的版本（IDOR 修复）
    resp = v2_client.get("/api/v1/ai/stats/prompt-versions-comparison", headers=headers_a)
    assert resp.status_code == 200, resp.text
    items = resp.json()["data"]
    ids = {item["id"] for item in items}
    assert pv_a["id"] in ids
    assert pv_b["id"] not in ids

    # 字段与 v1 一致
    item = next(i for i in items if i["id"] == pv_a["id"])
    assert set(item.keys()) == {
        "id", "feature", "name", "version", "is_active",
        "total_invocations", "success_count", "failure_count", "success_rate",
        "avg_latency_ms", "avg_tokens", "avg_cost",
    }
    assert item["success_rate"] == 0.0

    # feature 过滤
    resp = v2_client.get(
        "/api/v1/ai/stats/prompt-versions-comparison?feature=dedup", headers=headers_b
    )
    items = resp.json()["data"]
    assert pv_b["id"] in {item["id"] for item in items}
    assert pv_a["id"] not in {item["id"] for item in items}
