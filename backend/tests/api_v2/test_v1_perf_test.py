"""
v1 性能测试路由平迁测试（/api/v1/perf-test/*，源：app/api/perf_test.py）

覆盖：
1. 未登录 401
2. 创建压测场景 → 查询（列表 + 详情）
3. 访问他人场景 404（IDOR 修复：GET/PUT/DELETE/run 越权一律 404）
4. 启动压测：Celery apply_async 被 mock，绝不真实派发，返回 task_id
5. 非法 target_url 被拒（格式非法 / SSRF 内网地址）

说明：合法 target_url 走 SSRF_ALLOWLIST_HOSTS 白名单路径，避免 DNS 解析外呼；
所有 Celery 交互均被 mock，测试全程零网络请求。
"""

import gc
import uuid

import pytest
from sqlalchemy import update

BASE = "/api/v1/perf-test"


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    """测试环境无 Redis：把 token 黑名单/限流服务的 _get_redis 打桩为 None。

    token_blacklist 与 rate_limit_service 对 Redis 不可用均有文档化的降级路径
    （版本号取 0、黑名单放行、限流放行），打桩后签发/校验 JWT 全程零 socket，
    避免测试环境对 redis://localhost:6379 的 TCP 连接挂起。
    """
    import app.services.rate_limit_service as rate_limit_service
    import app.services.token_blacklist as token_blacklist

    monkeypatch.setattr(token_blacklist, "_get_redis", lambda: None)
    monkeypatch.setattr(rate_limit_service, "_get_redis", lambda: None, raising=False)
    yield


def _username() -> str:
    return f"v1perf_{uuid.uuid4().hex[:8]}"


@pytest.fixture()
def ssrf_allowlist(monkeypatch):
    """把测试目标域名加入 SSRF 白名单，避免 is_safe_url 做真实 DNS 解析"""
    monkeypatch.setenv("SSRF_ALLOWLIST_HOSTS", "perf-target.example.test")
    return "https://perf-target.example.test/api/v1/users?name=test"


def _create_scenario(v2_client, headers, name="登录接口压测", target_url=None, **extra):
    payload = {
        "name": name,
        "target_url": target_url or "https://perf-target.example.test/api/v1/users?name=test",
        "method": "GET",
        "user_count": 5,
        "spawn_rate": 1,
        "duration": 10,
    }
    payload.update(extra)
    resp = v2_client.post(f"{BASE}/scenarios", headers=headers, json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


# ---------------------------------------------------------------------------
# 1. 未登录 401
# ---------------------------------------------------------------------------

def test_unauthenticated_returns_401(v2_client):
    assert v2_client.get(f"{BASE}/scenarios").status_code == 401
    assert v2_client.post(f"{BASE}/scenarios", json={"name": "x"}).status_code == 401
    assert v2_client.get(f"{BASE}/running").status_code == 401
    assert v2_client.post(f"{BASE}/ai/generate", json={"prompt": "x"}).status_code == 401
    assert v2_client.get(f"{BASE}/results").status_code == 401
    assert v2_client.get(f"{BASE}/baselines").status_code == 401


# ---------------------------------------------------------------------------
# 2. 创建压测场景 → 查询
# ---------------------------------------------------------------------------

def test_create_and_query_scenario(v2_client, make_user, auth_headers, ssrf_allowlist):
    uid = make_user(_username())
    headers = auth_headers(uid)

    data = _create_scenario(v2_client, headers, description="压测登录接口")

    # v1 响应结构：{code, message, data, timestamp}
    assert data["name"] == "登录接口压测"
    assert data["target_url"] == "https://perf-target.example.test/api/v1/users?name=test"
    assert data["user_id"] == uid
    assert data["user_count"] == 5 and data["duration"] == 10
    # 未提供 script_content 时自动生成 Locust 脚本
    assert "Locust" in (data["script_content"] or "")

    # 列表查询
    lst = v2_client.get(f"{BASE}/scenarios", headers=headers)
    assert lst.status_code == 200, lst.text
    items = lst.json()["data"]
    assert any(s["id"] == data["id"] for s in items)

    # 详情查询
    detail = v2_client.get(f"{BASE}/scenarios/{data['id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["data"]["id"] == data["id"]

    # 更新后查询
    upd = v2_client.put(f"{BASE}/scenarios/{data['id']}", headers=headers, json={"name": "改名后"})
    assert upd.status_code == 200
    assert upd.json()["data"]["name"] == "改名后"


# ---------------------------------------------------------------------------
# 3. 访问他人场景 404（IDOR 修复）
# ---------------------------------------------------------------------------

def test_access_other_users_scenario_404(v2_client, make_user, auth_headers, ssrf_allowlist):
    owner_id = make_user(_username())
    attacker_id = make_user(_username())
    owner_headers = auth_headers(owner_id)
    attacker_headers = auth_headers(attacker_id)

    scenario = _create_scenario(v2_client, owner_headers, name="owner-only")
    sid = scenario["id"]

    # 越权读取 / 修改 / 删除 / 启动 一律 404
    assert v2_client.get(f"{BASE}/scenarios/{sid}", headers=attacker_headers).status_code == 404
    assert v2_client.put(
        f"{BASE}/scenarios/{sid}", headers=attacker_headers, json={"name": "hacked"}
    ).status_code == 404
    assert v2_client.delete(f"{BASE}/scenarios/{sid}", headers=attacker_headers).status_code == 404
    assert v2_client.post(f"{BASE}/scenarios/{sid}/run", headers=attacker_headers).status_code == 404
    assert v2_client.get(f"{BASE}/scenarios/{sid}/status", headers=attacker_headers).status_code == 404

    # 场景未被越权修改，属主仍可正常访问
    assert v2_client.get(f"{BASE}/scenarios/{sid}", headers=owner_headers).json()["data"]["name"] == "owner-only"

    # 基线路由同样按属主隔离：为他人场景创建基线 → 404
    r = v2_client.post(
        f"{BASE}/baselines", headers=attacker_headers,
        json={"scenario_id": sid, "metrics": {"p95": 100}},
    )
    assert r.status_code == 404
    # 与他人场景基线对比 → 404
    r = v2_client.post(
        f"{BASE}/baselines/compare", headers=attacker_headers,
        json={"scenario_id": sid, "current_metrics": {"p95": 200}},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 4. 启动压测（Celery 被 mock，不真实派发）
# ---------------------------------------------------------------------------

def test_run_scenario_returns_task_id_with_mocked_celery(
    v2_client, make_user, auth_headers, monkeypatch, ssrf_allowlist
):
    uid = make_user(_username())
    headers = auth_headers(uid)
    scenario = _create_scenario(v2_client, headers)
    sid = scenario["id"]

    from app.api.routes import perf_test as perf_test_v1

    captured = {}

    class _FakeAsyncResult:
        id = "mocked-task-id-001"

    def _fake_apply_async(args=None, task_id=None, **kwargs):
        captured["args"] = args
        captured["task_id"] = task_id
        return _FakeAsyncResult()

    # mock 掉共享任务对象的 apply_async，测试绝不触发真实 Celery 派发
    monkeypatch.setattr(perf_test_v1.run_perf_test_task, "apply_async", _fake_apply_async)

    r = v2_client.post(
        f"{BASE}/scenarios/{sid}/run", headers=headers,
        json={"user_count": 20, "spawn_rate": 2, "duration": 30},
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["task_id"] == "mocked-task-id-001"
    assert data["scenario_id"] == sid
    assert data["message"] == "Scenario submitted"
    assert data["config"] == {
        "users": 20,
        "spawn_rate": 2,
        "run_time": 30,
        "step_load_enabled": False,
        "step_users": 10,
        "step_duration": 30,
    }

    # 派发参数与 task_id 与 v1 完全一致
    assert captured["args"] == [sid, 20, 2, 30, False, 10, 30]
    assert captured["task_id"] == f"perf_test_{sid}_{uid}"

    # 执行状态查询：场景已置为 running
    st = v2_client.get(f"{BASE}/scenarios/{sid}/status", headers=headers)
    assert st.status_code == 200
    assert st.json()["data"]["status"] == "running"
    assert st.json()["data"]["last_run_at"] is not None

    # 运行中列表包含该场景
    running = v2_client.get(f"{BASE}/running", headers=headers)
    assert running.status_code == 200
    assert any(s["scenario_id"] == sid for s in running.json()["data"])

    # 重复启动 → 400
    r = v2_client.post(f"{BASE}/scenarios/{sid}/run", headers=headers)
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# 5. 非法 target_url 被拒
# ---------------------------------------------------------------------------

def test_invalid_target_url_rejected(v2_client, make_user, auth_headers, ssrf_allowlist):
    uid = make_user(_username())
    headers = auth_headers(uid)

    bad_urls = [
        "not-a-url",                       # 格式非法
        "ftp://example.com/x",             # 非 http/https
        "http://127.0.0.1/x",              # SSRF：内网回环（IP 字面量，无 DNS）
        "http://192.168.1.10/admin",       # SSRF：内网段
    ]
    for bad in bad_urls:
        r = v2_client.post(
            f"{BASE}/scenarios", headers=headers,
            json={"name": "bad-url", "target_url": bad},
        )
        assert r.status_code == 400, f"{bad} -> {r.status_code} {r.text}"
        assert r.json()["code"] == 400

    # PUT 更新为非法 URL 同样被拒（先建一个合法场景用于校验）
    scenario = _create_scenario(v2_client, headers, name="valid-scenario")
    r = v2_client.put(
        f"{BASE}/scenarios/{scenario['id']}", headers=headers,
        json={"target_url": "http://127.0.0.1/x"},
    )
    assert r.status_code == 400
    # URL 未被修改
    assert v2_client.get(
        f"{BASE}/scenarios/{scenario['id']}", headers=headers
    ).json()["data"]["target_url"] == scenario["target_url"]

    # 非法 URL 全部被拒，仅有此前创建的合法场景
    lst = v2_client.get(f"{BASE}/scenarios", headers=headers)
    assert [s["name"] for s in lst.json()["data"]] == ["valid-scenario"]


# ---------------------------------------------------------------------------
# 补充：结果分页包结构与基线属主内创建/查询
# ---------------------------------------------------------------------------

def test_results_envelope_and_pagination(v2_client, make_user, auth_headers):
    uid = make_user(_username())
    headers = auth_headers(uid)

    r = v2_client.get(f"{BASE}/results", headers=headers)
    assert r.status_code == 200
    body = r.json()
    # v1 paginate_response 结构
    assert body["code"] == 200 and body["message"] == "success"
    assert body["data"]["items"] == []
    assert body["data"]["pagination"] == {"total": 0, "page": 1, "per_page": 20, "pages": 0}


def test_baselines_owner_scoped(v2_client, make_user, auth_headers, ssrf_allowlist):
    uid = make_user(_username())
    headers = auth_headers(uid)
    scenario = _create_scenario(v2_client, headers)
    sid = scenario["id"]

    # 创建基线（v1 行为：HTTP 201 + body.code 201）
    r = v2_client.post(
        f"{BASE}/baselines", headers=headers,
        json={"scenario_id": sid, "metrics": {"p95": 100, "avg": 50}},
    )
    assert r.status_code == 200, r.text
    assert r.json()["code"] == 200
    baseline = r.json()["data"]
    assert baseline["scenario_id"] == sid and baseline["is_active"] is True

    # 列表只含本人基线
    lst = v2_client.get(f"{BASE}/baselines", headers=headers)
    assert lst.status_code == 200
    assert [b["id"] for b in lst.json()["data"]] == [baseline["id"]]

    # 与基线对比：p95 从 100 涨到 200 → 退化 100%
    r = v2_client.post(
        f"{BASE}/baselines/compare", headers=headers,
        json={"scenario_id": sid, "current_metrics": {"p95": 200}},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["has_baseline"] is True
    assert data["is_degraded"] is True and data["degraded_metrics"] == ["p95"]
    assert data["comparison"]["p95"]["change_pct"] == 100.0
