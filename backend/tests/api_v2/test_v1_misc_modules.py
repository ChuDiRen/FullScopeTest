"""
v1 杂项模块平迁路由测试（FastAPI 实现，11 个模块：visual / notifications /
comments / alert_rules / branding / webhook_debugger / swagger_gen /
dashboard_config / global_search / gitlab_webhooks）

覆盖：
1. 未登录 401（各模块抽端点参数化；gitlab webhook 为公开端点，
   以"配置密钥后缺签名 → 401"等价覆盖其鉴权边界）
2. visual 基准/差异的属主校验（IDOR：他人资源 404，本人 200）
3. comments 创建 → 列表 → 详情（含他人评论 404）
4. notifications 列表 + 配置更新/测试发送（v1 无"已读标记"端点，
   状态更新以 is_active 覆盖；test 端点 stub 掉真实 HTTP 发送）
5. 他人资源 404：notifications 配置、alert_rules 规则/日志、
   dashboard 布局隔离、comments 详情
6. 公开端点：branding GET、
   gitlab webhook 事件分发

本文件所有测试依赖本机 Redis 不可用：autouse fixture 将
app.services.token_blacklist._get_redis 与 app.services.rate_limit_service._get_redis
monkeypatch 为 lambda: None（黑名单/版本校验按"Redis 不可用降级"路径放行）。
"""
from sqlalchemy import select
from app.core.runtime import get_config

import hashlib
import hmac
import uuid

import pytest
from app.extensions import db


# ---------------------------------------------------------------------------
# Redis 桩（本机 Redis 挂死）
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _stub_redis(monkeypatch):
    """本机 Redis 挂死：所有 Redis 访问路径直接返回 None。

    - token_blacklist._get_redis → None：is_token_blacklisted → False（放行）、
      is_token_version_valid → True（版本 0 >= 0），鉴权链不触网
    - rate_limit_service._get_redis → None：sliding_window_rate_limit 走
      "Redis 不可用放行"分支，限流中间件不触网
    deps.py 为调用期惰性 import，patch 模块属性即可生效。"""
    import app.services.token_blacklist as tb
    import app.services.rate_limit_service as rls

    monkeypatch.setattr(tb, "_get_redis", lambda: None)
    monkeypatch.setattr(rls, "_get_redis", lambda: None)


# ---------------------------------------------------------------------------
# 工具与数据工厂
# ---------------------------------------------------------------------------

def _uname() -> str:
    return f"misc_{uuid.uuid4().hex[:10]}"


def _make_project(app, owner_id: int) -> int:
    from app.models.project import Project
    project = Project(
        name=f"proj_{owner_id}_{uuid.uuid4().hex[:8]}",
        owner_id=owner_id,
    )
    db.session.add(project)
    db.session.commit()
    return project.id

def _make_run(app, project_id: int) -> int:
    from app.models.test_run import TestRun
    run = TestRun(
        project_id=project_id,
        test_type="web",
        status="success",
        total_cases=3,
        passed=3,
    )
    db.session.add(run)
    db.session.commit()
    return run.id

def _make_baseline(app, project_id: int, test_case_id: int, **extra) -> int:
    from app.models.visual_baseline import VisualBaseline
    baseline = VisualBaseline(
        project_id=project_id,
        test_case_id=test_case_id,
        test_type=extra.pop("test_type", "web"),
        step_index=extra.pop("step_index", 0),
        baseline_image_path=extra.pop("baseline_image_path", "/img/baseline.png"),
        **extra,
    )
    db.session.add(baseline)
    db.session.commit()
    return baseline.id

def _make_diff(app, run_id: int, test_case_id: int, **extra) -> int:
    from app.models.visual_diff import VisualDiff
    diff = VisualDiff(
        test_run_id=run_id,
        test_case_id=test_case_id,
        test_type=extra.pop("test_type", "web"),
        step_index=extra.pop("step_index", 0),
        current_image_path=extra.pop("current_image_path", "/img/current.png"),
        diff_percentage=extra.pop("diff_percentage", 1.5),
        status=extra.pop("status", "visual_pass"),
        **extra,
    )
    db.session.add(diff)
    db.session.commit()
    return diff.id

def _make_scenario(app, user_id: int) -> int:
    from app.models.perf_test_scenario import PerfTestScenario
    scenario = PerfTestScenario(
        name=f"scenario_{user_id}_{uuid.uuid4().hex[:6]}",
        target_url="https://load.example.com",
        user_id=user_id,
    )
    db.session.add(scenario)
    db.session.commit()
    return scenario.id

def _make_alert_rule(app, name: str, scenario_id=None) -> int:
    from app.models.perf_test_alert import PerformanceAlertRule
    rule = PerformanceAlertRule(
        name=name,
        scenario_id=scenario_id,
        error_rate_threshold=50.0,
    )
    db.session.add(rule)
    db.session.commit()
    return rule.id

def _make_alert_log(app, rule_id: int, result_id: int, alert_type: str = "absolute") -> int:
    from app.models.perf_test_alert import PerformanceAlertLog
    log = PerformanceAlertLog(
        rule_id=rule_id,
        result_id=result_id,
        alert_type=alert_type,
        metric_name="error_rate",
        threshold_value=50.0,
        actual_value=80.0,
        message="test alert",
    )
    db.session.add(log)
    db.session.commit()
    return log.id

def _make_perf_result(app, scenario_id: int) -> int:
    from app.models.perf_test_result import PerformanceTestResult
    result = PerformanceTestResult(
        scenario_id=scenario_id,
        user_count=10,
        spawn_rate=1,
        duration=60,
        status="completed",
    )
    db.session.add(result)
    db.session.commit()
    return result.id

def _make_org_membership(app, user_id: int, is_active: bool = True):
    from app.models.organization import Organization, OrganizationMember
    from sqlalchemy import select
    membership = db.session.scalar(
        select(OrganizationMember).filter_by(user_id=user_id)
    )
    if membership is None:
        org = db.session.scalar(select(Organization).filter_by(owner_id=user_id))
        if org is None:
            org = Organization(
                name=f"org_{user_id}",
                slug=f"org-{user_id}-{uuid.uuid4().hex[:8]}",
                owner_id=user_id,
            )
            db.session.add(org)
            db.session.flush()
        membership = OrganizationMember(
            organization_id=org.id, user_id=user_id, role="admin"
        )
        db.session.add(membership)
        db.session.flush()
    membership.is_active = is_active
    db.session.commit()
    return membership.id

# ---------------------------------------------------------------------------
# 1. 未登录 401（各模块参数化）
# ---------------------------------------------------------------------------

class TestUnauthenticated401:
    @pytest.mark.parametrize(
        "method,url,json_body",
        [
            # visual
            ("get", "/api/v1/visual/baselines/1", None),
            # notifications
            ("get", "/api/v1/notifications/configs", None),
            # comments（GET 详情 / POST 创建）
            ("get", "/api/v1/comments/1", None),
            ("post", "/api/v1/comments", {"resource_type": "test_case", "resource_id": 1, "content": "x"}),
            # alert_rules
            ("get", "/api/v1/perf-test/alert-rules", None),
            # branding（GET 公开，PUT 需鉴权）
            ("put", "/api/v1/branding/config", {"platform_name": "X"}),
            # webhook_debugger
            # swagger_gen
            ("post", "/api/v1/ai/generate-cases-from-swagger", {"swagger_content": "openapi: 3.0.0"}),
            # dashboard_config
            ("get", "/api/v1/dashboard/widgets", None),
            # global_search
            ("post", "/api/v1/ai/global-search", {"query": "登录"}),
        ],
    )
    def test_unauthenticated_401(self, v2_client, method, url, json_body):
        """未登录访问受保护端点 → 401，响应结构与 v1 error_response 一致"""
        kwargs = {}
        if json_body is not None:
            kwargs["json"] = json_body
        resp = getattr(v2_client, method)(url, **kwargs)
        assert resp.status_code == 401, f"{method.upper()} {url}: {resp.text}"
        body = resp.json()
        assert body["code"] == 401


# ---------------------------------------------------------------------------
# 2. visual 属主校验（基准/差异/历史）
# ---------------------------------------------------------------------------

class TestVisualOwnership:
    def test_baseline_owner_can_read_and_approve(self, v2_client, app, make_user, auth_headers):
        owner = make_user(_uname())
        headers = auth_headers(owner)
        project_id = _make_project(app, owner)
        test_case_id = 910001
        _make_baseline(app, project_id, test_case_id, step_index=0)
        _make_baseline(app, project_id, test_case_id, step_index=1)

        # 本人读取
        resp = v2_client.get(f"/api/v1/visual/baselines/{test_case_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == 200
        assert len(body["data"]) == 2

        # test_type / step_index 过滤
        resp = v2_client.get(
            f"/api/v1/visual/baselines/{test_case_id}?step_index=1", headers=headers
        )
        assert resp.status_code == 200
        assert len(resp.json()["data"]) == 1

        # 本人批准
        baseline_id = body["data"][0]["id"]
        resp = v2_client.post(
            f"/api/v1/visual/baselines/{baseline_id}/approve", headers=headers
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["status"] == "active"
        assert data["approved_by"] == owner

    def test_baseline_other_user_404(self, v2_client, app, make_user, auth_headers):
        owner = make_user(_uname())
        intruder = make_user(_uname())
        owner_headers = auth_headers(owner)
        intruder_headers = auth_headers(intruder)
        project_id = _make_project(app, owner)
        test_case_id = 910002
        baseline_id = _make_baseline(app, project_id, test_case_id)

        # 他人读取 → 404（IDOR）
        resp = v2_client.get(f"/api/v1/visual/baselines/{test_case_id}", headers=intruder_headers)
        assert resp.status_code == 404, resp.text
        assert resp.json()["message"] == "基准截图不存在"

        # 他人批准 → 404
        resp = v2_client.post(
            f"/api/v1/visual/baselines/{baseline_id}/approve", headers=intruder_headers
        )
        assert resp.status_code == 404, resp.text

        # 他人删除 → 404
        resp = v2_client.delete(
            f"/api/v1/visual/baselines/{baseline_id}", headers=intruder_headers
        )
        assert resp.status_code == 404, resp.text

        # 不存在的基准 → 404
        resp = v2_client.get("/api/v1/visual/baselines/99887766", headers=owner_headers)
        assert resp.status_code == 200  # 无资源 → 空列表（与 v1 一致）
        assert resp.json()["data"] == []
        resp = v2_client.post("/api/v1/visual/baselines/99887766/approve", headers=owner_headers)
        assert resp.status_code == 404

    def test_diffs_owner_check(self, v2_client, app, make_user, auth_headers):
        owner = make_user(_uname())
        intruder = make_user(_uname())
        owner_headers = auth_headers(owner)
        intruder_headers = auth_headers(intruder)
        project_id = _make_project(app, owner)
        run_id = _make_run(app, project_id)
        test_case_id = 910003
        _make_diff(app, run_id, test_case_id)

        # 本人 → 200 分页结构
        resp = v2_client.get(f"/api/v1/visual/diffs/{run_id}", headers=owner_headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["data"]["pagination"]["total"] == 1
        assert body["data"]["items"][0]["test_run_id"] == run_id

        # 他人 → 404（IDOR）
        resp = v2_client.get(f"/api/v1/visual/diffs/{run_id}", headers=intruder_headers)
        assert resp.status_code == 404, resp.text
        assert resp.json()["message"] == "测试运行记录不存在"

    def test_history_owner_check(self, v2_client, app, make_user, auth_headers):
        owner = make_user(_uname())
        intruder = make_user(_uname())
        owner_headers = auth_headers(owner)
        intruder_headers = auth_headers(intruder)
        project_id = _make_project(app, owner)
        run_id = _make_run(app, project_id)
        test_case_id = 910004
        _make_diff(app, run_id, test_case_id, status="visual_fail")

        # 本人 → 200 汇总
        resp = v2_client.get(f"/api/v1/visual/history/{test_case_id}", headers=owner_headers)
        assert resp.status_code == 200, resp.text
        history = resp.json()["data"]
        assert len(history) == 1
        assert history[0]["test_run_id"] == run_id
        assert history[0]["fail_count"] == 1
        assert history[0]["pass_count"] == 0

        # 他人 → 404（IDOR 修复：v1 history 无属主校验）
        resp = v2_client.get(f"/api/v1/visual/history/{test_case_id}", headers=intruder_headers)
        assert resp.status_code == 404, resp.text

    def test_delete_baseline_soft_deletes(self, v2_client, app, make_user, auth_headers):
        owner = make_user(_uname())
        headers = auth_headers(owner)
        project_id = _make_project(app, owner)
        test_case_id = 910005
        baseline_id = _make_baseline(app, project_id, test_case_id)

        resp = v2_client.delete(f"/api/v1/visual/baselines/{baseline_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "基准截图已删除"

        # 软删除后状态为 deprecated
        from app.extensions import db
        from app.models.visual_baseline import VisualBaseline

# ---------------------------------------------------------------------------
# 3. comments 创建 → 列表
# ---------------------------------------------------------------------------

class TestComments:
    def test_create_then_list(self, v2_client, app, make_user, auth_headers):
        user_id = make_user(_uname())
        headers = auth_headers(user_id)
        resource_type, resource_id = "test_case", 880001

        resp = v2_client.post(
            "/api/v1/comments",
            headers=headers,
            json={
                "resource_type": resource_type,
                "resource_id": resource_id,
                "content": "这条用例的断言需要覆盖 @admin",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == 200
        assert body["message"] == "评论创建成功"
        assert body["data"]["content"].startswith("这条用例")
        assert body["data"]["user_id"] == user_id
        comment_id = body["data"]["id"]

        # 列表（仅顶层评论）
        resp = v2_client.get(f"/api/v1/comments/{resource_type}/{resource_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["total"] == 1
        assert data["items"][0]["id"] == comment_id

        # 详情（作者可见）
        resp = v2_client.get(f"/api/v1/comments/{comment_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["id"] == comment_id

        # 编辑（作者）
        resp = v2_client.put(
            f"/api/v1/comments/{comment_id}", headers=headers, json={"content": "更新后的评论"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["content"] == "更新后的评论"

        # 删除（作者，软删除）
        resp = v2_client.delete(f"/api/v1/comments/{comment_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "评论已删除"

        # 软删除后列表不再返回
        resp = v2_client.get(f"/api/v1/comments/{resource_type}/{resource_id}", headers=headers)
        assert resp.json()["data"]["total"] == 0

    def test_create_validation(self, v2_client, app, make_user, auth_headers):
        user_id = make_user(_uname())
        headers = auth_headers(user_id)

        # 缺少必需字段
        resp = v2_client.post(
            "/api/v1/comments", headers=headers, json={"resource_type": "test_case"}
        )
        assert resp.status_code == 400, resp.text
        assert "缺少必需字段" in resp.json()["message"]

        # 非 JSON Content-Type（复刻 v1 validate_json 行为）
        resp = v2_client.post(
            "/api/v1/comments",
            headers={**headers, "Content-Type": "text/plain"},
            content=b"not-json",
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["message"] == "请求必须是 JSON 格式"

        # 不支持的资源类型
        resp = v2_client.post(
            "/api/v1/comments",
            headers=headers,
            json={"resource_type": "hack", "resource_id": 1, "content": "x"},
        )
        assert resp.status_code == 400, resp.text
        assert "不支持的资源类型" in resp.json()["message"]

    def test_comment_idor(self, v2_client, app, make_user, auth_headers):
        author = make_user(_uname())
        other = make_user(_uname())
        author_headers = auth_headers(author)
        other_headers = auth_headers(other)

        resp = v2_client.post(
            "/api/v1/comments",
            headers=author_headers,
            json={"resource_type": "test_run", "resource_id": 880002, "content": "私有评论"},
        )
        comment_id = resp.json()["data"]["id"]

        # 他人读单条评论 → 404（IDOR 修复：v1 无属主校验）
        resp = v2_client.get(f"/api/v1/comments/{comment_id}", headers=other_headers)
        assert resp.status_code == 404, resp.text

        # 他人编辑 → 403（service 层作者校验，与 v1 状态码一致）
        resp = v2_client.put(
            f"/api/v1/comments/{comment_id}", headers=other_headers, json={"content": "篡改"}
        )
        assert resp.status_code == 403, resp.text

        # 他人删除 → 403
        resp = v2_client.delete(f"/api/v1/comments/{comment_id}", headers=other_headers)
        assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# 4. notifications 列表 + 配置状态更新/测试发送
# ---------------------------------------------------------------------------

class TestNotifications:
    def _create_config(self, v2_client, headers, name="my-dingtalk"):
        resp = v2_client.post(
            "/api/v1/notifications/configs",
            headers=headers,
            json={
                "name": name,
                "channel_type": "dingtalk",
                "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=x",
            },
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["data"]

    def test_create_list_update_delete(self, v2_client, app, make_user, auth_headers):
        user_id = make_user(_uname())
        headers = auth_headers(user_id)

        config = self._create_config(v2_client, headers)
        assert config["channel"] == "dingtalk"
        assert config["is_active"] is True

        # 列表
        resp = v2_client.get("/api/v1/notifications/configs", headers=headers)
        assert resp.status_code == 200, resp.text
        items = resp.json()["data"]
        assert len(items) == 1
        assert items[0]["id"] == config["id"]

        # 更新（is_active 状态标记）
        resp = v2_client.put(
            f"/api/v1/notifications/configs/{config['id']}",
            headers=headers,
            json={"is_active": False, "name": "renamed"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["is_active"] is False
        assert data["name"] == "renamed"

        # 删除
        resp = v2_client.delete(f"/api/v1/notifications/configs/{config['id']}", headers=headers)
        assert resp.status_code == 200, resp.text
        resp = v2_client.get("/api/v1/notifications/configs", headers=headers)
        assert resp.json()["data"] == []

    def test_create_validation(self, v2_client, app, make_user, auth_headers):
        user_id = make_user(_uname())
        headers = auth_headers(user_id)

        resp = v2_client.post(
            "/api/v1/notifications/configs", headers=headers, json={"name": "x"}
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["message"] == "channel_type 不能为空"

        resp = v2_client.post(
            "/api/v1/notifications/configs",
            headers=headers,
            json={"name": "x", "channel_type": "sms", "webhook_url": "https://e.com"},
        )
        assert resp.status_code == 400, resp.text
        assert "channel_type 必须是" in resp.json()["message"]

    def test_test_send_stubbed(self, v2_client, app, make_user, auth_headers, monkeypatch):
        """test 端点：stub 真实 HTTP 发送，覆盖成功与失败分支"""
        user_id = make_user(_uname())
        headers = auth_headers(user_id)
        config = self._create_config(v2_client, headers)

        import app.services.notification_service as ns

        monkeypatch.setattr(
            ns, "send_notification", lambda **kw: {"success": True, "status_code": 200}
        )
        resp = v2_client.post(f"/api/v1/notifications/configs/{config['id']}/test", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "测试通知发送成功"

        monkeypatch.setattr(
            ns, "send_notification", lambda **kw: {"success": False, "error": "boom"}
        )
        resp = v2_client.post(f"/api/v1/notifications/configs/{config['id']}/test", headers=headers)
        assert resp.status_code == 500, resp.text
        assert "测试通知发送失败" in resp.json()["message"]


# ---------------------------------------------------------------------------
# 5. 他人资源 404（notifications / alert_rules / dashboard）
# ---------------------------------------------------------------------------

class TestOthersResource404:
    def test_notification_config_other_user_404(
        self, v2_client, app, make_user, auth_headers
    ):
        owner = make_user(_uname())
        other = make_user(_uname())
        owner_headers = auth_headers(owner)
        other_headers = auth_headers(other)

        resp = v2_client.post(
            "/api/v1/notifications/configs",
            headers=owner_headers,
            json={"name": "private", "channel_type": "slack", "webhook_url": "https://h/s"},
        )
        config_id = resp.json()["data"]["id"]

        for method in ("put", "delete"):
            if method == "put":
                resp = v2_client.put(
                    f"/api/v1/notifications/configs/{config_id}",
                    headers=other_headers,
                    json={"name": "hijack"},
                )
                assert resp.status_code == 404, resp.text
            else:
                resp = v2_client.delete(
                    f"/api/v1/notifications/configs/{config_id}", headers=other_headers
                )
                assert resp.status_code == 404, resp.text
        resp = v2_client.post(
            f"/api/v1/notifications/configs/{config_id}/test", headers=other_headers
        )
        assert resp.status_code == 404, resp.text

        # 本人仍可读
        resp = v2_client.get("/api/v1/notifications/configs", headers=owner_headers)
        assert len(resp.json()["data"]) == 1
        # 他人列表为空（user_id 隔离）
        resp = v2_client.get("/api/v1/notifications/configs", headers=other_headers)
        assert resp.json()["data"] == []

    def test_alert_rule_other_user_404(self, v2_client, app, make_user, auth_headers):
        owner = make_user(_uname())
        other = make_user(_uname())
        owner_headers = auth_headers(owner)
        other_headers = auth_headers(other)

        scenario_id = _make_scenario(app, owner)
        rule_id = _make_alert_rule(app, "owner-rule", scenario_id=scenario_id)

        # 他人 GET/PUT/DELETE/evaluate → 404（scenario 属主校验）
        resp = v2_client.get(f"/api/v1/perf-test/alert-rules/{rule_id}", headers=other_headers)
        assert resp.status_code == 404, resp.text
        resp = v2_client.put(
            f"/api/v1/perf-test/alert-rules/{rule_id}",
            headers=other_headers,
            json={"name": "hijack"},
        )
        assert resp.status_code == 404, resp.text
        resp = v2_client.delete(f"/api/v1/perf-test/alert-rules/{rule_id}", headers=other_headers)
        assert resp.status_code == 404, resp.text
        resp = v2_client.post(
            f"/api/v1/perf-test/alert-rules/{rule_id}/evaluate",
            headers=other_headers,
            json={"test_result_id": 1},
        )
        assert resp.status_code == 404, resp.text

        # 规则列表：他人看不到关联他人场景的规则
        resp = v2_client.get("/api/v1/perf-test/alert-rules", headers=other_headers)
        assert all(r["id"] != rule_id for r in resp.json()["data"])
        # 本人可见
        resp = v2_client.get("/api/v1/perf-test/alert-rules", headers=owner_headers)
        assert any(r["id"] == rule_id for r in resp.json()["data"])

    def test_alert_rule_create_cannot_reference_others_scenario(
        self, v2_client, app, make_user, auth_headers
    ):
        """IDOR 修复：创建/改挂规则时引用他人场景 → 404"""
        owner = make_user(_uname())
        other = make_user(_uname())
        other_headers = auth_headers(other)
        scenario_id = _make_scenario(app, owner)

        resp = v2_client.post(
            "/api/v1/perf-test/alert-rules",
            headers=other_headers,
            json={"name": "steal", "scenario_id": scenario_id, "p95_threshold": 100},
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["message"] == "关联场景不存在"

    def test_alert_logs_scoped_to_visible_rules(
        self, v2_client, app, make_user, auth_headers
    ):
        """IDOR 修复：alert-logs 仅返回可见规则（全局 + 本人场景）的日志"""
        owner = make_user(_uname())
        other = make_user(_uname())
        owner_headers = auth_headers(owner)
        other_headers = auth_headers(other)

        scenario_id = _make_scenario(app, owner)
        result_id = _make_perf_result(app, scenario_id)
        rule_id = _make_alert_rule(app, "scoped-rule", scenario_id=scenario_id)
        _make_alert_log(app, rule_id, result_id)

        # 本人可见
        resp = v2_client.get(f"/api/v1/perf-test/alert-logs?rule_id={rule_id}", headers=owner_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["pagination"]["total"] == 1

        # 他人 → 该规则日志被过滤（total=0）
        resp = v2_client.get(f"/api/v1/perf-test/alert-logs?rule_id={rule_id}", headers=other_headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["pagination"]["total"] == 0

    def test_dashboard_widgets_user_isolation(self, v2_client, app, make_user, auth_headers):
        user_a = make_user(_uname())
        user_b = make_user(_uname())
        headers_a = auth_headers(user_a)
        headers_b = auth_headers(user_b)

        widgets = [
            {"widget_type": "summary", "title": "概览", "position_x": 0, "position_y": 0},
            {"widget_type": "trend", "title": "趋势", "position_x": 1, "position_y": 0},
        ]
        resp = v2_client.put("/api/v1/dashboard/widgets", headers=headers_a, json={"widgets": widgets})
        assert resp.status_code == 200, resp.text
        assert len(resp.json()["data"]) == 2

        # 用户 B 看不到用户 A 的布局（user_id 隔离）
        resp = v2_client.get("/api/v1/dashboard/widgets", headers=headers_b)
        assert resp.status_code == 200
        assert resp.json()["data"] == []

        # widget-types
        resp = v2_client.get("/api/v1/dashboard/widget-types", headers=headers_b)
        assert resp.status_code == 200, resp.text
        assert isinstance(resp.json()["data"], dict)

        # reset（仅清自己的）
        resp = v2_client.post("/api/v1/dashboard/widgets/reset", headers=headers_a)
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "已恢复默认布局"
        resp = v2_client.get("/api/v1/dashboard/widgets", headers=headers_a)
        assert resp.json()["data"] == []


# ---------------------------------------------------------------------------
# 6. branding / swagger_gen / global_search
# ---------------------------------------------------------------------------

class TestBranding:
    def test_get_public(self, v2_client):
        """GET /branding/config 为 v1 公开端点（前端启动时调用）"""
        resp = v2_client.get("/api/v1/branding/config")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == 200
        assert body["data"]["platform_name"] == "大熊AI测试平台"
        assert body["data"]["primary_color"] == "#5FA59B"

    def test_update_requires_admin(self, v2_client, app, make_user, auth_headers):
        member = make_user(_uname())
        resp = v2_client.put(
            "/api/v1/branding/config",
            headers=auth_headers(member),
            json={"platform_name": "Hacked"},
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["message"] == "需要管理员权限"

        # 全局配置不被修改
        resp = v2_client.get("/api/v1/branding/config")
        assert resp.json()["data"]["platform_name"] == "大熊AI测试平台"

    def test_admin_updates_config(self, v2_client, app, make_user, auth_headers):
        admin = make_user(_uname(), role="admin")
        headers = auth_headers(admin)

        resp = v2_client.put(
            "/api/v1/branding/config",
            headers=headers,
            json={"platform_name": "AcmeQA", "primary_color": "#123456"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["platform_name"] == "AcmeQA"
        assert data["primary_color"] == "#123456"

        # 公开读取可见
        resp = v2_client.get("/api/v1/branding/config")
        assert resp.json()["data"]["platform_name"] == "AcmeQA"


class TestSwaggerGen:
    SWAGGER = (
        '{"openapi": "3.0.0", "info": {"title": "Demo API"}, '
        '"paths": {"/ping": {"get": {"responses": {"200": {"description": "ok"}}}}}}'
    )

    def test_generate_validation(self, v2_client, app, make_user, auth_headers):
        user_id = make_user(_uname())
        headers = auth_headers(user_id)

        resp = v2_client.post(
            "/api/v1/ai/generate-cases-from-swagger", headers=headers, json={}
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["message"] == "swagger_content is required"

        resp = v2_client.post(
            "/api/v1/ai/generate-cases-from-swagger",
            headers=headers,
            json={"swagger_content": self.SWAGGER, "content_type": "xml"},
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["message"] == 'content_type must be "json" or "yaml"'

    def test_generate_without_save(self, v2_client, app, make_user, auth_headers, monkeypatch):
        """生成端点：AI 服务以桩替换（与本仓 test_v1_ai_modules.py 做法一致）"""
        user_id = make_user(_uname())
        headers = auth_headers(user_id)

        import app.api.routes.swagger_gen as swagger_mod

        fake_result = {
            "spec_info": {"title": "Demo API"},
            "endpoints_count": 1,
            "generated_cases": [
                {"name": "GET /ping", "method": "GET", "url": "{baseUrl}/ping",
                 "expected_status": 200}
            ],
            "summary": {"total_cases": 1},
        }
        monkeypatch.setattr(
            swagger_mod.swagger_case_generator, "generate_cases", lambda **kw: fake_result
        )

        resp = v2_client.post(
            "/api/v1/ai/generate-cases-from-swagger",
            headers=headers,
            json={"swagger_content": self.SWAGGER},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["endpoints_count"] == 1
        assert "saved_count" not in data
        assert data["generated_cases"][0]["name"] == "GET /ping"

    def test_save_requires_accessible_project(self, v2_client, app, make_user, auth_headers):
        """IDOR 修复：project_id 不在可访问域 → 404"""
        owner = make_user(_uname())
        other = make_user(_uname())
        other_headers = auth_headers(other)
        project_id = _make_project(app, owner)

        # cases 为空 → 400
        resp = v2_client.post(
            "/api/v1/ai/generate-cases-from-swagger/save",
            headers=other_headers,
            json={"cases": [], "project_id": project_id},
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["message"] == "cases is required and must be a non-empty array"

        # project_id 缺失 → 400
        resp = v2_client.post(
            "/api/v1/ai/generate-cases-from-swagger/save",
            headers=other_headers,
            json={"cases": [{"name": "c", "method": "GET", "url": "/x"}]},
        )
        assert resp.status_code == 400, resp.text

        # 他人项目 → 404（IDOR 修复）
        cases = [{"name": "ping", "method": "GET", "url": "/ping", "expected_status": 200}]
        resp = v2_client.post(
            "/api/v1/ai/generate-cases-from-swagger/save",
            headers=other_headers,
            json={"cases": cases, "project_id": project_id},
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["message"] == "项目不存在"

        # 本人项目 → 保存成功
        owner_headers = auth_headers(owner)
        resp = v2_client.post(
            "/api/v1/ai/generate-cases-from-swagger/save",
            headers=owner_headers,
            json={"cases": cases, "project_id": project_id, "collection_name": "AI Cases"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["saved_count"] == 1


class TestGlobalSearch:
    def test_query_required(self, v2_client, app, make_user, auth_headers):
        user_id = make_user(_uname())
        headers = auth_headers(user_id)

        resp = v2_client.post("/api/v1/ai/global-search", headers=headers, json={"query": "   "})
        assert resp.status_code == 400, resp.text
        assert resp.json()["message"] == "query is required"

        resp = v2_client.post("/api/v1/ai/global-search", headers=headers, json={})
        assert resp.status_code == 400, resp.text


# ---------------------------------------------------------------------------
# 7. GitLab webhook
# ---------------------------------------------------------------------------

class TestGitlabWebhooks:
    def test_event_ignored_public(self, v2_client):
        """公开端点：未知事件类型 → 200 ignored"""
        resp = v2_client.post(
            "/api/v1/webhooks/gitlab",
            json={"project": {"path_with_namespace": "g/demo"}},
            headers={"X-Gitlab-Event": "Note Hook"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "Event Note Hook ignored"

    def test_signature_missing_401(self, v2_client, app, monkeypatch):
        """配置 GITLAB_WEBHOOK_SECRET 后缺签名 → 401（公开端点的鉴权边界）"""
        monkeypatch.setitem(get_config(), "GITLAB_WEBHOOK_SECRET", "shhh")
        resp = v2_client.post(
            "/api/v1/webhooks/gitlab",
            json={},
            headers={"X-Gitlab-Event": "Push Hook"},
        )
        assert resp.status_code == 401, resp.text
        assert resp.json()["message"] == "Missing signature"

    def test_signature_invalid_401(self, v2_client, app, monkeypatch):
        monkeypatch.setitem(get_config(), "GITLAB_WEBHOOK_SECRET", "shhh")
        resp = v2_client.post(
            "/api/v1/webhooks/gitlab",
            json={},
            headers={"X-Gitlab-Event": "Push Hook", "X-Gitlab-Token": "sha256=deadbeef"},
        )
        assert resp.status_code == 401, resp.text
        assert resp.json()["message"] == "Signature verification failed"

    def test_push_event_no_trigger(self, v2_client, app, monkeypatch):
        """合法签名 + Push Hook：无匹配触发规则 → 200 No trigger matched"""
        secret = "shhh"
        monkeypatch.setitem(get_config(), "GITLAB_WEBHOOK_SECRET", secret)
        payload = (
            '{"project": {"path_with_namespace": "g/lab-'
            + uuid.uuid4().hex[:8]
            + '"}, "ref": "refs/heads/main", "commits": []}'
        ).encode()
        sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

        resp = v2_client.post(
            "/api/v1/webhooks/gitlab",
            content=payload,
            headers={
                "X-Gitlab-Event": "Push Hook",
                "X-Gitlab-Token": sig,
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "No trigger matched"

    def test_push_event_creates_test_run(self, v2_client, app, monkeypatch):
        """合法签名 + Push Hook：命中触发规则 → 创建 TestRun 并返回 test_run_id"""
        from app.extensions import db
        from app.models.project import Project
        from app.models.trigger_rule import TriggerRule

        secret = "shhh"
        monkeypatch.setitem(get_config(), "GITLAB_WEBHOOK_SECRET", secret)
        repo = f"g/lab-{uuid.uuid4().hex[:8]}"
        payload = (
            '{"project": {"path_with_namespace": "'
            + repo
            + '"}, "ref": "refs/heads/main", "commits": [{"id": "abc", "message": "feat"}]}'
        ).encode()
        sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()

        # 预置 owner_id=1 的同名项目 + api 类型 push 触发规则
        # 目标集合不存在时调度失败会被捕获（与 v1 一致，不影响响应）
        project = Project(
            name=repo.split("/")[-1],
            owner_id=1,
        )
        db.session.add(project)
        db.session.flush()
        rule = TriggerRule(
            project_id=project.id,
            name="push-api",
            created_by=1,
            trigger_event="push",
            target_branches=["main"],
            test_types=["api"],
            target_type="api_collection",
        )
        db.session.add(rule)
        db.session.commit()

        resp = v2_client.post(
            "/api/v1/webhooks/gitlab",
            content=payload,
            headers={
                "X-Gitlab-Event": "Push Hook",
                "X-Gitlab-Token": sig,
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["message"] == "success"
        assert body["data"]["triggered_by"] == "push"
        assert body["data"]["test_run_id"]
