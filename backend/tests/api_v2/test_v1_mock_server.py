"""
v1 mock_server 平迁路由测试（/api/v1/mock-servers、/api/v1/mock-rules、
/api/v1/mock/{server_id}/{subpath}，源：app/api/mock_server.py）

覆盖：
1. 管理类端点（server/rule CRUD、请求日志）未登录一律 401
2. 公开 Mock 代理端点（v1 无鉴权设计）未登录可访问：规则命中/未命中/禁用/服务器不存在/OPTIONS 预检
3. server/rule 创建 → 列表 → 详情 → 更新 → 删除 全链路，信封与 v1 一致
4. IDOR：服务层 user_id 属主校验（他人 server/rule → 404）、他人项目列表/创建 → 404
"""

import gc
import uuid

import pytest
from app.extensions import db

BASE = "/api/v1"


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
    return f"mock_{uuid.uuid4().hex[:10]}"


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

def _create_server(v2_client, headers, project_id, name="用户服务 Mock", **extra) -> dict:
    payload = {"project_id": project_id, "name": name, **extra}
    resp = v2_client.post(f"{BASE}/mock-servers", json=payload, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _create_rule(v2_client, headers, server_id, name="users-mock-rule", **extra) -> dict:
    payload = {
        "name": name,
        "match_path": extra.pop("match_path", "/api/users"),
        **extra,
    }
    resp = v2_client.post(f"{BASE}/mock-servers/{server_id}/rules", json=payload, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


# ---------------------------------------------------------------------------
# 1. 未登录 401
# ---------------------------------------------------------------------------

class TestMockServerAuth:
    @pytest.mark.parametrize(
        "method,url",
        [
            ("get", f"{BASE}/mock-servers"),
            ("post", f"{BASE}/mock-servers"),
            ("get", f"{BASE}/mock-servers/1"),
            ("put", f"{BASE}/mock-servers/1"),
            ("delete", f"{BASE}/mock-servers/1"),
            ("post", f"{BASE}/mock-servers/1/rules"),
            ("put", f"{BASE}/mock-rules/1"),
            ("delete", f"{BASE}/mock-rules/1"),
            ("get", f"{BASE}/mock-servers/1/logs"),
            ("delete", f"{BASE}/mock-servers/1/logs"),
        ],
    )
    def test_protected_endpoints_require_auth(self, v2_client, method, url):
        """未登录访问管理类端点 → 401"""
        resp = getattr(v2_client, method)(url)
        assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# 2. Mock Server CRUD
# ---------------------------------------------------------------------------

class TestMockServerCRUD:
    def test_create_list_detail_update_delete_flow(self, v2_client, app, make_user, auth_headers):
        """创建 → 列表 → 详情（含规则）→ 更新 → 删除"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)

        server = _create_server(v2_client, headers, project_id)
        assert server["name"] == "用户服务 Mock"
        assert server["is_enabled"] is True
        assert server["path_prefix"] == "/"

        rule = _create_rule(v2_client, headers, server["id"])

        # 列表
        resp = v2_client.get(f"{BASE}/mock-servers?project_id={project_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        items = resp.json()["data"]
        assert any(s["id"] == server["id"] and s["rule_count"] == 1 for s in items)

        # 详情（含规则）
        resp = v2_client.get(f"{BASE}/mock-servers/{server['id']}", headers=headers)
        assert resp.status_code == 200, resp.text
        detail = resp.json()["data"]
        assert [r["id"] for r in detail["rules"]] == [rule["id"]]

        # 更新
        resp = v2_client.put(
            f"{BASE}/mock-servers/{server['id']}",
            json={"name": "改名 Mock", "path_prefix": "/mock-api"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["message"] == "Mock 服务器已更新"
        assert body["data"]["name"] == "改名 Mock"
        assert body["data"]["path_prefix"] == "/mock-api"

        # 删除（级联删规则）
        resp = v2_client.delete(f"{BASE}/mock-servers/{server['id']}", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "Mock 服务器已删除"

        resp = v2_client.get(f"{BASE}/mock-servers/{server['id']}", headers=headers)
        assert resp.status_code == 404, resp.text

    def test_create_server_missing_name_400(self, v2_client, app, make_user, auth_headers):
        """缺名称 → 400（v1 行为）"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)
        resp = v2_client.post(
            f"{BASE}/mock-servers", json={"project_id": project_id}, headers=headers
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["message"] == "缺少服务器名称"

    def test_create_server_missing_project_400(self, v2_client, make_user, auth_headers):
        """缺 project_id → 400（v1 行为）"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        resp = v2_client.post(f"{BASE}/mock-servers", json={"name": "NoProj"}, headers=headers)
        assert resp.status_code == 400, resp.text
        assert resp.json()["message"] == "缺少 project_id"

    def test_mock_server_idor(self, v2_client, app, make_user, auth_headers):
        """他人项目列表 404；向他人项目注入 404；他人 server 读写删 404（服务层属主校验）"""
        owner = make_user(_username())
        stranger = make_user(_username())
        owner_headers = auth_headers(owner)
        stranger_headers = auth_headers(stranger)
        project_id = _make_project(app, owner)

        server = _create_server(v2_client, owner_headers, project_id)

        # 列表他人项目 → 404（v1 原有 project.owner_id 校验）
        resp = v2_client.get(f"{BASE}/mock-servers?project_id={project_id}", headers=stranger_headers)
        assert resp.status_code == 404, resp.text
        assert resp.json()["message"] == "项目不存在"

        # 向他人项目注入 Mock 服务器 → 404（IDOR 修复：v1 可向他人项目注入）
        resp = v2_client.post(
            f"{BASE}/mock-servers",
            json={"project_id": project_id, "name": "Injected"},
            headers=stranger_headers,
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["message"] == "项目不存在"

        # 他人 server：详情/更新/删除 → 404（服务层 _get_owned_server 属主校验）
        for method, kwargs in [
            ("get", {}),
            ("put", {"json": {"name": "hijack"}}),
            ("delete", {}),
        ]:
            resp = getattr(v2_client, method)(
                f"{BASE}/mock-servers/{server['id']}", headers=stranger_headers, **kwargs
            )
            assert resp.status_code == 404, f"{method}: {resp.text}"
            assert "不存在" in resp.json()["message"]

        # 在他人 server 下创建规则 → 404（服务层属主校验）
        resp = v2_client.post(
            f"{BASE}/mock-servers/{server['id']}/rules",
            json={"name": "Injected Rule", "match_path": "/x"},
            headers=stranger_headers,
        )
        assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# 3. Mock Rule CRUD
# ---------------------------------------------------------------------------

class TestMockRuleCRUD:
    def test_create_update_delete_flow(self, v2_client, app, make_user, auth_headers):
        """创建规则 → 详情可见 → 更新 → 删除"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)
        server = _create_server(v2_client, headers, project_id)

        rule = _create_rule(
            v2_client,
            headers,
            server["id"],
            match_method="GET",
            response_code=201,
            response_body='{"user": "mocked"}',
        )
        assert rule["match_path"] == "/api/users"
        assert rule["response_code"] == 201

        # 更新
        resp = v2_client.put(
            f"{BASE}/mock-rules/{rule['id']}",
            json={"name": "改名规则", "response_code": 202, "priority": 5},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["message"] == "规则已更新"
        assert body["data"]["response_code"] == 202
        assert body["data"]["priority"] == 5

        # 删除
        resp = v2_client.delete(f"{BASE}/mock-rules/{rule['id']}", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "规则已删除"

        # 详情中规则已消失
        resp = v2_client.get(f"{BASE}/mock-servers/{server['id']}", headers=headers)
        assert resp.json()["data"]["rules"] == []

    def test_create_rule_missing_fields_400(self, v2_client, app, make_user, auth_headers):
        """缺 name/match_path → 400（v1 行为）"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)
        server = _create_server(v2_client, headers, project_id)

        resp = v2_client.post(
            f"{BASE}/mock-servers/{server['id']}/rules",
            json={"match_path": "/x"},
            headers=headers,
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["message"] == "缺少规则名称"

        resp = v2_client.post(
            f"{BASE}/mock-servers/{server['id']}/rules",
            json={"name": "NoPath"},
            headers=headers,
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["message"] == "缺少匹配路径"

    def test_mock_rule_idor(self, v2_client, app, make_user, auth_headers):
        """他人规则：更新/删除 → 404（服务层 rule → server → project.owner 属主校验）"""
        owner = make_user(_username())
        stranger = make_user(_username())
        owner_headers = auth_headers(owner)
        stranger_headers = auth_headers(stranger)
        project_id = _make_project(app, owner)

        server = _create_server(v2_client, owner_headers, project_id)
        rule = _create_rule(v2_client, owner_headers, server["id"])

        resp = v2_client.put(
            f"{BASE}/mock-rules/{rule['id']}", json={"name": "hijack"}, headers=stranger_headers
        )
        assert resp.status_code == 404, resp.text
        assert "不存在" in resp.json()["message"]

        resp = v2_client.delete(f"{BASE}/mock-rules/{rule['id']}", headers=stranger_headers)
        assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# 4. 公开 Mock 代理端点（v1 无鉴权设计）
# ---------------------------------------------------------------------------

class TestMockProxy:
    def _setup(self, v2_client, app, make_user, auth_headers):
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)
        server = _create_server(v2_client, headers, project_id)
        rule = _create_rule(
            v2_client,
            headers,
            server["id"],
            match_method="GET",
            response_code=201,
            response_body='{"user": "mocked"}',
        )
        return headers, project_id, server, rule

    def test_proxy_public_no_auth(self, v2_client, app, make_user, auth_headers):
        """公开端点：未登录直接可访问 Mock 代理，命中规则返回预设响应"""
        _, _, server, rule = self._setup(v2_client, app, make_user, auth_headers)

        resp = v2_client.get(
            f"{BASE}/mock/{server['id']}/api/users",
            headers={"Origin": "http://front.example.test"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.text == '{"user": "mocked"}'
        assert resp.headers["X-Mock-Rule"] == rule["name"]
        assert resp.headers["X-Mock-Server-ID"] == str(server["id"])
        assert resp.headers["Access-Control-Allow-Origin"] == "http://front.example.test"
        assert resp.headers["Content-Type"].startswith("application/json")

    def test_proxy_no_matching_rule_404(self, v2_client, app, make_user, auth_headers):
        """未命中规则 → 404 + 默认 JSON 错误体（v1 行为）"""
        _, _, server, _ = self._setup(v2_client, app, make_user, auth_headers)

        resp = v2_client.get(f"{BASE}/mock/{server['id']}/api/unknown/path")
        assert resp.status_code == 404, resp.text
        assert resp.text == '{"error": "No matching mock rule found"}'
        assert resp.headers["X-Mock-Server-ID"] == str(server["id"])

    def test_proxy_server_not_found(self, v2_client):
        """Mock 服务器不存在 → 404 信封（v1 行为）"""
        resp = v2_client.get(f"{BASE}/mock/999999/api/anything")
        assert resp.status_code == 404, resp.text
        body = resp.json()
        assert body["code"] == 404
        assert body["message"] == "Mock 服务器不存在"

    def test_proxy_disabled_server_503(self, v2_client, app, make_user, auth_headers):
        """禁用的 Mock 服务器 → 503（服务层行为，与 v1 一致）"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)
        server = _create_server(v2_client, headers, project_id, is_enabled=False)

        resp = v2_client.get(f"{BASE}/mock/{server['id']}/api/users")
        assert resp.status_code == 503, resp.text
        assert resp.text == '{"error": "Mock server is disabled"}'

    def test_proxy_options_preflight(self, v2_client, app, make_user, auth_headers):
        """OPTIONS 预检 → 200 空 body + CORS 头（公开端点）"""
        _, _, server, _ = self._setup(v2_client, app, make_user, auth_headers)

        resp = v2_client.options(
            f"{BASE}/mock/{server['id']}/api/users",
            headers={"Origin": "http://front.example.test"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["Access-Control-Allow-Origin"] == "http://front.example.test"
        assert "PATCH" in resp.headers["Access-Control-Allow-Methods"]
        assert "Authorization" in resp.headers["Access-Control-Allow-Headers"]

    def test_proxy_post_with_body_match(self, v2_client, app, make_user, auth_headers):
        """POST + body 包含匹配（验证请求体经依赖读取后参与规则匹配）"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)
        server = _create_server(v2_client, headers, project_id)
        rule = _create_rule(
            v2_client,
            headers,
            server["id"],
            name="login-mock",
            match_path="/api/login",
            match_body_contains="special-token-42",
            response_code=200,
            response_body='{"token": "fake-jwt"}',
        )

        # body 不含关键字 → 不命中
        resp = v2_client.post(f"{BASE}/mock/{server['id']}/api/login", json={"user": "a"})
        assert resp.status_code == 404, resp.text

        # body 含关键字 → 命中
        resp = v2_client.post(
            f"{BASE}/mock/{server['id']}/api/login",
            content='{"user": "a", "secret": "special-token-42"}',
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.text == '{"token": "fake-jwt"}'
        assert resp.headers["X-Mock-Rule"] == rule["name"]


# ---------------------------------------------------------------------------
# 5. 请求日志
# ---------------------------------------------------------------------------

class TestMockRequestLogs:
    def test_logs_get_and_clear(self, v2_client, app, make_user, auth_headers):
        """代理请求产生日志 → GET 日志可见 → DELETE 清空 → GET 为空"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)
        server = _create_server(v2_client, headers, project_id)
        _create_rule(v2_client, headers, server["id"], response_code=200, response_body="{}")

        # 产生请求日志
        resp = v2_client.get(f"{BASE}/mock/{server['id']}/api/users")
        assert resp.status_code == 200, resp.text

        # GET 日志
        resp = v2_client.get(f"{BASE}/mock-servers/{server['id']}/logs", headers=headers)
        assert resp.status_code == 200, resp.text
        logs = resp.json()["data"]
        assert len(logs) == 1
        assert logs[0]["method"] == "GET"
        assert logs[0]["path"] == "/api/users"

        # limit 参数
        resp = v2_client.get(
            f"{BASE}/mock-servers/{server['id']}/logs?limit=10", headers=headers
        )
        assert resp.status_code == 200, resp.text

        # DELETE 清空
        resp = v2_client.delete(f"{BASE}/mock-servers/{server['id']}/logs", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["message"] == "日志已清空"
        assert body["data"]["deleted"] == 1

        # 清空后为空
        resp = v2_client.get(f"{BASE}/mock-servers/{server['id']}/logs", headers=headers)
        assert resp.json()["data"] == []
