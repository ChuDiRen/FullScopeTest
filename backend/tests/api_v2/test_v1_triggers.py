"""
v1 triggers 平迁路由测试（/api/v1/webhooks、/api/v1/trigger-rules、
/api/v1/triggers/{token}、/api/v1/schedules，源：app/api/triggers.py）

覆盖：
1. 受保护端点未登录一律 401；公开触发端点（v1 无鉴权设计）未登录可访问
2. Webhook/触发规则/定时任务 创建 → 列表 → 更新/删除 全链路，信封与 v1 一致
3. IDOR：规则 created_by 过滤（他人规则 404）、create 校验 project 归属（403）、
   他人项目列表/删除 → 404/403
4. Webhook 触发：无效 token 404、有效 token 派发 Celery 任务（任务对象全 mock，零外呼）
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
    return f"trg_{uuid.uuid4().hex[:10]}"


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

def _create_webhook(v2_client, headers, project_id, **extra) -> dict:
    payload = {
        "project_id": project_id,
        "name": extra.pop("name", "CI Hook"),
        "target_type": extra.pop("target_type", "api_collection"),
        "target_id": extra.pop("target_id", 1),
        **extra,
    }
    resp = v2_client.post(f"{BASE}/webhooks", json=payload, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _create_rule(v2_client, headers, project_id, **extra) -> dict:
    payload = {
        "project_id": project_id,
        "name": extra.pop("name", "PR 主干回归"),
        "trigger_event": extra.pop("trigger_event", "pull_request"),
        "target_type": extra.pop("target_type", "api_collection"),
        **extra,
    }
    resp = v2_client.post(f"{BASE}/trigger-rules", json=payload, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _create_schedule(v2_client, headers, project_id, **extra) -> dict:
    payload = {
        "project_id": project_id,
        "name": extra.pop("name", "每日回归"),
        "cron_expression": extra.pop("cron_expression", "0 9 * * *"),
        "target_type": extra.pop("target_type", "api_collection"),
        "target_id": extra.pop("target_id", 1),
        **extra,
    }
    resp = v2_client.post(f"{BASE}/schedules", json=payload, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


# ---------------------------------------------------------------------------
# 1. 未登录 401 + 公开端点
# ---------------------------------------------------------------------------

class TestTriggersAuth:
    @pytest.mark.parametrize(
        "method,url",
        [
            ("get", f"{BASE}/webhooks"),
            ("post", f"{BASE}/webhooks"),
            ("delete", f"{BASE}/webhooks/1"),
            ("get", f"{BASE}/trigger-rules"),
            ("post", f"{BASE}/trigger-rules"),
            ("put", f"{BASE}/trigger-rules/1"),
            ("delete", f"{BASE}/trigger-rules/1"),
            ("get", f"{BASE}/schedules"),
            ("post", f"{BASE}/schedules"),
            ("put", f"{BASE}/schedules/1"),
            ("delete", f"{BASE}/schedules/1"),
        ],
    )
    def test_protected_endpoints_require_auth(self, v2_client, method, url):
        """未登录访问受保护端点 → 401"""
        resp = getattr(v2_client, method)(url)
        assert resp.status_code == 401, resp.text

    def test_trigger_endpoint_is_public(self, v2_client):
        """触发端点是 v1 公开设计：无鉴权访问（无效 token → 404 而非 401）"""
        resp = v2_client.post(f"{BASE}/triggers/not-a-real-token-{uuid.uuid4().hex}")
        assert resp.status_code == 404, resp.text
        body = resp.json()
        assert body["code"] == 404
        assert body["message"] == "无效的 Token"


# ---------------------------------------------------------------------------
# 2. Webhook CRUD
# ---------------------------------------------------------------------------

class TestWebhookCRUD:
    def test_create_list_delete_flow(self, v2_client, app, make_user, auth_headers):
        """创建 → 列表 → 删除"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)

        webhook = _create_webhook(v2_client, headers, project_id, name="Deploy Hook")
        assert webhook["name"] == "Deploy Hook"
        assert webhook["token"]  # 返回 token 字段（触发凭据）
        assert webhook["project_id"] == project_id

        resp = v2_client.get(f"{BASE}/webhooks?project_id={project_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        items = resp.json()["data"]
        assert any(w["id"] == webhook["id"] for w in items)

        resp = v2_client.delete(f"{BASE}/webhooks/{webhook['id']}", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "Webhook 删除成功"

        resp = v2_client.get(f"{BASE}/webhooks?project_id={project_id}", headers=headers)
        assert all(w["id"] != webhook["id"] for w in resp.json()["data"])

    def test_get_webhooks_missing_project_400(self, v2_client, make_user, auth_headers):
        """缺 project_id → 400（v1 行为）"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        resp = v2_client.get(f"{BASE}/webhooks", headers=headers)
        assert resp.status_code == 400, resp.text
        assert resp.json()["message"] == "缺少 project_id 参数"

    def test_create_webhook_incomplete_400(self, v2_client, make_user, auth_headers):
        """缺必需字段 → 400 参数不完整（v1 行为）"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        resp = v2_client.post(f"{BASE}/webhooks", json={"name": "half"}, headers=headers)
        assert resp.status_code == 400, resp.text
        assert resp.json()["message"] == "参数不完整"

    def test_webhook_idor(self, v2_client, app, make_user, auth_headers):
        """他人项目：列表 404（防枚举）、注入 403、删他人 Webhook 403"""
        owner = make_user(_username())
        stranger = make_user(_username())
        owner_headers = auth_headers(owner)
        stranger_headers = auth_headers(stranger)
        project_id = _make_project(app, owner)

        webhook = _create_webhook(v2_client, owner_headers, project_id)

        # 列表他人项目 → 404（IDOR 修复：v1 可枚举他人 Webhook）
        resp = v2_client.get(f"{BASE}/webhooks?project_id={project_id}", headers=stranger_headers)
        assert resp.status_code == 404, resp.text
        assert resp.json()["message"] == "项目不存在"

        # 向他人项目注入 Webhook → 403（IDOR 修复：v1 可向他人项目注入）
        resp = v2_client.post(
            f"{BASE}/webhooks",
            json={
                "project_id": project_id,
                "name": "Injected",
                "target_type": "api_collection",
                "target_id": 1,
            },
            headers=stranger_headers,
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["message"] == "无权在该项目下创建 Webhook"

        # 删除他人项目的 Webhook → 403（v1 原有校验）
        resp = v2_client.delete(f"{BASE}/webhooks/{webhook['id']}", headers=stranger_headers)
        assert resp.status_code == 403, resp.text
        assert resp.json()["message"] == "无权删除该 Webhook"


# ---------------------------------------------------------------------------
# 3. Webhook 触发（公开端点，Celery 任务全 mock）
# ---------------------------------------------------------------------------

class _FakeAsyncResult:
    id = "fake-celery-task-id-123"


class _FakeCeleryTask:
    def __init__(self):
        self.calls = []

    def delay(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return _FakeAsyncResult()


class TestWebhookTrigger:
    @pytest.fixture()
    def _mock_celery_tasks(self, monkeypatch):
        """把 app.tasks 的三个派发任务替换为 mock（v1 import 名不存在/不可用时同样兜底）"""
        import app.tasks as tasks_pkg

        api_task, web_task, perf_task = _FakeCeleryTask(), _FakeCeleryTask(), _FakeCeleryTask()
        monkeypatch.setattr(tasks_pkg, "run_api_collection_task", api_task, raising=False)
        monkeypatch.setattr(tasks_pkg, "run_web_collection_task", web_task, raising=False)
        monkeypatch.setattr(tasks_pkg, "run_perf_scenario_task", perf_task, raising=False)
        return {"api": api_task, "web": web_task, "perf": perf_task}

    def test_trigger_with_valid_token(
        self, v2_client, app, make_user, auth_headers, _mock_celery_tasks
    ):
        """有效 token + POST → 派发 api_collection 任务并返回 task_id（无需登录）"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)
        webhook = _create_webhook(v2_client, headers, project_id, target_type="api_collection")

        resp = v2_client.post(f"{BASE}/triggers/{webhook['token']}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["message"] == "任务已触发"
        assert body["data"]["task_id"] == "fake-celery-task-id-123"
        assert _mock_celery_tasks["api"].calls[0][0] == (webhook["target_id"], None)

    def test_trigger_with_get_method(
        self, v2_client, app, make_user, auth_headers, _mock_celery_tasks
    ):
        """GET 方式触发（v1 路由允许 GET/POST）"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)
        webhook = _create_webhook(v2_client, headers, project_id, target_type="web_collection")

        resp = v2_client.get(f"{BASE}/triggers/{webhook['token']}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["task_id"] == "fake-celery-task-id-123"
        assert _mock_celery_tasks["web"].calls

    def test_trigger_unsupported_target_type(
        self, v2_client, app, make_user, auth_headers, _mock_celery_tasks
    ):
        """不支持的 target_type → 400（v1 行为）"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)
        webhook = _create_webhook(v2_client, headers, project_id, target_type="bogus_type")

        resp = v2_client.post(f"{BASE}/triggers/{webhook['token']}")
        assert resp.status_code == 400, resp.text
        assert resp.json()["message"] == "不支持的 target_type"


# ---------------------------------------------------------------------------
# 4. 触发规则 CRUD
# ---------------------------------------------------------------------------

class TestTriggerRuleCRUD:
    def test_create_list_update_delete_flow(self, v2_client, app, make_user, auth_headers):
        """创建 → 列表 → 更新 → 删除"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)

        rule = _create_rule(v2_client, headers, project_id, name="PR 主干回归")
        assert rule["created_by"] == user_id
        assert rule["trigger_event"] == "pull_request"

        # 列表
        resp = v2_client.get(f"{BASE}/trigger-rules?project_id={project_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        items = resp.json()["data"]
        assert any(r["id"] == rule["id"] and r["name"] == "PR 主干回归" for r in items)

        # 更新
        resp = v2_client.put(
            f"{BASE}/trigger-rules/{rule['id']}",
            json={"description": "主干 PR 触发回归", "is_active": False},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["message"] == "触发规则更新成功"
        assert body["data"]["description"] == "主干 PR 触发回归"
        assert body["data"]["is_active"] is False

        # 删除
        resp = v2_client.delete(f"{BASE}/trigger-rules/{rule['id']}", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "触发规则删除成功"

        resp = v2_client.get(f"{BASE}/trigger-rules?project_id={project_id}", headers=headers)
        assert all(r["id"] != rule["id"] for r in resp.json()["data"])

    def test_get_rules_missing_project_400(self, v2_client, make_user, auth_headers):
        """缺 project_id → 400（v1 行为）"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        resp = v2_client.get(f"{BASE}/trigger-rules", headers=headers)
        assert resp.status_code == 400, resp.text

    def test_create_rule_incomplete_400(self, v2_client, make_user, auth_headers):
        """缺必需字段 → 400 参数不完整（v1 行为）"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        resp = v2_client.post(f"{BASE}/trigger-rules", json={"name": "half"}, headers=headers)
        assert resp.status_code == 400, resp.text
        assert resp.json()["message"] == "参数不完整"

    def test_trigger_rule_idor(self, v2_client, app, make_user, auth_headers):
        """他人规则：created_by 过滤（列表不可见、更新/删除 404）；他人项目创建 403"""
        owner = make_user(_username())
        stranger = make_user(_username())
        owner_headers = auth_headers(owner)
        stranger_headers = auth_headers(stranger)
        project_id = _make_project(app, owner)
        stranger_project_id = _make_project(app, stranger)

        rule = _create_rule(v2_client, owner_headers, project_id)

        # 列表：created_by 过滤，他人的项目里看不到他人规则（IDOR 修复）
        resp = v2_client.get(f"{BASE}/trigger-rules?project_id={project_id}", headers=stranger_headers)
        assert resp.status_code == 200, resp.text
        assert all(r["id"] != rule["id"] for r in resp.json()["data"])

        # 更新/删除他人规则 → 404（created_by 属主校验，源文件已有）
        resp = v2_client.put(
            f"{BASE}/trigger-rules/{rule['id']}", json={"name": "hijack"}, headers=stranger_headers
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["message"] == "规则不存在"
        resp = v2_client.delete(f"{BASE}/trigger-rules/{rule['id']}", headers=stranger_headers)
        assert resp.status_code == 404, resp.text

        # 向他人项目注入规则 → 403（源文件已有校验）
        resp = v2_client.post(
            f"{BASE}/trigger-rules",
            json={
                "project_id": project_id,
                "name": "Injected",
                "trigger_event": "push",
                "target_type": "api_collection",
            },
            headers=stranger_headers,
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["message"] == "无权在该项目下创建触发规则"

        # 自己项目创建正常
        resp = v2_client.post(
            f"{BASE}/trigger-rules",
            json={
                "project_id": stranger_project_id,
                "name": "自己的规则",
                "trigger_event": "push",
                "target_type": "api_collection",
            },
            headers=stranger_headers,
        )
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# 5. 定时任务 CRUD
# ---------------------------------------------------------------------------

class TestScheduleCRUD:
    def test_create_list_update_delete_flow(self, v2_client, app, make_user, auth_headers):
        """创建 → 列表 → 更新 → 删除（APScheduler 调用与 v1 一致，仅内存调度器）"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)

        task = _create_schedule(v2_client, headers, project_id, name="每日回归")
        assert task["cron_expression"] == "0 9 * * *"
        assert task["is_active"] is True

        resp = v2_client.get(f"{BASE}/schedules?project_id={project_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        assert any(t["id"] == task["id"] for t in resp.json()["data"])

        # 更新（is_active=False → remove_job 分支）
        resp = v2_client.put(
            f"{BASE}/schedules/{task['id']}",
            json={"name": "改名回归", "is_active": False},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["message"] == "定时任务更新成功"
        assert body["data"]["name"] == "改名回归"
        assert body["data"]["is_active"] is False

        # 删除
        resp = v2_client.delete(f"{BASE}/schedules/{task['id']}", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "定时任务删除成功"

    def test_create_schedule_incomplete_400(self, v2_client, make_user, auth_headers):
        """缺必需字段 → 400 参数不完整（v1 行为）"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        resp = v2_client.post(f"{BASE}/schedules", json={"name": "half"}, headers=headers)
        assert resp.status_code == 400, resp.text
        assert resp.json()["message"] == "参数不完整"

    def test_schedule_idor(self, v2_client, app, make_user, auth_headers):
        """他人项目：列表 404（防枚举）、注入 403、更新/删除他人任务 403"""
        owner = make_user(_username())
        stranger = make_user(_username())
        owner_headers = auth_headers(owner)
        stranger_headers = auth_headers(stranger)
        project_id = _make_project(app, owner)

        task = _create_schedule(v2_client, owner_headers, project_id)

        # 列表他人项目 → 404（IDOR 修复：v1 可枚举他人定时任务）
        resp = v2_client.get(f"{BASE}/schedules?project_id={project_id}", headers=stranger_headers)
        assert resp.status_code == 404, resp.text
        assert resp.json()["message"] == "项目不存在"

        # 向他人项目注入定时任务 → 403（IDOR 修复）
        resp = v2_client.post(
            f"{BASE}/schedules",
            json={
                "project_id": project_id,
                "name": "Injected",
                "cron_expression": "0 9 * * *",
                "target_type": "api_collection",
                "target_id": 1,
            },
            headers=stranger_headers,
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["message"] == "无权在该项目下创建定时任务"

        # 更新/删除他人定时任务 → 403（v1 原有校验）
        resp = v2_client.put(
            f"{BASE}/schedules/{task['id']}", json={"name": "hijack"}, headers=stranger_headers
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["message"] == "无权修改该定时任务"
        resp = v2_client.delete(f"{BASE}/schedules/{task['id']}", headers=stranger_headers)
        assert resp.status_code == 403, resp.text
        assert resp.json()["message"] == "无权删除该定时任务"
