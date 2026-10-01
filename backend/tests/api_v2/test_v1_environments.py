"""
v1 environments 平迁路由测试（FastAPI 实现，自 backend/app/api/environments.py 平迁）

覆盖：
1. 未登录访问全部 11 条受保护路由 → 401
2. 创建 → 列表 → 详情 → 更新 → 设默认 → 导出/导入 → 删除 全链路
3. 访问/修改他人资源 → 404（IDOR 修复验证：Environment 模型无 user_id，
   属主过滤经 Project.owner_id join 实现，v1 的越权 403 统一为 404）

环境说明：
- 本机 Redis 挂死/不可用：autouse fixture 把 token_blacklist 与
  rate_limit_service 的 _get_redis 替换为 lambda: None（黑名单 fail-open、
  token_version 默认 0、限流放行），用例内不再发起真实 Redis 连接。
- 环境列表缓存禁用（CACHE_ENABLED=false），避免全局缓存单例跨用例串扰。
"""

import uuid

import pytest
from app.extensions import db

ENV_BASE = "/api/v1/environments"
PROJ_ENV_BASE = "/api/v1/projects"


@pytest.fixture(autouse=True)
def _offline_redis(monkeypatch):
    """本机 Redis 挂死：token 黑名单/限流服务注入无网络实现（返回 None 即降级路径）"""
    import app.services.token_blacklist as tb
    import app.services.rate_limit_service as rls

    monkeypatch.setattr(tb, "_get_redis", lambda: None)
    monkeypatch.setattr(rls, "_get_redis", lambda: None)
    # 禁用环境列表缓存（get_cache_service 优先读 CACHE_ENABLED），避免跨用例串扰
    monkeypatch.setenv("CACHE_ENABLED", "false")


def _uname() -> str:
    return f"env_{uuid.uuid4().hex[:10]}"


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

def _make_env(app, project_id: int, name: str = "Development", **extra) -> int:
    from app.models.environment import Environment
    env = Environment(
        project_id=project_id,
        name=name,
        base_url=extra.pop("base_url", "https://env.example.com"),
        **extra,
    )
    db.session.add(env)
    db.session.commit()
    return env.id

# ---------------------------------------------------------------------------
# 1. 未登录 401
# ---------------------------------------------------------------------------

class TestEnvironmentsAuth:
    @pytest.mark.parametrize(
        "method,url",
        [
            ("get", ENV_BASE),
            ("post", ENV_BASE),
            ("get", f"{ENV_BASE}/1"),
            ("put", f"{ENV_BASE}/1"),
            ("delete", f"{ENV_BASE}/1"),
            ("post", f"{ENV_BASE}/1/default"),
            ("get", f"{ENV_BASE}/1/export"),
            ("get", f"{ENV_BASE}/1/export-docker"),
            ("get", f"{PROJ_ENV_BASE}/1/environments"),
            ("post", f"{PROJ_ENV_BASE}/1/environments"),
            ("post", f"{PROJ_ENV_BASE}/1/environments/import"),
        ],
    )
    def test_unauthenticated_401(self, v2_client, method, url):
        """未登录访问受保护端点 → 401，响应结构与 error_response 一致"""
        # httpx 版 TestClient 的 get/delete 不接受 json= 参数
        kwargs = {"json": {}} if method in ("post", "put") else {}
        resp = getattr(v2_client, method)(url, **kwargs)
        assert resp.status_code == 401, f"{method.upper()} {url}: {resp.text}"
        assert resp.json()["code"] == 401


# ---------------------------------------------------------------------------
# 2. 全链路 CRUD + 默认环境 + 导出/导入
# ---------------------------------------------------------------------------

class TestEnvironmentsCRUD:
    def test_full_crud_cycle(self, v2_client, app, make_user, auth_headers):
        user_id = make_user(_uname())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)

        # 创建
        resp = v2_client.post(
            ENV_BASE,
            headers=headers,
            json={
                "name": "Development",
                "project_id": project_id,
                "base_url": "http://localhost:8080",
                "variables": {"api_key": "test-key-123"},
                "headers": {"X-Environment": "dev"},
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == 200
        assert body["message"] == "创建成功"
        env = body["data"]
        assert env["name"] == "Development"
        assert env["project_id"] == project_id
        assert env["base_url"] == "http://localhost:8080"
        assert env["variables"] == {"api_key": "test-key-123"}
        assert env["headers"] == {"X-Environment": "dev"}
        assert env["is_default"] is False
        assert env["is_active"] is False  # to_dict 派生字段
        assert env["created_at"] is not None
        env_id = env["id"]

        # 同名环境 → 400
        resp = v2_client.post(
            ENV_BASE,
            headers=headers,
            json={"name": "Development", "project_id": project_id, "base_url": "http://x"},
        )
        assert resp.status_code == 400
        assert resp.json()["message"] == "环境名称已存在"

        # 全局列表（缓存已禁用，直查 DB）
        resp = v2_client.get(ENV_BASE, headers=headers)
        assert resp.status_code == 200, resp.text
        envs = resp.json()["data"]
        assert any(e["id"] == env_id for e in envs)

        # 全局列表按 project_id 过滤
        resp = v2_client.get(f"{ENV_BASE}?project_id={project_id}", headers=headers)
        assert resp.status_code == 200
        assert all(e["project_id"] == project_id for e in resp.json()["data"])

        # 项目级列表
        resp = v2_client.get(f"{PROJ_ENV_BASE}/{project_id}/environments", headers=headers)
        assert resp.status_code == 200, resp.text
        assert [e["id"] for e in resp.json()["data"]] == [env_id]

        # 详情
        resp = v2_client.get(f"{ENV_BASE}/{env_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["name"] == "Development"

        # 更新
        resp = v2_client.put(
            f"{ENV_BASE}/{env_id}",
            headers=headers,
            json={
                "name": "Staging",
                "base_url": "https://staging.example.com",
                "variables": {"api_key": "staging-key"},
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "更新成功"
        updated = resp.json()["data"]
        assert updated["name"] == "Staging"
        assert updated["base_url"] == "https://staging.example.com"
        assert updated["variables"] == {"api_key": "staging-key"}

        # 更新为已存在的同名环境 → 400
        _make_env(app, project_id, name="Prod")
        resp = v2_client.put(
            f"{ENV_BASE}/{env_id}", headers=headers, json={"name": "Prod"}
        )
        assert resp.status_code == 400
        assert resp.json()["message"] == "环境名称已存在"

        # 设为默认
        resp = v2_client.post(f"{ENV_BASE}/{env_id}/default", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "设置成功"
        resp = v2_client.get(f"{ENV_BASE}/{env_id}", headers=headers)
        assert resp.json()["data"]["is_default"] is True

        # 导出 JSON
        resp = v2_client.get(f"{ENV_BASE}/{env_id}/export", headers=headers)
        assert resp.status_code == 200, resp.text
        export = resp.json()["data"]
        assert export["version"] == "1.0"
        assert "export_time" in export
        assert export["environment"]["name"] == "Staging"
        assert export["environment"]["base_url"] == "https://staging.example.com"
        assert export["environment"]["variables"] == {"api_key": "staging-key"}
        assert "description" in export["environment"]

        # 导出 Docker
        resp = v2_client.get(f"{ENV_BASE}/{env_id}/export-docker", headers=headers)
        assert resp.status_code == 200, resp.text
        docker = resp.json()["data"]
        assert docker["environment_name"] == "Staging"
        assert "BASE_URL=https://staging.example.com" in docker["env_file"]
        assert "api_key=staging-key" in docker["env_file"]
        assert "- api_key=${api_key}" in docker["compose_snippet"]

        # 删除
        resp = v2_client.delete(f"{ENV_BASE}/{env_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "删除成功"
        # 删除后详情 404
        assert v2_client.get(f"{ENV_BASE}/{env_id}", headers=headers).status_code == 404

    def test_create_without_project_uses_default_project(self, v2_client, app, make_user, auth_headers):
        user_id = make_user(_uname())
        headers = auth_headers(user_id)

        resp = v2_client.post(
            ENV_BASE,
            headers=headers,
            json={"name": "NoProj", "base_url": "http://a.b"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        # 自动创建的默认项目
        from app.extensions import db
        from app.models.project import Project

    def test_project_scoped_create_validations(self, v2_client, app, make_user, auth_headers):
        user_id = make_user(_uname())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)

        # 空 body → 400
        resp = v2_client.post(f"{PROJ_ENV_BASE}/{project_id}/environments", headers=headers, json={})
        assert resp.status_code == 400
        assert resp.json()["message"] == "请求体不能为空"

        # 缺 base_url → 400（等价 v1 validate_json）
        resp = v2_client.post(
            f"{PROJ_ENV_BASE}/{project_id}/environments", headers=headers, json={"name": "X"}
        )
        assert resp.status_code == 400
        assert resp.json()["message"] == "缺少必需字段: base_url"

        # 创建成功
        resp = v2_client.post(
            f"{PROJ_ENV_BASE}/{project_id}/environments",
            headers=headers,
            json={"name": "ViaProject", "base_url": "http://p1"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["project_id"] == project_id

    def test_variables_validation(self, v2_client, app, make_user, auth_headers):
        user_id = make_user(_uname())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)

        # variables 为数组 → 400
        resp = v2_client.post(
            ENV_BASE,
            headers=headers,
            json={"name": "V1", "project_id": project_id, "base_url": "http://x", "variables": ["a"]},
        )
        assert resp.status_code == 400
        assert resp.json()["message"] == 'variables 必须是对象类型（如 {"key": "value"}），不能是数组'

        # variables 超过 100 个 → 400
        resp = v2_client.post(
            ENV_BASE,
            headers=headers,
            json={
                "name": "V2",
                "project_id": project_id,
                "base_url": "http://x",
                "variables": {f"k{i}": str(i) for i in range(101)},
            },
        )
        assert resp.status_code == 400
        assert resp.json()["message"] == "环境变量不能超过100个，当前有101个"

        # 更新时 variables 非对象 → 400（v1 update 语义）
        env_id = _make_env(app, project_id)
        resp = v2_client.put(
            f"{ENV_BASE}/{env_id}", headers=headers, json={"variables": "not-an-object"}
        )
        assert resp.status_code == 400
        assert resp.json()["message"] == "variables 格式不正确，必须是有效的 JSON 对象"

    def test_default_env_switching(self, v2_client, app, make_user, auth_headers):
        user_id = make_user(_uname())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)

        env_a = _make_env(app, project_id, name="A")
        env_b = _make_env(app, project_id, name="B")

        # A 设默认
        assert v2_client.post(f"{ENV_BASE}/{env_a}/default", headers=headers).status_code == 200
        # B 以 is_default=true 创建 → A 被取消默认
        resp = v2_client.post(
            f"{PROJ_ENV_BASE}/{project_id}/environments",
            headers=headers,
            json={"name": "C", "base_url": "http://c", "is_default": True},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["is_default"] is True

    def test_import_env_file_and_json(self, v2_client, app, make_user, auth_headers):
        user_id = make_user(_uname())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)

        # .env 格式导入
        env_content = "# 注释行\nAPI_KEY=abc123\nDB_HOST=\"localhost\"\n\n"
        resp = v2_client.post(
            f"{PROJ_ENV_BASE}/{project_id}/environments/import",
            headers=headers,
            json={"data": env_content},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["message"] == "导入成功（merge模式）"
        imported = body["data"]
        assert imported["name"].startswith("导入的环境 ")
        assert imported["variables"] == {"API_KEY": "abc123", "DB_HOST": "localhost"}
        assert imported["project_id"] == project_id

        # JSON 格式导入（新建）
        resp = v2_client.post(
            f"{PROJ_ENV_BASE}/{project_id}/environments/import",
            headers=headers,
            json={"data": {"name": "Imported", "base_url": "http://imp", "variables": {"A": "1"}}},
        )
        assert resp.status_code == 200, resp.text
        imported_id = resp.json()["data"]["id"]
        assert resp.json()["data"]["variables"] == {"A": "1"}

        # merge 模式合并变量
        resp = v2_client.post(
            f"{PROJ_ENV_BASE}/{project_id}/environments/import",
            headers=headers,
            json={"data": {"name": "Imported", "variables": {"B": "2"}}, "mode": "merge"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["variables"] == {"A": "1", "B": "2"}

        # override 模式覆盖变量（base_url 不变）
        resp = v2_client.post(
            f"{PROJ_ENV_BASE}/{project_id}/environments/import",
            headers=headers,
            json={"data": {"name": "Imported", "variables": {"C": "3"}}, "mode": "override"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["variables"] == {"C": "3"}
        assert resp.json()["data"]["base_url"] == "http://imp"
        assert resp.json()["data"]["id"] == imported_id

        # 缺少导入数据 → 400
        resp = v2_client.post(
            f"{PROJ_ENV_BASE}/{project_id}/environments/import", headers=headers, json={}
        )
        assert resp.status_code == 400
        assert resp.json()["message"] == "缺少导入数据"

        # 不支持的数据格式 → 400
        resp = v2_client.post(
            f"{PROJ_ENV_BASE}/{project_id}/environments/import",
            headers=headers,
            json={"data": [1, 2, 3]},
        )
        assert resp.status_code == 400
        assert resp.json()["message"] == "不支持的数据格式"

    def test_missing_environment_404(self, v2_client, make_user, auth_headers):
        user_id = make_user(_uname())
        headers = auth_headers(user_id)

        assert v2_client.get(f"{ENV_BASE}/999999", headers=headers).status_code == 404
        assert v2_client.put(
            f"{ENV_BASE}/999999", headers=headers, json={"name": "X"}
        ).status_code == 404
        assert v2_client.delete(f"{ENV_BASE}/999999", headers=headers).status_code == 404


# ---------------------------------------------------------------------------
# 3. 访问/修改他人资源 → 404（IDOR 修复）
# ---------------------------------------------------------------------------

class TestEnvironmentsIDOR:
    def test_other_user_environment_404(self, v2_client, app, make_user, auth_headers):
        owner_id = make_user(_uname())
        attacker_id = make_user(_uname())
        owner_headers = auth_headers(owner_id)
        attacker_headers = auth_headers(attacker_id)

        project_id = _make_project(app, owner_id)
        env_id = _make_env(app, project_id, name="OwnerEnv")

        # 他人环境：读/改/删/设默认/导出 全部 404
        assert v2_client.get(f"{ENV_BASE}/{env_id}", headers=attacker_headers).status_code == 404
        assert (
            v2_client.put(
                f"{ENV_BASE}/{env_id}", headers=attacker_headers, json={"name": "Hacked"}
            ).status_code
            == 404
        )
        assert v2_client.delete(f"{ENV_BASE}/{env_id}", headers=attacker_headers).status_code == 404
        assert (
            v2_client.post(f"{ENV_BASE}/{env_id}/default", headers=attacker_headers).status_code
            == 404
        )
        assert (
            v2_client.get(f"{ENV_BASE}/{env_id}/export", headers=attacker_headers).status_code
            == 404
        )
        assert (
            v2_client.get(f"{ENV_BASE}/{env_id}/export-docker", headers=attacker_headers).status_code
            == 404
        )

        # 他人项目级路由 → 404 项目不存在
        resp = v2_client.get(f"{PROJ_ENV_BASE}/{project_id}/environments", headers=attacker_headers)
        assert resp.status_code == 404
        assert resp.json()["message"] == "项目不存在"
        assert (
            v2_client.post(
                f"{PROJ_ENV_BASE}/{project_id}/environments",
                headers=attacker_headers,
                json={"name": "X", "base_url": "http://x"},
            ).status_code
            == 404
        )
        assert (
            v2_client.post(
                f"{PROJ_ENV_BASE}/{project_id}/environments/import",
                headers=attacker_headers,
                json={"data": "A=1"},
            ).status_code
            == 404
        )

        # 全局创建挂他人 project_id → 404
        resp = v2_client.post(
            ENV_BASE,
            headers=attacker_headers,
            json={"name": "Steal", "project_id": project_id, "base_url": "http://x"},
        )
        assert resp.status_code == 404

        # 全局列表看不到他人环境（属主隔离）
        resp = v2_client.get(ENV_BASE, headers=attacker_headers)
        assert resp.status_code == 200
        assert all(e["id"] != env_id for e in resp.json()["data"])

        # owner 本人一切正常
        assert v2_client.get(f"{ENV_BASE}/{env_id}", headers=owner_headers).status_code == 200
        assert (
            v2_client.get(f"{PROJ_ENV_BASE}/{project_id}/environments", headers=owner_headers).status_code
            == 200
        )

    def test_cannot_modify_after_owner_deleted_env(self, v2_client, app, make_user, auth_headers):
        user_id = make_user(_uname())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)
        env_id = _make_env(app, project_id)

        # 删除后他人也无法再操作（资源已不存在）
        assert v2_client.delete(f"{ENV_BASE}/{env_id}", headers=headers).status_code == 200
        assert v2_client.get(f"{ENV_BASE}/{env_id}", headers=headers).status_code == 404
