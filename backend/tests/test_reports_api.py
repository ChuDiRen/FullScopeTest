"""
报告模块 API 集成测试

覆盖：测试执行记录 CRUD、报告列表/详情/统计、健康检查、权限校验
"""

import uuid


def _auth_headers(client):
    username = f"rp_{uuid.uuid4().hex[:8]}"
    password = "Passw0rd!"
    email = f"{username}@example.com"
    client.post("/api/v1/auth/register", json={"username": username, "email": email, "password": password})
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    token = resp.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _create_project(client, headers, name=None):
    if name is None:
        name = f"Proj_{uuid.uuid4().hex[:8]}"
    resp = client.post("/api/v1/projects", headers=headers, json={"name": name})
    return resp.json()["data"]


# ====================================================================
# 测试执行记录
# ====================================================================

class TestTestRunCRUD:
    def test_create_test_run(self, client):
        headers = _auth_headers(client)
        project = _create_project(client, headers)
        resp = client.post("/api/v1/test-runs", headers=headers, json={
            "project_id": project["id"],
            "test_type": "api",
            "test_object_name": "User Login Test",
            "total_cases": 5,
        })
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["project_id"] == project["id"]
        assert data["status"] == "pending"

    def test_create_test_run_missing_project(self, client):
        headers = _auth_headers(client)
        resp = client.post("/api/v1/test-runs", headers=headers, json={
            "test_type": "api",
        })
        assert resp.status_code == 400

    def test_create_test_run_project_not_found(self, client):
        headers = _auth_headers(client)
        resp = client.post("/api/v1/test-runs", headers=headers, json={
            "project_id": 99999,
            "test_type": "api",
        })
        assert resp.status_code == 404

    def test_get_test_runs_list(self, client):
        headers = _auth_headers(client)
        project = _create_project(client, headers)
        client.post("/api/v1/test-runs", headers=headers, json={
            "project_id": project["id"],
            "test_type": "api",
        })
        resp = client.get(f"/api/v1/test-runs?project_id={project['id']}", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "data" in data

    def test_get_test_runs_filter_by_type(self, client):
        headers = _auth_headers(client)
        project = _create_project(client, headers)
        client.post("/api/v1/test-runs", headers=headers, json={
            "project_id": project["id"], "test_type": "api",
        })
        resp = client.get(f"/api/v1/test-runs?project_id={project['id']}&test_type=api", headers=headers)
        assert resp.status_code == 200

    def test_get_test_runs_filter_by_status(self, client):
        headers = _auth_headers(client)
        project = _create_project(client, headers)
        client.post("/api/v1/test-runs", headers=headers, json={
            "project_id": project["id"], "test_type": "api",
        })
        resp = client.get(f"/api/v1/test-runs?project_id={project['id']}&status=pending", headers=headers)
        assert resp.status_code == 200

    def test_get_test_run_detail(self, client):
        headers = _auth_headers(client)
        project = _create_project(client, headers)
        create_resp = client.post("/api/v1/test-runs", headers=headers, json={
            "project_id": project["id"], "test_type": "api",
        })
        run_id = create_resp.json()["data"]["id"]
        resp = client.get(f"/api/v1/test-runs/{run_id}", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["data"]["id"] == run_id

    def test_get_test_run_detail_not_found(self, client):
        headers = _auth_headers(client)
        resp = client.get("/api/v1/test-runs/99999", headers=headers)
        assert resp.status_code == 404

    def test_create_test_run_unauthorized(self, client):
        resp = client.post("/api/v1/test-runs", json={"project_id": 1, "test_type": "api"})
        assert resp.status_code == 401

    def test_get_test_runs_unauthorized(self, client):
        resp = client.get("/api/v1/test-runs")
        assert resp.status_code == 401


# ====================================================================
# 报告列表（test-reports 端点）
# ====================================================================

class TestReportList:
    def test_get_reports_list_by_project(self, client):
        """按项目过滤报告列表（paginate bug 已修复）"""
        headers = _auth_headers(client)
        project = _create_project(client, headers)
        resp = client.get(f"/api/v1/test-reports?project_id={project['id']}", headers=headers)
        assert resp.status_code == 200
        assert "items" in resp.json()["data"]

    def test_get_report_not_found(self, client):
        headers = _auth_headers(client)
        resp = client.get("/api/v1/test-reports/99999", headers=headers)
        assert resp.status_code == 404

    def test_get_reports_unauthorized(self, client):
        resp = client.get("/api/v1/test-reports")
        assert resp.status_code == 401


# ====================================================================
# 报告统计与仪表盘
# ====================================================================

class TestReportDashboard:
    def test_get_dashboard(self, client):
        headers = _auth_headers(client)
        resp = client.get("/api/v1/reports/dashboard", headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert "data" in data

    def test_get_statistics(self, client):
        headers = _auth_headers(client)
        resp = client.get("/api/v1/reports/statistics", headers=headers)
        assert resp.status_code == 200

    def test_get_statistics_with_project(self, client):
        headers = _auth_headers(client)
        project = _create_project(client, headers)
        resp = client.get(f"/api/v1/reports/statistics?project_id={project['id']}", headers=headers)
        assert resp.status_code == 200


# ====================================================================
# 报告导出
# ====================================================================

class TestReportExport:
    def test_export_nonexistent_report_returns_404(self, client):
        headers = _auth_headers(client)
        resp = client.get("/api/v1/reports/99999/export", headers=headers)
        assert resp.status_code in (404, 400)

    def test_report_html_nonexistent(self, client):
        headers = _auth_headers(client)
        resp = client.get("/api/v1/test-reports/99999/html", headers=headers)
        assert resp.status_code in (404, 400)


# ====================================================================
# IDOR 防护
# ====================================================================

class TestReportIDOR:
    def test_get_test_run_enforces_ownership_check(self, client):
        """报告 test-run 详情接口必须校验所有权：用户 B 访问用户 A 的执行记录 → 404"""
        headers_a = _auth_headers(client)
        project_a = _create_project(client, headers_a, "ProjectA")
        create_resp = client.post("/api/v1/test-runs", headers=headers_a, json={
            "project_id": project_a["id"], "test_type": "api",
        })
        run_id = create_resp.json()["data"]["id"]

        headers_b = _auth_headers(client)
        resp = client.get(f"/api/v1/test-runs/{run_id}", headers=headers_b)
        assert resp.status_code == 404  # 属主过滤生效，越权 404
