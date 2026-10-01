"""
v1 reports 平迁路由测试（FastAPI 实现，自 backend/app/api/reports.py 平迁）

覆盖：
1. 公开端点 /api/v1/reports/health 200
2. 未登录访问受保护端点 → 401
3. 创建测试报告数据（项目/执行记录/报告）→ 列表/详情 200 且字段正确
4. 访问他人报告/执行记录 → 404（IDOR 修复验证）
5. 导出端点返回正确 Content-Type 与 Content-Disposition
"""

import uuid

import pytest
from app.extensions import db


@pytest.fixture(autouse=True)
def _stub_token_blacklist(monkeypatch):
    """鉴权依赖 get_current_user 会在请求期查询 Redis（token 黑名单/版本校验），
    部分环境下 Redis 连接会无限阻塞（无 socket 超时）。测试内替换为无网络实现：
    - is_token_blacklisted → False（等价 Redis 不可用时的降级放行策略）
    - is_token_version_valid → True（等价 token 无版本声明时的默认通过）
    deps.py 为调用期惰性 import，patch 模块属性即可生效。"""
    import app.services.token_blacklist as tb

    monkeypatch.setattr(tb, "is_token_blacklisted", lambda jti: False)
    monkeypatch.setattr(tb, "is_token_version_valid", lambda user_id, version: True)


def _uname() -> str:
    return f"rep_{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------------------
# 数据工厂（直接用 app fixture 的 db 构造模型数据）
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

def _make_report(app, project_id: int, run_id: int, **extra) -> int:
    from app.models.test_report import TestReport
    title = extra.pop("title", "接口测试报告")
    report = TestReport(
        project_id=project_id,
        test_run_id=run_id,
        test_type="api",
        title=title,
        summary=extra.pop("summary", {"total": 10, "passed": 8, "failed": 2}),
        report_data=extra.pop(
            "report_data",
            {"results": [{"status_code": 200, "name": "示例接口", "passed": True}]},
        ),
        **extra,
    )
    db.session.add(report)
    db.session.commit()
    return report.id

# ---------------------------------------------------------------------------
# 1. 公开端点 + 未登录 401
# ---------------------------------------------------------------------------

class TestReportsAuth:
    def test_health_public(self, v2_client):
        """健康检查是 v1 公开端点，无鉴权应 200"""
        resp = v2_client.get("/api/v1/reports/health")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == 200
        assert body["message"] == "报告模块正常"

    @pytest.mark.parametrize(
        "method,url",
        [
            ("get", "/api/v1/test-runs"),
            ("get", "/api/v1/test-reports"),
            ("get", "/api/v1/reports/statistics"),
            ("get", "/api/v1/reports/dashboard"),
            ("get", "/api/v1/reports/trend"),
            ("get", "/api/v1/reports/percentiles"),
        ],
    )
    def test_unauthenticated_401(self, v2_client, method, url):
        """未登录访问受保护端点 → 401，响应结构与 v1 error_response 一致"""
        resp = getattr(v2_client, method)(url)
        assert resp.status_code == 401, f"{method.upper()} {url}: {resp.text}"
        body = resp.json()
        assert body["code"] == 401

    def test_post_run_unauthenticated_401(self, v2_client):
        resp = v2_client.post("/api/v1/test-runs", json={"project_id": 1})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 2. 创建 → 列表/详情/统计 200 且字段正确
# ---------------------------------------------------------------------------

class TestReportsCRUD:
    def test_create_list_detail_update_run(self, v2_client, app, make_user, auth_headers):
        user_id = make_user(_uname())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)

        # 创建执行记录
        resp = v2_client.post(
            "/api/v1/test-runs",
            headers=headers,
            json={
                "project_id": project_id,
                "test_type": "api",
                "test_object_name": "登录接口",
                "total_cases": 5,
                "environment_name": "test-env",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == 200
        assert body["message"] == "创建成功"
        run = body["data"]
        assert run["project_id"] == project_id
        assert run["test_type"] == "api"
        assert run["status"] == "pending"
        assert run["total_cases"] == 5
        assert run["environment_name"] == "test-env"
        assert run["triggered_user_id"] == user_id
        run_id = run["id"]

        # 列表
        resp = v2_client.get("/api/v1/test-runs", headers=headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["pagination"]["total"] >= 1
        assert data["pagination"]["per_page"] == 20
        assert any(item["id"] == run_id for item in data["items"])

        # 详情
        resp = v2_client.get(f"/api/v1/test-runs/{run_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        detail = resp.json()["data"]
        assert detail["id"] == run_id
        assert detail["test_object_name"] == "登录接口"

        # 更新
        resp = v2_client.put(
            f"/api/v1/test-runs/{run_id}",
            headers=headers,
            json={"status": "success", "passed": 5},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["status"] == "success"
        assert resp.json()["data"]["passed"] == 5

    def test_report_list_and_detail(self, v2_client, app, make_user, auth_headers):
        user_id = make_user(_uname())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)
        run_id = _make_run(app, project_id, user_id)
        report_id = _make_report(app, project_id, run_id)

        # 报告列表
        resp = v2_client.get("/api/v1/test-reports", headers=headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["pagination"]["total"] >= 1
        item = next(i for i in data["items"] if i["id"] == report_id)
        assert item["title"] == "接口测试报告"
        assert item["test_type"] == "api"
        assert item["test_run_id"] == run_id
        assert item["project_id"] == project_id

        # 报告详情（含 report_data）
        resp = v2_client.get(f"/api/v1/test-reports/{report_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        detail = resp.json()["data"]
        assert detail["id"] == report_id
        assert detail["title"] == "接口测试报告"
        assert detail["summary"]["passed"] == 8
        assert detail["report_data"]["results"][0]["status_code"] == 200

        # 按项目过滤
        resp = v2_client.get(
            f"/api/v1/test-reports?project_id={project_id}", headers=headers
        )
        assert resp.status_code == 200
        assert any(i["id"] == report_id for i in resp.json()["data"]["items"])

    def test_statistics_and_dashboard(self, v2_client, app, make_user, auth_headers):
        from app.models.test_run import TestRun

        user_id = make_user(_uname())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)
        _make_run(app, project_id, user_id)

        # 统计
        resp = v2_client.get("/api/v1/reports/statistics", headers=headers)
        assert resp.status_code == 200, resp.text
        stats = resp.json()["data"]
        assert stats["summary"]["total_runs"] >= 1
        assert stats["summary"]["success_runs"] >= 1
        assert len(stats["daily_trend"]) == 7  # 默认 days=7

        # 仪表盘
        resp = v2_client.get("/api/v1/reports/dashboard", headers=headers)
        assert resp.status_code == 200, resp.text
        dash = resp.json()["data"]
        assert dash["api_tests"]["total"] >= 0
        assert isinstance(dash["recent_runs"], list)

        # 带 response_time 的执行记录（分位数统计来源）
        run_with_rt = TestRun(
            project_id=project_id,
            triggered_user_id=user_id,
            test_type="api",
            status="success",
            total_cases=3,
            passed=3,
            results=[
                {"response_time": 100},
                {"response_time": 200},
                {"response_time": 300},
            ],
        )
        db.session.add(run_with_rt)
        db.session.commit()

        # 分位数（results 内有 response_time）
        resp = v2_client.get("/api/v1/reports/percentiles", headers=headers)
        assert resp.status_code == 200, resp.text
        p = resp.json()["data"]
        assert p["total_requests"] >= 1
        assert p["p50"] > 0


# ---------------------------------------------------------------------------
# 3. 访问他人报告 → 404（IDOR 修复）
# ---------------------------------------------------------------------------

class TestReportsIDOR:
    def test_other_user_resources_404(self, v2_client, app, make_user, auth_headers):
        owner_id = make_user(_uname())
        attacker_id = make_user(_uname())
        owner_headers = auth_headers(owner_id)
        attacker_headers = auth_headers(attacker_id)

        project_id = _make_project(app, owner_id)
        run_id = _make_run(app, project_id, owner_id)
        report_id = _make_report(app, project_id, run_id)

        # 他人执行记录/报告 → 404
        assert v2_client.get(
            f"/api/v1/test-runs/{run_id}", headers=attacker_headers
        ).status_code == 404
        assert v2_client.get(
            f"/api/v1/test-reports/{report_id}", headers=attacker_headers
        ).status_code == 404
        assert v2_client.get(
            f"/api/v1/test-reports/{report_id}/html", headers=attacker_headers
        ).status_code == 404

        # 他人报告/执行记录不可删 → 404
        assert v2_client.delete(
            f"/api/v1/test-reports/{report_id}", headers=attacker_headers
        ).status_code == 404
        assert v2_client.delete(
            f"/api/v1/test-runs/{run_id}", headers=attacker_headers
        ).status_code == 404

        # 他人数据导出 → 404（v1 服务层无属主校验，为 IDOR 修复点）
        for suffix in ("excel", "csv", "pdf"):
            resp = v2_client.get(
                f"/api/v1/test-runs/{run_id}/export/{suffix}", headers=attacker_headers
            )
            assert resp.status_code == 404, f"export/{suffix}: {resp.text}"

        # 他人项目范围导出/趋势 → 404
        assert v2_client.get(
            f"/api/v1/reports/export/excel?project_id={project_id}",
            headers=attacker_headers,
        ).status_code == 404
        assert v2_client.get(
            f"/api/v1/reports/trend?project_id={project_id}", headers=attacker_headers
        ).status_code == 404

        # owner 本人访问一切正常
        assert v2_client.get(
            f"/api/v1/test-runs/{run_id}", headers=owner_headers
        ).status_code == 200
        assert v2_client.get(
            f"/api/v1/test-reports/{report_id}", headers=owner_headers
        ).status_code == 200

    def test_deleted_report_404_and_cleanup(self, v2_client, app, make_user, auth_headers):
        user_id = make_user(_uname())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)
        run_id = _make_run(app, project_id, user_id)
        report_id = _make_report(app, project_id, run_id)

        resp = v2_client.delete(f"/api/v1/test-reports/{report_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "删除成功"

        # 删除后详情 404
        assert v2_client.get(
            f"/api/v1/test-reports/{report_id}", headers=headers
        ).status_code == 404


# ---------------------------------------------------------------------------
# 4. 导出端点 Content-Type / Content-Disposition
# ---------------------------------------------------------------------------

class TestReportsExport:
    def test_export_csv_content_type(self, v2_client, app, make_user, auth_headers):
        user_id = make_user(_uname())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)
        run_id = _make_run(app, project_id, user_id)

        resp = v2_client.get(
            f"/api/v1/test-runs/{run_id}/export/csv", headers=headers
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"] == "text/csv; charset=utf-8"
        assert (
            resp.headers["content-disposition"]
            == f"attachment; filename=test_report_{run_id}.csv"
        )
        assert "执行 ID" in resp.text

    def test_export_report_json_and_html(self, v2_client, app, make_user, auth_headers):
        user_id = make_user(_uname())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)
        run_id = _make_run(app, project_id, user_id, total_cases=10, passed=8, failed=2)

        # json 格式
        resp = v2_client.get(
            f"/api/v1/reports/{run_id}/export", headers=headers
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["generated_by"] == "FullScopeTest"
        assert data["report"]["id"] == run_id
        assert "generated_at" in data

        # html 格式（文件下载）
        resp = v2_client.get(
            f"/api/v1/reports/{run_id}/export?format=html", headers=headers
        )
        assert resp.status_code == 200, resp.text
        assert "text/html" in resp.headers["content-type"]
        assert resp.headers["content-disposition"].startswith(
            f"attachment; filename=report_{run_id}_"
        )
        assert "<!DOCTYPE html>" in resp.text

        # 不支持的格式
        resp = v2_client.get(
            f"/api/v1/reports/{run_id}/export?format=docx", headers=headers
        )
        assert resp.status_code == 400

    def test_report_html_endpoint(self, v2_client, app, make_user, auth_headers):
        user_id = make_user(_uname())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)
        run_id = _make_run(app, project_id, user_id)
        report_id = _make_report(app, project_id, run_id)

        resp = v2_client.get(
            f"/api/v1/test-reports/{report_id}/html", headers=headers
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"] == "text/html; charset=utf-8"
        assert "<html" in resp.text
        assert "接口测试报告" in resp.text
