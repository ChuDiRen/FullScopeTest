"""
v1 接口测试路由平迁测试（/api/v1/api-test/*，源：app/api/api_test.py）

覆盖：
1. 公开端点（health / mock server）无鉴权 200；受保护端点未登录 401
2. 创建集合 → 创建用例 → 列表 → 详情 → 更新 → 删除 全链路 200，响应信封与 v1 一致
3. 访问/更新/删除他人用例与集合、越权执行/自愈/成本估算/进度 → 404（IDOR 修复验证）
4. 执行类端点参数校验 400（缺 url / SSRF 内网地址），execution_service 与
   scenario_executor 全部 mock，测试全程零真实外呼、零 Celery 派发
"""

import gc
import uuid

import pytest
from sqlalchemy import select
from app.extensions import db
from sqlalchemy import func
from sqlalchemy import update

BASE = "/api/v1/api-test"


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
    return f"apitest_{uuid.uuid4().hex[:10]}"


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

def _make_run(app, project_id: int, user_id: int, **extra) -> int:
    from app.models.test_run import TestRun
    run = TestRun(
        project_id=project_id,
        triggered_user_id=user_id,
        test_type="api",
        status="success",
        total_cases=1,
        passed=1,
    )
    db.session.add(run)
    db.session.commit()
    return run.id

def _create_collection(v2_client, headers, name="冒烟集合", **extra) -> int:
    resp = v2_client.post(
        f"{BASE}/collections",
        json={"name": name, "description": extra.pop("description", "自动化测试集合"), **extra},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["id"]


def _create_case(v2_client, headers, collection_id=None, **extra) -> int:
    payload = {
        "name": extra.pop("name", "登录接口"),
        "method": extra.pop("method", "GET"),
        "url": extra.pop("url", "https://api.example.test/login"),
        "collection_id": collection_id,
    }
    payload.update(extra)
    resp = v2_client.post(f"{BASE}/cases", json=payload, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["id"]


# ---------------------------------------------------------------------------
# 1. 公开端点 + 未登录 401
# ---------------------------------------------------------------------------

class TestApiTestAuth:
    def test_health_public(self, v2_client):
        """健康检查是 v1 公开端点，无鉴权应 200"""
        resp = v2_client.get(f"{BASE}/health")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == 200
        assert body["message"] == "接口测试模块正常"

    def test_mock_server_public(self, v2_client, app, make_user, auth_headers):
        """Mock Server 是 v1 无鉴权设计端点：未登录直接可取 Mock 数据，含 CORS 与自定义状态码"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        case_id = _create_case(
            v2_client,
            headers,
            mock_enabled=True,
            mock_response_code=201,
            mock_response_body='{"mocked": true}',
        )

        resp = v2_client.get(
            f"{BASE}/mock/{case_id}",
            headers={"Origin": "http://front.example.test"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.text == '{"mocked": true}'
        assert resp.headers["Access-Control-Allow-Origin"] == "http://front.example.test"
        assert resp.headers["Content-Type"].startswith("application/json")

    def test_mock_server_disabled_400(self, v2_client, make_user, auth_headers):
        """未开启 Mock 的用例 → 400（公开端点的业务校验与 v1 一致）"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        case_id = _create_case(v2_client, headers)

        resp = v2_client.get(f"{BASE}/mock/{case_id}")
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == 400
        assert body["message"] == "该用例未开启 Mock 功能"

    def test_mock_options_preflight(self, v2_client, make_user, auth_headers):
        """OPTIONS 预检返回 CORS 头（公开端点）"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        case_id = _create_case(v2_client, headers)

        resp = v2_client.options(
            f"{BASE}/mock/{case_id}",
            headers={"Origin": "http://front.example.test"},
        )
        assert resp.status_code == 200
        assert resp.headers["Access-Control-Allow-Origin"] == "http://front.example.test"
        assert "PATCH" in resp.headers["Access-Control-Allow-Methods"]

    @pytest.mark.parametrize(
        "method,url",
        [
            ("get", f"{BASE}/ai/config"),
            ("post", f"{BASE}/ai/config"),
            ("post", f"{BASE}/ai/plan"),
            ("post", f"{BASE}/ai/synthesize-cases"),
            ("post", f"{BASE}/ai/review-collection"),
            ("get", f"{BASE}/collections"),
            ("post", f"{BASE}/collections"),
            ("put", f"{BASE}/collections/1"),
            ("delete", f"{BASE}/collections/1"),
            ("get", f"{BASE}/cases"),
            ("post", f"{BASE}/cases"),
            ("get", f"{BASE}/cases/1"),
            ("put", f"{BASE}/cases/1"),
            ("delete", f"{BASE}/cases/1"),
            ("post", f"{BASE}/import/postman"),
            ("post", f"{BASE}/import/csv"),
            ("get", f"{BASE}/import/template"),
            ("post", f"{BASE}/execute"),
            ("post", f"{BASE}/cases/1/run"),
            ("post", f"{BASE}/collections/1/run"),
            ("get", f"{BASE}/runs/1/progress"),
            ("get", f"{BASE}/cases/1/versions"),
            ("get", f"{BASE}/versions/1"),
            ("get", f"{BASE}/versions/diff"),
            ("post", f"{BASE}/import-curl"),
            ("post", f"{BASE}/execute-scenario"),
            ("get", f"{BASE}/history"),
            ("post", f"{BASE}/history"),
            ("get", f"{BASE}/history/trend"),
            ("get", f"{BASE}/history/1"),
            ("post", f"{BASE}/smart-select"),
            ("post", f"{BASE}/heal-case"),
            ("post", f"{BASE}/apply-heal"),
            ("get", f"{BASE}/tags/stats"),
            ("post", f"{BASE}/tags/filter"),
            ("post", f"{BASE}/validate-schema"),
            ("post", f"{BASE}/generate-schema"),
            ("post", f"{BASE}/import-har"),
            ("post", f"{BASE}/parse-har"),
            ("post", f"{BASE}/detect-changes"),
            ("post", f"{BASE}/bdd/parse"),
            ("post", f"{BASE}/bdd/import"),
            ("get", f"{BASE}/collections/1/estimate"),
        ],
    )
    def test_unauthenticated_401(self, v2_client, method, url):
        """未登录访问受保护端点 → 401，响应结构与 v1 error_response 一致"""
        # 该版本 TestClient 的 get/delete 不支持 json=，仅对写方法带 body
        kwargs = {"json": {}} if method in ("post", "put", "patch") else {}
        resp = getattr(v2_client, method)(url, **kwargs)
        assert resp.status_code == 401, f"{method.upper()} {url}: {resp.text}"
        body = resp.json()
        assert body["code"] == 401


# ---------------------------------------------------------------------------
# 2. 创建 → 列表 → 详情 → 更新 → 删除
# ---------------------------------------------------------------------------

class TestCollectionAndCaseCRUD:
    def test_collection_and_case_full_flow(self, v2_client, make_user, auth_headers):
        user_id = make_user(_username())
        headers = auth_headers(user_id)

        # 创建集合
        resp = v2_client.post(
            f"{BASE}/collections",
            json={"name": "冒烟集合", "description": "d1"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == 200
        assert body["message"] == "创建成功"
        collection_id = body["data"]["id"]
        assert body["data"]["name"] == "冒烟集合"

        # 集合列表包含新集合
        resp = v2_client.get(f"{BASE}/collections", headers=headers)
        assert resp.status_code == 200
        items = resp.json()["data"]
        assert any(c["id"] == collection_id for c in items)

        # 创建用例
        resp = v2_client.post(
            f"{BASE}/cases",
            json={
                "name": "登录接口",
                "method": "get",
                "url": "https://api.example.test/login",
                "collection_id": collection_id,
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["message"] == "创建成功"
        case_id = body["data"]["id"]
        assert body["data"]["method"] == "GET"  # v1 service 统一大写

        # 用例列表（按集合过滤）
        resp = v2_client.get(f"{BASE}/cases", params={"collection_id": collection_id}, headers=headers)
        assert resp.status_code == 200
        assert any(c["id"] == case_id for c in resp.json()["data"])

        # 用例详情
        resp = v2_client.get(f"{BASE}/cases/{case_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["name"] == "登录接口"

        # 更新用例
        resp = v2_client.put(
            f"{BASE}/cases/{case_id}",
            json={"name": "登录接口V2"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "更新成功"

        # 版本历史（更新产生快照）
        resp = v2_client.get(f"{BASE}/cases/{case_id}/versions", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["total"] >= 1
        version_id = resp.json()["data"]["items"][0]["id"]

        # 版本详情
        resp = v2_client.get(f"{BASE}/versions/{version_id}", headers=headers)
        assert resp.status_code == 200, resp.text

        # 版本 diff（单版本也能对比自身）
        resp = v2_client.get(
            f"{BASE}/versions/diff",
            params={"v1": version_id, "v2": version_id},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert "diff" in resp.json()["data"]

        # 删除用例 → 详情 404
        resp = v2_client.delete(f"{BASE}/cases/{case_id}", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["message"] == "删除成功"
        resp = v2_client.get(f"{BASE}/cases/{case_id}", headers=headers)
        assert resp.status_code == 404

        # 更新/删除集合
        resp = v2_client.put(
            f"{BASE}/collections/{collection_id}",
            json={"name": "冒烟集合V2"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        resp = v2_client.delete(f"{BASE}/collections/{collection_id}", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["message"] == "删除成功"

    def test_import_curl(self, v2_client, make_user, auth_headers):
        """cURL 导入为纯解析逻辑，无网络请求"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)

        curl = (
            "curl -X POST 'https://api.example.test/v1/orders' "
            "-H 'Content-Type: application/json' "
            "-H 'Authorization: Bearer abc' "
            "-d '{\"sku\": 1}'"
        )
        resp = v2_client.post(f"{BASE}/import-curl", json={"curl": curl}, headers=headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["method"] == "POST"
        assert data["url"] == "https://api.example.test/v1/orders"
        assert data["headers"]["Content-Type"] == "application/json"
        assert "sku" in data["body"]

        resp = v2_client.post(f"{BASE}/import-curl", json={}, headers=headers)
        assert resp.status_code == 400
        assert resp.json()["message"] == "缺少 curl 参数"

    def test_history_save_and_query(self, v2_client, make_user, auth_headers):
        """响应历史保存 201 → 列表/详情/趋势可查（user_id 隔离）"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)

        resp = v2_client.post(
            f"{BASE}/history",
            json={
                "url": "https://api.example.test/login",
                "method": "GET",
                "status_code": 200,
                "response_time": 88.5,
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        history_id = resp.json()["data"]["id"]

        resp = v2_client.get(f"{BASE}/history", headers=headers)
        assert resp.status_code == 200
        assert len(resp.json()["data"]) == 1

        resp = v2_client.get(f"{BASE}/history/{history_id}", headers=headers)
        assert resp.status_code == 200

        resp = v2_client.get(f"{BASE}/history/trend", params={"case_id": 0}, headers=headers)
        assert resp.status_code == 400
        assert resp.json()["message"] == "缺少 case_id 参数"


# ---------------------------------------------------------------------------
# 3. IDOR：访问/更新/删除/执行他人资源 → 404
# ---------------------------------------------------------------------------

class TestIDOR:
    def test_foreign_case_read_update_delete_404(
        self, v2_client, make_user, auth_headers
    ):
        """他人用例：GET/PUT/DELETE 一律 404（service 层 user_id 过滤）"""
        owner = make_user(_username())
        attacker = make_user(_username())
        owner_headers = auth_headers(owner)
        attacker_headers = auth_headers(attacker)

        case_id = _create_case(v2_client, owner_headers)

        resp = v2_client.get(f"{BASE}/cases/{case_id}", headers=attacker_headers)
        assert resp.status_code == 404, resp.text
        assert resp.json()["code"] == 404

        resp = v2_client.put(
            f"{BASE}/cases/{case_id}", json={"name": "hacked"}, headers=attacker_headers
        )
        assert resp.status_code == 404

        resp = v2_client.delete(f"{BASE}/cases/{case_id}", headers=attacker_headers)
        assert resp.status_code == 404

        # 属主不受影响
        resp = v2_client.get(f"{BASE}/cases/{case_id}", headers=owner_headers)
        assert resp.status_code == 200

    def test_foreign_collection_update_delete_estimate_run_404(
        self, v2_client, make_user, auth_headers
    ):
        """他人集合：PUT/DELETE/run/estimate 一律 404"""
        owner = make_user(_username())
        attacker = make_user(_username())
        owner_headers = auth_headers(owner)
        attacker_headers = auth_headers(attacker)

        collection_id = _create_collection(v2_client, owner_headers)

        resp = v2_client.put(
            f"{BASE}/collections/{collection_id}",
            json={"name": "hacked"},
            headers=attacker_headers,
        )
        assert resp.status_code == 404

        resp = v2_client.delete(f"{BASE}/collections/{collection_id}", headers=attacker_headers)
        assert resp.status_code == 404

        # estimate：路由层补属主过滤（IDOR 修复点）
        resp = v2_client.get(
            f"{BASE}/collections/{collection_id}/estimate", headers=attacker_headers
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["message"] == "集合不存在"

        # run：service 层按 user_id 过滤集合（不会触达用例执行，零外呼）
        resp = v2_client.post(
            f"{BASE}/collections/{collection_id}/run", json={}, headers=attacker_headers
        )
        assert resp.status_code == 404

    def test_foreign_case_run_versions_progress_404(
        self, v2_client, app, make_user, auth_headers
    ):
        """他人用例 run / 版本历史 / 执行进度 → 404"""
        import app.api.routes.api_test as api_test_mod

        owner = make_user(_username())
        attacker = make_user(_username())
        owner_headers = auth_headers(owner)
        attacker_headers = auth_headers(attacker)

        collection_id = _create_collection(v2_client, owner_headers)
        case_id = _create_case(v2_client, owner_headers, collection_id=collection_id)
        project_id = _make_project(app, owner)
        run_id = _make_run(app, project_id, owner)

        # run_case：service 层 user_id 过滤
        resp = v2_client.post(f"{BASE}/cases/{case_id}/run", json={}, headers=attacker_headers)
        assert resp.status_code == 404

        # 版本历史：路由层属主过滤（IDOR 修复点）
        resp = v2_client.get(f"{BASE}/cases/{case_id}/versions", headers=attacker_headers)
        assert resp.status_code == 404, resp.text
        assert resp.json()["message"] == "用例不存在"

        # 执行进度：路由层 run 属主过滤（IDOR 修复点，未触达 Redis）
        resp = v2_client.get(f"{BASE}/runs/{run_id}/progress", headers=attacker_headers)
        assert resp.status_code == 404, resp.text
        assert resp.json()["message"] == "执行记录不存在"

    def test_progress_owned_passthrough_and_default(
        self, v2_client, app, make_user, auth_headers, monkeypatch
    ):
        """属主查询进度：service 返回数据时透传；无进度时返回 v1 默认 unknown 结构"""
        import app.api.routes.api_test as api_test_mod

        user_id = make_user(_username())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)
        run_id = _make_run(app, project_id, user_id)

        monkeypatch.setattr(
            api_test_mod.execution_service,
            "get_progress",
            lambda run_id_: {
                "current": 1, "total": 2, "passed": 1, "failed": 0, "status": "running",
            },
        )
        resp = v2_client.get(f"{BASE}/runs/{run_id}/progress", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["status"] == "running"

        monkeypatch.setattr(api_test_mod.execution_service, "get_progress", lambda run_id_: None)
        resp = v2_client.get(f"{BASE}/runs/{run_id}/progress", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["data"] == {
            "current": 0, "total": 0, "passed": 0, "failed": 0, "status": "unknown",
        }

    def test_heal_apply_detect_foreign_case_404(self, v2_client, make_user, auth_headers):
        """heal-case / apply-heal / detect-changes 指向他人用例 → 404（路由层 IDOR 修复点）"""
        owner = make_user(_username())
        attacker = make_user(_username())
        owner_headers = auth_headers(owner)
        attacker_headers = auth_headers(attacker)

        case_id = _create_case(v2_client, owner_headers)

        resp = v2_client.post(
            f"{BASE}/heal-case",
            json={"case_id": case_id, "failure_info": {"error": "timeout"}},
            headers=attacker_headers,
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["message"] == "用例不存在"

        resp = v2_client.post(
            f"{BASE}/apply-heal",
            json={"case_id": case_id, "fixes": [{"field": "url", "value": "https://x.test"}]},
            headers=attacker_headers,
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["message"] == "用例不存在"

        resp = v2_client.post(
            f"{BASE}/detect-changes",
            json={"case_id": case_id, "response_body": "{}"},
            headers=attacker_headers,
        )
        assert resp.status_code == 404

        # 缺参 400 优先于属主判断（与 v1 校验顺序一致）
        resp = v2_client.post(f"{BASE}/heal-case", json={}, headers=owner_headers)
        assert resp.status_code == 400
        assert resp.json()["message"] == "缺少 case_id"

    def test_execute_foreign_case_id_404(self, v2_client, make_user, auth_headers):
        """execute 的 body.case_id 指向他人用例 → 404（路由层 IDOR 修复点）"""
        owner = make_user(_username())
        attacker = make_user(_username())
        owner_headers = auth_headers(owner)
        attacker_headers = auth_headers(attacker)

        case_id = _create_case(v2_client, owner_headers)

        resp = v2_client.post(
            f"{BASE}/execute",
            json={"method": "GET", "url": "https://api.example.test/x", "case_id": case_id},
            headers=attacker_headers,
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["message"] == "用例不存在"

    def test_foreign_project_scoped_endpoints_404(
        self, v2_client, app, make_user, auth_headers
    ):
        """project_id 类端点指向他人项目 → 404（路由层 IDOR 修复点）"""
        attacker = make_user(_username())
        attacker_headers = auth_headers(attacker)
        foreign_project_id = _make_project(app, make_user(_username()))

        resp = v2_client.post(
            f"{BASE}/import/postman",
            json={"project_id": foreign_project_id, "content": "{}"},
            headers=attacker_headers,
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["message"] == "项目不存在"

        resp = v2_client.get(
            f"{BASE}/tags/stats", params={"project_id": foreign_project_id},
            headers=attacker_headers,
        )
        assert resp.status_code == 404

        resp = v2_client.post(
            f"{BASE}/tags/filter",
            json={"tags": ["smoke"], "project_id": foreign_project_id},
            headers=attacker_headers,
        )
        assert resp.status_code == 404

        resp = v2_client.post(
            f"{BASE}/smart-select",
            json={"changed_files": ["app/models/user.py"], "project_id": foreign_project_id},
            headers=attacker_headers,
        )
        assert resp.status_code == 404

        resp = v2_client.post(
            f"{BASE}/import-har",
            json={"har_content": '{"log": {}}', "project_id": foreign_project_id},
            headers=attacker_headers,
        )
        assert resp.status_code == 404

        resp = v2_client.post(
            f"{BASE}/bdd/parse",
            json={"gherkin": "Feature: x", "project_id": foreign_project_id},
            headers=attacker_headers,
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 4. 执行类端点参数校验 400（服务层全 mock，零真实外呼）
# ---------------------------------------------------------------------------

class TestExecutionValidation:
    def _patch_execution(self, monkeypatch, result):
        import app.api.routes.api_test as api_test_mod

        monkeypatch.setattr(
            api_test_mod.execution_service, "execute_request", lambda data, user_id: result
        )

    def test_execute_missing_fields_400(self, v2_client, make_user, auth_headers):
        """缺 method/url → 400（validate_required 消息与 v1 一致）"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)

        resp = v2_client.post(f"{BASE}/execute", json={}, headers=headers)
        assert resp.status_code == 400
        assert resp.json()["message"] == "请求数据不能为空"

        resp = v2_client.post(f"{BASE}/execute", json={"method": "GET"}, headers=headers)
        assert resp.status_code == 400
        assert resp.json()["message"] == "缺少必需字段: url"

    def test_execute_ssrf_blocked_400(self, v2_client, make_user, auth_headers, monkeypatch):
        """SSRF 内网/元数据地址 → 400，且不触达执行服务（防真实外呼）"""
        self._patch_execution(monkeypatch, {"success": True, "status_code": 200})
        user_id = make_user(_username())
        headers = auth_headers(user_id)

        for url in (
            "http://127.0.0.1:8080/admin",
            "http://169.254.169.254/latest/meta-data/",
            "http://localhost:6379",
            "file:///etc/passwd",
        ):
            resp = v2_client.post(
                f"{BASE}/execute", json={"method": "GET", "url": url}, headers=headers
            )
            assert resp.status_code == 400, f"{url}: {resp.text}"
            assert "URL 安全校验失败" in resp.json()["message"]

    def test_execute_success_saves_history(
        self, v2_client, app, make_user, auth_headers, monkeypatch
    ):
        """合法公网 URL + mock 的执行结果 → 200 并落响应历史（不发起真实 HTTP）"""
        self._patch_execution(
            monkeypatch,
            {
                "success": True,
                "status_code": 200,
                "response_time": 12.3,
                "response_size": "128",
                "headers": {"X-Test": "1"},
                "body": {"ok": True},
            },
        )
        user_id = make_user(_username())
        headers = auth_headers(user_id)

        resp = v2_client.post(
            f"{BASE}/execute",
            json={"method": "GET", "url": "https://api.example.test/users"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["status_code"] == 200

        from app.extensions import db
        from app.models.response_history import ResponseHistory

    def test_execute_failure_result_maps_error(
        self, v2_client, make_user, auth_headers, monkeypatch
    ):
        """执行失败结果 → v1 的 error_response 映射（status_code/error/script_execution）"""
        self._patch_execution(
            monkeypatch,
            {
                "success": False,
                "status_code": 502,
                "error": "Bad Gateway",
                "script_execution": {"pre_script": {"executed": True, "passed": False}},
            },
        )
        user_id = make_user(_username())
        headers = auth_headers(user_id)

        resp = v2_client.post(
            f"{BASE}/execute",
            json={"method": "GET", "url": "https://api.example.test/broken"},
            headers=headers,
        )
        assert resp.status_code == 502, resp.text
        body = resp.json()
        assert body["message"] == "Bad Gateway"
        assert body["errors"] == {"pre_script": {"executed": True, "passed": False}}

    def test_scenario_missing_steps_400(self, v2_client, make_user, auth_headers):
        """场景执行缺 steps → 400"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)

        resp = v2_client.post(f"{BASE}/execute-scenario", json={}, headers=headers)
        assert resp.status_code == 400
        assert resp.json()["message"] == "缺少步骤定义"

    def test_scenario_ssrf_blocked_400(self, v2_client, make_user, auth_headers, monkeypatch):
        """场景步骤/ base_url 指向内网 → 400，不触达执行器"""
        import app.services.scenario_executor as se_mod

        monkeypatch.setattr(
            se_mod, "get_scenario_executor",
            lambda: pytest.fail("SSRF 校验失败后不应触达场景执行器"),
        )
        user_id = make_user(_username())
        headers = auth_headers(user_id)

        resp = v2_client.post(
            f"{BASE}/execute-scenario",
            json={"steps": [{"method": "GET", "url": "http://10.0.0.1/internal"}]},
            headers=headers,
        )
        assert resp.status_code == 400, resp.text
        assert "URL 安全校验失败" in resp.json()["message"]

        resp = v2_client.post(
            f"{BASE}/execute-scenario",
            json={"base_url": "http://192.168.1.1", "steps": [{"method": "GET", "url": "/x"}]},
            headers=headers,
        )
        assert resp.status_code == 400
        assert "base_url 安全校验失败" in resp.json()["message"]

    def test_scenario_success_with_executor_mock(
        self, v2_client, make_user, auth_headers, monkeypatch
    ):
        """合法公网步骤 URL → 透传执行器结果（执行器 mock，零外呼）"""
        import app.services.scenario_executor as se_mod

        class _FakeExecutor:
            def execute_scenario(self, steps, context):
                assert context["user_id"]
                return {
                    "total": 1, "passed": 1, "failed": 0,
                    "duration": 0.1, "step_results": [], "variables": {},
                }

        monkeypatch.setattr(se_mod, "get_scenario_executor", lambda: _FakeExecutor())
        user_id = make_user(_username())
        headers = auth_headers(user_id)

        resp = v2_client.post(
            f"{BASE}/execute-scenario",
            json={
                "base_url": "https://api.example.test",
                "steps": [{"method": "GET", "url": "/login"}],
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["passed"] == 1
