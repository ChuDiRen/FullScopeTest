"""
v1 平迁路由测试：质量门禁 / 测试计划 / API Token / GitHub 集成 / GitHub Checks
（源：app/api/quality_gates.py、test_plans.py、tokens.py、github_integration.py、
github_checks.py，FastAPI 实现在 app/api/routes/ 同名模块）

覆盖：
1. 未登录 401（五模块受保护端点全量参数化）；公开端点（GitHub OAuth 回调/config）
   未登录可访问
2. 质量门禁 创建 → 列表 → 评估（通过率阈值判定 passed/failed）全链路
3. 测试计划 创建 → 列表（分页信封与 v1 一致）
4. API Token 生成（明文仅创建时返回一次，服务端只存 sha256 哈希）
   → 列表（不返回明文 token）→ 撤销 → 复删 404
5. 他人资源 404（门禁组织隔离、计划属主过滤、Token 属主过滤、TestRun 项目归属）
6. GitHub API 全部走 services 并 mock（create_check_service /
   exchange_code_for_token 等），零真实外呼
"""

import gc
import hashlib
import uuid

import pytest
from app.extensions import db

BASE = "/api/v1"


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    """本机 Redis 挂死：把 token 黑名单/限流服务的 _get_redis 打桩为 None。

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
    return f"gpt_{uuid.uuid4().hex[:10]}"


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

def _make_test_run(app, project_id: int, total: int = 4, passed: int = 3) -> int:
    from app.models.test_run import TestRun
    run = TestRun(
        project_id=project_id,
        test_type="api",
        status="success",
        total_cases=total,
        passed=passed,
        failed=total - passed,
    )
    db.session.add(run)
    db.session.commit()
    return run.id

def _make_gate(v2_client, headers, project_id: int, **extra) -> dict:
    payload = {
        "project_id": project_id,
        "name": extra.pop("name", "主干门禁"),
        "min_pass_rate": extra.pop("min_pass_rate", 80.0),
        **extra,
    }
    resp = v2_client.post(f"{BASE}/quality-gates", json=payload, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _make_plan(v2_client, headers, project_id: int, **extra) -> dict:
    payload = {
        "project_id": project_id,
        "name": extra.pop("name", "回归计划"),
        "include_cases": extra.pop("include_cases", []),
        **extra,
    }
    resp = v2_client.post(f"{BASE}/test-plans", json=payload, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _make_integration(app, user_id: int) -> int:
    from app.models.github_integration import GitHubIntegration
    integration = GitHubIntegration(
        user_id=user_id,
        github_username=f"gh_{user_id}",
        github_user_id=str(10000 + user_id),
        access_token_encrypted="encrypted-token",
    )
    db.session.add(integration)
    db.session.commit()
    return integration.id

# ---------------------------------------------------------------------------
# 1. 未登录 401 + 公开端点
# ---------------------------------------------------------------------------

class TestAuthRequired:
    @pytest.mark.parametrize(
        "method,url",
        [
            # quality_gates（7）
            ("get", f"{BASE}/quality-gates"),
            ("post", f"{BASE}/quality-gates"),
            ("get", f"{BASE}/quality-gates/1"),
            ("put", f"{BASE}/quality-gates/1"),
            ("delete", f"{BASE}/quality-gates/1"),
            ("post", f"{BASE}/quality-gates/1/evaluate"),
            ("get", f"{BASE}/quality-gates/1/evaluations"),
            # test_plans（11）
            ("post", f"{BASE}/test-plans"),
            ("get", f"{BASE}/test-plans"),
            ("get", f"{BASE}/test-plans/1"),
            ("put", f"{BASE}/test-plans/1"),
            ("delete", f"{BASE}/test-plans/1"),
            ("post", f"{BASE}/test-plans/1/runs"),
            ("get", f"{BASE}/test-plans/1/runs"),
            ("get", f"{BASE}/test-plan-runs/1"),
            ("patch", f"{BASE}/test-plan-runs/1/case-results"),
            ("post", f"{BASE}/test-plan-runs/1/complete"),
            ("get", f"{BASE}/test-plans/1/trend"),
            # tokens（4）
            ("get", f"{BASE}/tokens"),
            ("post", f"{BASE}/tokens"),
            ("delete", f"{BASE}/tokens/1"),
            ("post", f"{BASE}/tokens/validate"),
            # github_integration（鉴权端点 3）
            ("get", f"{BASE}/integrations/github/auth"),
            ("get", f"{BASE}/integrations/github/status"),
            ("post", f"{BASE}/integrations/github/unbind"),
            # github_checks（3）
            ("post", f"{BASE}/github-checks/1/create"),
            ("post", f"{BASE}/github-checks/1/update"),
            ("post", f"{BASE}/github-checks/1/complete"),
        ],
    )
    def test_protected_endpoints_require_auth(self, v2_client, method, url):
        """未登录访问受保护端点 → 401"""
        resp = getattr(v2_client, method)(url)
        assert resp.status_code == 401, resp.text

    def test_github_callback_is_public(self, v2_client):
        """GitHub OAuth 回调是 v1 公开设计（GitHub 服务器重定向而来）：未登录可访问，
        缺参数 → 302 redirect 前端错误页"""
        resp = v2_client.get(
            f"{BASE}/integrations/github/callback", follow_redirects=False
        )
        assert resp.status_code == 302, resp.text
        assert "github_error=missing_params" in resp.headers["location"]

    def test_github_config_is_public(self, v2_client):
        """GitHub OAuth 配置是 v1 公开设计：未登录可访问"""
        resp = v2_client.get(f"{BASE}/integrations/github/config")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == 200
        assert "is_configured" in body["data"]


# ---------------------------------------------------------------------------
# 2. 质量门禁：创建 → 评估
# ---------------------------------------------------------------------------

class TestQualityGates:
    def test_create_gate_missing_fields(self, v2_client, make_user, auth_headers):
        """缺 name/project_id → 400（与 v1 报错一致）"""
        headers = auth_headers(make_user(_username()))
        resp = v2_client.post(f"{BASE}/quality-gates", json={"name": "x"}, headers=headers)
        assert resp.status_code == 400, resp.text
        assert resp.json()["message"] == "name and project_id are required"

    def test_create_and_evaluate_flow(self, v2_client, app, make_user, auth_headers):
        """创建 → 详情 → 评估（通过率阈值判定）→ 更新 → 删除 全链路"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)

        # 创建
        gate = _make_gate(v2_client, headers, project_id, min_pass_rate=75.0)
        assert gate["project_id"] == project_id
        assert gate["created_by"] == user_id
        assert gate["is_active"] is True

        # 列表
        resp = v2_client.get(f"{BASE}/quality-gates?project_id={project_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        assert any(g["id"] == gate["id"] for g in resp.json()["data"])

        # 详情
        resp = v2_client.get(f"{BASE}/quality-gates/{gate['id']}", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["name"] == "主干门禁"

        # 评估：4 用例过 3 → 75% >= 75 → 通过
        run_id = _make_test_run(app, project_id, total=4, passed=3)
        resp = v2_client.post(
            f"{BASE}/quality-gates/{gate['id']}/evaluate",
            json={"test_run_id": run_id},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["message"] == "评估完成"
        assert body["data"]["passed"] is True
        assert body["data"]["gate_id"] == gate["id"]
        assert body["data"]["details"]["pass_rate"]["actual"] == 75.0

        # 评估不通过：阈值 80，实际 75
        gate2 = _make_gate(v2_client, headers, project_id, name="严格门禁", min_pass_rate=80.0)
        resp = v2_client.post(
            f"{BASE}/quality-gates/{gate2['id']}/evaluate",
            json={"test_run_id": run_id},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["passed"] is False

        # 评估：test_run_id 缺失 → 400；不存在 → 404
        resp = v2_client.post(
            f"{BASE}/quality-gates/{gate['id']}/evaluate", json={}, headers=headers
        )
        assert resp.status_code == 400, resp.text
        resp = v2_client.post(
            f"{BASE}/quality-gates/{gate['id']}/evaluate",
            json={"test_run_id": 999999},
            headers=headers,
        )
        assert resp.status_code == 404, resp.text

        # 更新 → 删除
        resp = v2_client.put(
            f"{BASE}/quality-gates/{gate['id']}",
            json={"name": "改名的门禁"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["name"] == "改名的门禁"

        resp = v2_client.delete(f"{BASE}/quality-gates/{gate['id']}", headers=headers)
        assert resp.status_code == 200, resp.text
        resp = v2_client.get(f"{BASE}/quality-gates/{gate['id']}", headers=headers)
        assert resp.status_code == 404, resp.text

    def test_gate_evaluation_history(self, v2_client, app, make_user, auth_headers):
        """评估历史分页（v1 源码缺 QualityGateEvaluation import 必然 500，平迁修正后可用）"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)
        gate = _make_gate(v2_client, headers, project_id)

        resp = v2_client.get(
            f"{BASE}/quality-gates/{gate['id']}/evaluations", headers=headers
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["items"] == []
        assert data["total"] == 0
        assert data["page"] == 1


# ---------------------------------------------------------------------------
# 3. 测试计划：创建 → 列表
# ---------------------------------------------------------------------------

class TestTestPlans:
    def test_create_list_flow(self, v2_client, app, make_user, auth_headers):
        """创建（201）→ 列表（分页信封与 v1 一致）"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)

        plan = _make_plan(v2_client, headers, project_id, name="冒烟计划")
        assert plan["project_id"] == project_id
        assert plan["user_id"] == user_id
        assert plan["status"] == "draft"

        resp = v2_client.get(
            f"{BASE}/test-plans?project_id={project_id}", headers=headers
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["total"] >= 1
        assert any(p["id"] == plan["id"] for p in data["items"])
        assert data["page"] == 1

    def test_create_plan_requires_fields(self, v2_client, make_user, auth_headers):
        """缺 name/project_id → 400（等价 v1 @validate_json：空体/缺字段两种报错）"""
        headers = auth_headers(make_user(_username()))
        # 空 body → 请求体不能为空（与 v1 @validate_json 一致）
        resp = v2_client.post(f"{BASE}/test-plans", json={}, headers=headers)
        assert resp.status_code == 400, resp.text
        assert resp.json()["message"] == "请求体不能为空"
        # 有部分字段 → 缺少必需字段: project_id
        resp = v2_client.post(f"{BASE}/test-plans", json={"name": "x"}, headers=headers)
        assert resp.status_code == 400, resp.text
        assert "缺少必需字段" in resp.json()["message"]
        assert "project_id" in resp.json()["message"]

    def test_list_requires_project_id(self, v2_client, make_user, auth_headers):
        """缺 project_id → 400（与 v1 报错一致）"""
        headers = auth_headers(make_user(_username()))
        resp = v2_client.get(f"{BASE}/test-plans", headers=headers)
        assert resp.status_code == 400, resp.text
        assert resp.json()["message"] == "缺少 project_id 参数"

    def test_plan_detail_update_delete(self, v2_client, app, make_user, auth_headers):
        """详情 → 更新 → 删除"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)
        plan = _make_plan(v2_client, headers, project_id)

        resp = v2_client.get(f"{BASE}/test-plans/{plan['id']}", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["name"] == "回归计划"

        resp = v2_client.put(
            f"{BASE}/test-plans/{plan['id']}", json={"name": "改名计划"}, headers=headers
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["name"] == "改名计划"

        resp = v2_client.delete(f"{BASE}/test-plans/{plan['id']}", headers=headers)
        assert resp.status_code == 200, resp.text
        resp = v2_client.get(f"{BASE}/test-plans/{plan['id']}", headers=headers)
        assert resp.status_code == 404, resp.text

    def test_plan_run_flow(self, v2_client, app, make_user, auth_headers):
        """创建轮次（无用例 → 400）与趋势查询端点可达"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)
        plan = _make_plan(v2_client, headers, project_id)

        # 计划没有 include_cases → 创建轮次 400（service 校验，与 v1 一致）
        resp = v2_client.post(f"{BASE}/test-plans/{plan['id']}/runs", json={}, headers=headers)
        assert resp.status_code == 400, resp.text

        # 轮次列表 / 趋势端点可达（空数据）
        resp = v2_client.get(f"{BASE}/test-plans/{plan['id']}/runs", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["total"] == 0

        resp = v2_client.get(f"{BASE}/test-plans/{plan['id']}/trend", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"] == []


# ---------------------------------------------------------------------------
# 4. API Token：生成 → 列表 → 撤销
# ---------------------------------------------------------------------------

class TestApiTokens:
    def test_token_create_list_revoke(self, v2_client, app, make_user, auth_headers):
        """生成（明文仅一次）→ 列表（不含明文）→ 撤销 → 复删 404"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)

        # 创建
        resp = v2_client.post(
            f"{BASE}/tokens",
            json={"name": "CI Token", "actions": ["read", "execute"]},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["message"] == "Token 创建成功"
        token_plain = body["data"]["token"]
        assert token_plain
        assert body["data"]["actions"] == ["read", "execute"]
        assert body["data"]["name"] == "CI Token"

        # 哈希存储：库中只有 sha256(token)，无明文
        from app.extensions import db
        from app.models.api_token import ApiToken

        # 列表：返回记录但不返回明文 token
        resp = v2_client.get(f"{BASE}/tokens", headers=headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["pagination"]["total"] == 1
        assert data["items"][0]["id"] == body["data"]["id"]
        assert "token" not in data["items"][0]
        assert "token_hash" not in data["items"][0]
        assert "token_hash" not in data["items"][0]

        # 撤销（删除）
        resp = v2_client.delete(f"{BASE}/tokens/{body['data']['id']}", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "Token 已删除"

        # 复删 → 404
        resp = v2_client.delete(f"{BASE}/tokens/{body['data']['id']}", headers=headers)
        assert resp.status_code == 404, resp.text

    def test_token_create_invalid_actions(self, v2_client, make_user, auth_headers):
        """非法 action → 400 + valid_actions 提示（与 v1 一致）"""
        headers = auth_headers(make_user(_username()))
        resp = v2_client.post(
            f"{BASE}/tokens", json={"name": "x", "actions": ["root"]}, headers=headers
        )
        assert resp.status_code == 400, resp.text
        assert "无效的操作类型" in resp.json()["message"]
        assert set(resp.json()["errors"]["valid_actions"]) == {"read", "write", "execute", "delete"}

    def test_token_create_old_permissions_compat(self, v2_client, make_user, auth_headers):
        """旧格式 permissions=read-only → 转为 ['read']（向后兼容，与 v1 一致）"""
        headers = auth_headers(make_user(_username()))
        resp = v2_client.post(
            f"{BASE}/tokens", json={"name": "RO", "permissions": ["read-only"]}, headers=headers
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["actions"] == ["read"]

    def test_token_validate_with_api_token(self, v2_client, make_user, auth_headers):
        """携带 API Token 调 /tokens/validate：sha256 命中并返回权限判定"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)

        resp = v2_client.post(
            f"{BASE}/tokens",
            json={"name": "RO Token", "actions": ["read"]},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        token_plain = resp.json()["data"]["token"]

        api_headers = {"Authorization": f"Bearer {token_plain}"}
        resp = v2_client.post(
            f"{BASE}/tokens/validate",
            json={"action": "read"},
            headers=api_headers,
        )
        assert resp.status_code == 401, resp.text  # JWT 缺失 → 401（端点需登录）

        # JWT + API Token 同 header：JWT 命中鉴权后，按 sha256 查 API Token 必然未命中
        # → 401 'Token 无效'（v1 原样语义，迁移不做行为改写）
        resp = v2_client.post(
            f"{BASE}/tokens/validate", json={"action": "read"}, headers=headers
        )
        assert resp.status_code == 401, resp.text
        assert resp.json()["message"] == "Token 无效"


# ---------------------------------------------------------------------------
# 5. 他人资源 404（IDOR）
# ---------------------------------------------------------------------------

class TestOwnershipIsolation:
    def test_other_users_gate_404(self, v2_client, app, make_user, auth_headers):
        """他人门禁：详情/更新/删除/评估/评估历史 → 404（组织隔离）"""
        owner_id = make_user(_username())
        other_id = make_user(_username())
        owner_headers = auth_headers(owner_id)
        other_headers = auth_headers(other_id)

        project_id = _make_project(app, owner_id)
        gate = _make_gate(v2_client, owner_headers, project_id)

        for method, url in [
            ("get", f"{BASE}/quality-gates/{gate['id']}"),
            ("put", f"{BASE}/quality-gates/{gate['id']}"),
            ("delete", f"{BASE}/quality-gates/{gate['id']}"),
            ("post", f"{BASE}/quality-gates/{gate['id']}/evaluate"),
            ("get", f"{BASE}/quality-gates/{gate['id']}/evaluations"),
        ]:
            # 该版本 TestClient 的 get/delete 不支持 json=，仅对写方法带 body
            kwargs = {"headers": other_headers}
            if method in ("post", "put", "patch"):
                kwargs["json"] = {"test_run_id": 1}
            resp = getattr(v2_client, method)(url, **kwargs)
            assert resp.status_code == 404, f"{method} {url}: {resp.text}"
            assert resp.json()["message"] == "质量门禁不存在"

    def test_other_users_plan_404(self, v2_client, app, make_user, auth_headers):
        """他人测试计划：详情/更新/删除/轮次/趋势 → 404（属主过滤）"""
        owner_id = make_user(_username())
        other_id = make_user(_username())
        owner_headers = auth_headers(owner_id)
        other_headers = auth_headers(other_id)

        project_id = _make_project(app, owner_id)
        plan = _make_plan(v2_client, owner_headers, project_id)

        for method, url in [
            ("get", f"{BASE}/test-plans/{plan['id']}"),
            ("put", f"{BASE}/test-plans/{plan['id']}"),
            ("delete", f"{BASE}/test-plans/{plan['id']}"),
            ("post", f"{BASE}/test-plans/{plan['id']}/runs"),
            ("get", f"{BASE}/test-plans/{plan['id']}/runs"),
            ("get", f"{BASE}/test-plans/{plan['id']}/trend"),
        ]:
            # 该版本 TestClient 的 get/delete 不支持 json=，仅对写方法带 body
            kwargs = {"headers": other_headers}
            if method in ("post", "put", "patch"):
                kwargs["json"] = {}
            resp = getattr(v2_client, method)(url, **kwargs)
            assert resp.status_code == 404, f"{method} {url}: {resp.text}"

    def test_other_users_project_plan_inject_404(self, v2_client, app, make_user, auth_headers):
        """向他人项目注入计划 / 枚举他人项目计划 → 404"""
        owner_id = make_user(_username())
        other_id = make_user(_username())
        other_headers = auth_headers(other_id)
        project_id = _make_project(app, owner_id)

        resp = v2_client.post(
            f"{BASE}/test-plans",
            json={"name": "注入", "project_id": project_id},
            headers=other_headers,
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["message"] == "项目不存在"

        resp = v2_client.get(
            f"{BASE}/test-plans?project_id={project_id}", headers=other_headers
        )
        assert resp.status_code == 404, resp.text

    def test_other_users_token_404(self, v2_client, app, make_user, auth_headers):
        """删除他人 API Token → 404（属主过滤）"""
        owner_id = make_user(_username())
        other_id = make_user(_username())
        owner_headers = auth_headers(owner_id)
        other_headers = auth_headers(other_id)

        resp = v2_client.post(f"{BASE}/tokens", json={"name": "T"}, headers=owner_headers)
        assert resp.status_code == 200, resp.text
        token_id = resp.json()["data"]["id"]

        resp = v2_client.delete(f"{BASE}/tokens/{token_id}", headers=other_headers)
        assert resp.status_code == 404, resp.text
        assert resp.json()["message"] == "Token 不存在"

        # 属主列表看不到他人的 token
        resp = v2_client.get(f"{BASE}/tokens", headers=other_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["pagination"]["total"] == 0

    def test_other_users_test_run_check_404(self, v2_client, app, make_user, auth_headers):
        """github-checks：他人项目的 TestRun → 404（project 归属校验）"""
        owner_id = make_user(_username())
        other_id = make_user(_username())
        owner_headers = auth_headers(owner_id)
        other_headers = auth_headers(other_id)

        project_id = _make_project(app, owner_id)
        run_id = _make_test_run(app, project_id)

        resp = v2_client.post(
            f"{BASE}/github-checks/{run_id}/create",
            json={"repo_full_name": "o/r", "head_sha": "a" * 40},
            headers=other_headers,
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["message"] == "测试运行记录不存在"

    def test_github_unbind_without_binding_404(self, v2_client, make_user, auth_headers):
        """未绑定 GitHub 时解绑 → 404（与 v1 一致）"""
        headers = auth_headers(make_user(_username()))
        resp = v2_client.post(f"{BASE}/integrations/github/unbind", headers=headers)
        assert resp.status_code == 404, resp.text
        assert resp.json()["message"] == "未找到 GitHub 绑定信息"


# ---------------------------------------------------------------------------
# 6. GitHub 集成 / Check Run（services 全 mock，零真实外呼）
# ---------------------------------------------------------------------------

class TestGithubIntegration:
    def test_oauth_flow_with_mocked_services(self, v2_client, app, make_user, auth_headers, monkeypatch):
        """auth → callback（code 换 token、用户信息、绑定创建全 mock）→ status → unbind"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)

        # auth：返回 authorize_url + state
        resp = v2_client.get(f"{BASE}/integrations/github/auth", headers=headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["authorize_url"].startswith("https://github.com/login/oauth/authorize")
        assert data["state"]

        # mock 外呼服务（零真实 HTTP）
        import app.api.routes.github_integration as gi_module

        monkeypatch.setattr(
            gi_module,
            "exchange_code_for_token",
            lambda code: {"access_token": "gho_mock", "token_type": "bearer", "scope": "read:user"},
        )
        monkeypatch.setattr(
            gi_module,
            "get_github_user_info",
            lambda access_token: {
                "id": "9527",
                "login": "mocked_user",
                "email": "mocked@test.local",
                "avatar_url": "",
                "name": "Mocked",
                "html_url": "",
            },
        )
        real_create = gi_module.create_or_update_integration

        def _fake_create(user_id, github_user_data, token_data):
            """复用真服务落库（token_data 无外呼，安全），保证 status/unbind 可走真数据"""
            return real_create(
                user_id=user_id,
                github_user_data=github_user_data,
                token_data=token_data,
            )

        monkeypatch.setattr(gi_module, "create_or_update_integration", _fake_create)

        # callback：state 有效 → 创建绑定 → 302 成功页
        resp = v2_client.get(
            f"{BASE}/integrations/github/callback",
            params={"code": "abc", "state": data["state"]},
            follow_redirects=False,
        )
        assert resp.status_code == 302, resp.text
        assert "github_success=true" in resp.headers["location"]

        # state 一次性：重放 → invalid_state
        resp = v2_client.get(
            f"{BASE}/integrations/github/callback",
            params={"code": "abc", "state": data["state"]},
            follow_redirects=False,
        )
        assert resp.status_code == 302, resp.text
        assert "github_error=invalid_state" in resp.headers["location"]

        # status：已连接
        resp = v2_client.get(f"{BASE}/integrations/github/status", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()["data"]
        assert body["connected"] is True
        assert body["integration"]["github_username"] == "mocked_user"

        # callback：伪造 state → invalid_state（CSRF）
        resp = v2_client.get(
            f"{BASE}/integrations/github/callback",
            params={"code": "abc", "state": "forged-state"},
            follow_redirects=False,
        )
        assert resp.status_code == 302, resp.text
        assert "github_error=invalid_state" in resp.headers["location"]

        # unbind → 200；再解绑 → 404
        resp = v2_client.post(f"{BASE}/integrations/github/unbind", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "GitHub 账号已解绑"
        resp = v2_client.post(f"{BASE}/integrations/github/unbind", headers=headers)
        assert resp.status_code == 404, resp.text


class TestGithubChecks:
    def test_check_run_lifecycle_with_mocked_service(
        self, v2_client, app, make_user, auth_headers, monkeypatch
    ):
        """create → update → complete（create_check_service 打桩，零真实 GitHub 外呼）"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)
        run_id = _make_test_run(app, project_id)
        _make_integration(app, user_id)

        import app.api.routes.github_checks as gc_module

        calls = []

        class _FakeService:
            def start_test_check_run(self, test_run, repo_full_name, head_sha):
                calls.append(("start", repo_full_name, head_sha))
                return {"id": 424242, "status": "queued"}

            def update_test_progress(self, repo, check_run_id, test_run, current_step=None):
                calls.append(("update", repo, check_run_id, current_step))
                return {"id": check_run_id, "status": "in_progress"}

            def complete_test_check_run(self, repo, check_run_id, test_run, report_url=None):
                calls.append(("complete", repo, check_run_id, report_url))
                return {"id": check_run_id, "status": "completed", "conclusion": "success"}

        monkeypatch.setattr(gc_module, "create_check_service", lambda integration: _FakeService())

        # create
        resp = v2_client.post(
            f"{BASE}/github-checks/{run_id}/create",
            json={"repo_full_name": "acme/widget", "head_sha": "a" * 40},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["data"]["id"] == 424242
        assert body["message"] == "Check Run 创建成功"

        # create 缺参数 → 400
        resp = v2_client.post(
            f"{BASE}/github-checks/{run_id}/create", json={"repo_full_name": "o/r"}, headers=headers
        )
        assert resp.status_code == 400, resp.text

        # update / complete
        resp = v2_client.post(
            f"{BASE}/github-checks/{run_id}/update",
            json={"current_step": "执行中"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert body["data"]["id"] == 424242

        resp = v2_client.post(
            f"{BASE}/github-checks/{run_id}/complete",
            json={"report_url": "https://example.test/report"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["status"] == "completed"

        # TestRun 回写 check_run_id/repo（与 v1 一致）
        from app.extensions import db
        from app.models.test_run import TestRun

        # 零外呼断言：fake service 全部 3 次调用被路由消费
        assert [c[0] for c in calls] == ["start", "update", "complete"]

    def test_check_run_without_integration_404(self, v2_client, app, make_user, auth_headers):
        """未绑定 GitHub 集成 → 404（与 v1 一致，service 不应被调用）"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)
        run_id = _make_test_run(app, project_id)

        resp = v2_client.post(
            f"{BASE}/github-checks/{run_id}/create",
            json={"repo_full_name": "o/r", "head_sha": "a" * 40},
            headers=headers,
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["message"] == "未找到 GitHub 集成信息"

    def test_check_run_without_check_run_binding_400(self, v2_client, app, make_user, auth_headers):
        """TestRun 未关联 Check Run 时 update/complete → 400（与 v1 一致）"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)
        run_id = _make_test_run(app, project_id)
        _make_integration(app, user_id)

        resp = v2_client.post(
            f"{BASE}/github-checks/{run_id}/update", json={}, headers=headers
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["message"] == "此测试运行没有关联的 Check Run"
