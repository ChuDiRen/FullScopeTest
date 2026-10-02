"""
审计日志完善测试

覆盖：查询 API、过滤、统计、不可修改/删除
"""
from sqlalchemy import func, select
from app.extensions import db
import uuid
from datetime import datetime


def _auth_headers(client, username=None):
    uid = uuid.uuid4().hex[:8]
    username = username or f"audit_{uid}"
    password = "Passw0rd!"
    email = f"{username}@example.com"
    client.post("/api/v1/auth/register", json={"username": username, "email": email, "password": password})
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    token = resp.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


class TestAuditLogsAPI:
    """审计日志查询 API 测试"""

    def test_list_audit_logs_empty(self, client, no_rate_limit):
        """没有日志时返回空列表"""
        headers = _auth_headers(client)
        resp = client.get("/api/v1/audit-logs", headers=headers)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["total"] >= 0
        assert "items" in data

    def test_list_audit_logs_with_filters(self, client, no_rate_limit):
        """带过滤参数查询"""
        headers = _auth_headers(client)
        resp = client.get("/api/v1/audit-logs?action=create&resource_type=project", headers=headers)
        assert resp.status_code == 200

    def test_list_audit_logs_with_user_filter(self, client, no_rate_limit):
        """按用户过滤"""
        headers = _auth_headers(client)
        resp = client.get("/api/v1/audit-logs?user_id=1", headers=headers)
        assert resp.status_code == 200

    def test_list_audit_logs_with_time_filter(self, client, no_rate_limit):
        """按时间范围过滤"""
        headers = _auth_headers(client)
        resp = client.get(
            "/api/v1/audit-logs?start_time=2026-01-01T00:00:00Z&end_time=2026-12-31T23:59:59Z",
            headers=headers,
        )
        assert resp.status_code == 200

    def test_list_audit_logs_invalid_time(self, client, no_rate_limit):
        """无效时间格式应返回 400"""
        headers = _auth_headers(client)
        resp = client.get("/api/v1/audit-logs?start_time=invalid", headers=headers)
        assert resp.status_code == 400

    def test_list_audit_logs_pagination(self, client, no_rate_limit):
        """分页参数"""
        headers = _auth_headers(client)
        resp = client.get("/api/v1/audit-logs?page=1&per_page=5", headers=headers)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["page"] == 1
        assert data["per_page"] == 5

    def test_get_audit_log_detail(self, app, client, no_rate_limit):
        """获取单条审计日志详情"""
        from app.extensions import db
        from app.models.audit_log import AuditLog

        # 直接插入一条日志（log_action 服务已移除）
        log = AuditLog(action='create', resource_type='test_resource', resource_id=42)
        db.session.add(log)
        db.session.commit()
        log_id = log.id

        headers = _auth_headers(client, username='audit_detail_user')
        from app.models.user import User
        uid = db.session.scalar(select(User).filter_by(username='audit_detail_user')).id
        log.user_id = uid
        db.session.commit()

        resp = client.get(f"/api/v1/audit-logs/{log_id}", headers=headers)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["action"] == "create"
        assert data["resource_type"] == "test_resource"

    def test_get_audit_log_not_found(self, client, no_rate_limit):
        """获取不存在的日志应返回 404"""
        headers = _auth_headers(client)
        resp = client.get("/api/v1/audit-logs/99999", headers=headers)
        assert resp.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# 三、审计统计 API 测试
# ══════════════════════════════════════════════════════════════════════════════

class TestAuditStatsAPI:
    """审计统计 API 测试"""

    def test_get_audit_stats(self, client, no_rate_limit):
        """获取审计统计"""
        headers = _auth_headers(client)
        resp = client.get("/api/v1/audit-logs/stats?days=30", headers=headers)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert "period_days" in data
        assert "by_action" in data
        assert "by_resource" in data
        assert "active_users" in data

    def test_get_audit_stats_default_days(self, client, no_rate_limit):
        """默认统计 30 天"""
        headers = _auth_headers(client)
        resp = client.get("/api/v1/audit-logs/stats", headers=headers)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["period_days"] == 30


# ══════════════════════════════════════════════════════════════════════════════
# 四、AuditLog Model 测试
# ══════════════════════════════════════════════════════════════════════════════

class TestAuditLogModel:
    """AuditLog Model 测试"""

    def test_to_dict(self):
        from app.models.audit_log import AuditLog
        log = AuditLog(
            action='create', resource_type='project', resource_id=1,
            changes={'name': 'Test'}, ip_address='127.0.0.1',
        )
        d = log.to_dict()
        assert d['action'] == 'create'
        assert d['resource_type'] == 'project'
        assert d['resource_id'] == 1

    def test_repr(self):
        from app.models.audit_log import AuditLog
        log = AuditLog(action='delete', resource_type='test_case', resource_id=5)
        assert 'delete' in repr(log)
        assert 'test_case' in repr(log)