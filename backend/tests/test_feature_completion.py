"""
功能补全测试：数据工厂 / Flaky 检测 / 报告模板 / 定时报告 / 用例模板

覆盖前端页面对接的 5 组 API 的真实链路（DB/服务），AI 与外呼全 mock 不涉及。
"""
import uuid

import pytest

from app.extensions import db
from app.models.report_schedule import ReportSchedule
from app.models.report_template import ReportTemplate
from app.models.test_case_template import TestCaseTemplate


def _auth_headers(client, prefix="feat"):
    uid = uuid.uuid4().hex[:8]
    username = f"{prefix}_{uid}"
    password = "Passw0rd!"
    client.post("/api/v1/auth/register", json={
        "username": username, "email": f"{username}@example.com", "password": password,
    })
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {resp.json()['data']['access_token']}"}


@pytest.fixture(autouse=True)
def _allow_localhost_ssrf(monkeypatch):
    monkeypatch.setenv("SSRF_ALLOWLIST_HOSTS", "127.0.0.1")


# ══════════════════════════════════════════════════════════════════════════════
# 数据工厂
# ══════════════════════════════════════════════════════════════════════════════

def test_data_factory_schema_generate(app, client, no_rate_limit):
    headers = _auth_headers(client, "df")
    resp = client.post("/api/v1/ai/data-factory/generate", headers=headers, json={
        "schema": [
            {"name": "username", "type": "string", "rule": "usr"},
            {"name": "email", "type": "email", "rule": ""},
            {"name": "age", "type": "number", "rule": "18-65"},
        ],
        "count": 5,
    })
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 200
    rows = body["data"]
    assert len(rows) == 5
    assert all(r["username"].startswith("usr_") for r in rows)
    assert all("@" in r["email"] for r in rows)
    assert all(18 <= r["age"] <= 65 for r in rows)


def test_data_factory_template_generate(app, client, no_rate_limit):
    headers = _auth_headers(client, "df")
    resp = client.post("/api/v1/ai/data-factory/generate", headers=headers, json={
        "template": "user", "count": 3,
    })
    assert resp.status_code == 200
    assert resp.json()["code"] == 200
    assert len(resp.json()["data"]) == 3


def test_data_factory_empty_schema_400(app, client, no_rate_limit):
    headers = _auth_headers(client, "df")
    resp = client.post("/api/v1/ai/data-factory/generate", headers=headers, json={
        "schema": [], "count": 5,
    })
    assert resp.json()["code"] == 400


# ══════════════════════════════════════════════════════════════════════════════
# Flaky 检测
# ══════════════════════════════════════════════════════════════════════════════

def test_flaky_analyze_contract(app, client, no_rate_limit):
    headers = _auth_headers(client, "fk")
    resp = client.get("/api/v1/flaky-detector/analyze", headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 200
    assert isinstance(body["data"], list)
    for item in body["data"]:
        assert {"case_id", "case_name", "stability_score", "total_runs",
                "flaky_count", "pattern", "suggestion"} <= set(item.keys())


def test_flaky_report(app, client, no_rate_limit):
    headers = _auth_headers(client, "fk")
    resp = client.get("/api/v1/flaky-detector/report", headers=headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert {"total_analyzed", "suspected_flaky", "confirmed_flaky", "top_flaky"} <= set(data.keys())


# ══════════════════════════════════════════════════════════════════════════════
# 报告模板
# ══════════════════════════════════════════════════════════════════════════════

def test_report_template_crud(app, client, no_rate_limit):
    headers = _auth_headers(client, "rt")
    resp = client.post("/api/v1/report-templates", headers=headers, json={
        "name": "我的模板",
        "modules": [
            {"id": "summary", "name": "执行摘要", "enabled": True, "order": 1},
            {"id": "pass_rate", "name": "通过率", "enabled": True, "order": 2},
            {"id": "bogus_module", "name": "x", "enabled": True, "order": 3},
        ],
        "theme": "dark",
    })
    assert resp.status_code == 200
    body = resp.json()
    # 白名单清洗：非法模块被拒
    assert body["code"] == 400, body

    # 合法创建
    resp = client.post("/api/v1/report-templates", headers=headers, json={
        "name": "我的模板",
        "modules": [{"id": "summary", "name": "执行摘要", "enabled": True, "order": 1}],
        "theme": "dark",
    })
    body = resp.json()
    assert body["code"] == 200
    tid = body["data"]["id"]

    # 属主隔离：另一个用户看不到
    other = _auth_headers(client, "rt2")
    resp = client.get("/api/v1/report-templates", headers=other)
    assert all(t["id"] != tid for t in resp.json()["data"])
    resp = client.put(f"/api/v1/report-templates/{tid}", headers=other, json={"name": "x"})
    assert resp.json()["code"] == 404

    # 更新
    resp = client.put(f"/api/v1/report-templates/{tid}", headers=headers, json={
        "name": "改名模板", "theme": "light",
    })
    assert resp.json()["data"]["name"] == "改名模板"

    # 删除
    resp = client.delete(f"/api/v1/report-templates/{tid}", headers=headers)
    assert resp.json()["code"] == 200


# ══════════════════════════════════════════════════════════════════════════════
# 定时报告
# ══════════════════════════════════════════════════════════════════════════════

def test_report_schedule_crud_and_run(app, client, no_rate_limit, monkeypatch):
    ran = {}
    monkeypatch.setattr("app.scheduler.add_or_update_report_job",
                        lambda s: ran.setdefault("added", s.id))
    monkeypatch.setattr("app.scheduler.remove_report_job",
                        lambda sid: ran.setdefault("removed", sid))
    monkeypatch.setattr("app.scheduler.execute_report_schedule",
                        lambda sid: ran.setdefault("executed", sid))

    headers = _auth_headers(client, "rs")
    resp = client.post("/api/v1/report-schedules", headers=headers, json={
        "name": "每日报告", "frequency": "daily",
        "recipients": ["a@b.com"], "webhook_url": "https://hooks.invalid/xyz",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 200
    assert ran.get("added") == body["data"]["id"]
    sid = body["data"]["id"]

    # 非法频率
    resp = client.post("/api/v1/report-schedules", headers=headers, json={
        "name": "x", "frequency": "hourly",
    })
    assert resp.json()["code"] == 400

    # 停用触发 remove
    resp = client.put(f"/api/v1/report-schedules/{sid}", headers=headers, json={"is_active": False})
    assert resp.json()["data"]["is_active"] is False
    assert ran.get("removed") == sid

    # 立即执行（execute 被 mock，验证调度挂接）
    resp = client.post(f"/api/v1/report-schedules/{sid}/run", headers=headers)
    assert resp.json()["code"] == 200
    assert ran.get("executed") == sid

    resp = client.delete(f"/api/v1/report-schedules/{sid}", headers=headers)
    assert resp.json()["code"] == 200


def test_report_schedule_execution_real(app, client, no_rate_limit):
    """execute_report_schedule 真实执行：聚合 TestRun 统计并落库"""
    from app.scheduler import execute_report_schedule

    headers = _auth_headers(client, "rse")
    resp = client.post("/api/v1/report-schedules", headers=headers, json={
        "name": "周报", "frequency": "weekly",
    })
    sid = resp.json()["data"]["id"]

    execute_report_schedule(sid)

    schedule = db.session.get(ReportSchedule, sid)
    assert schedule.last_run_at is not None
    assert schedule.last_result is not None
    assert "total_runs" in schedule.last_result
    assert "pass_rate" in schedule.last_result


# ══════════════════════════════════════════════════════════════════════════════
# 用例模板
# ══════════════════════════════════════════════════════════════════════════════

def test_case_templates_builtin_seeded(app, client, no_rate_limit):
    headers = _auth_headers(client, "ct")
    resp = client.get("/api/v1/test-case-templates", headers=headers)
    assert resp.status_code == 200
    data = resp.json()["data"]
    builtins = [t for t in data if t["is_builtin"]]
    assert len(builtins) >= 5
    assert any(t["name"] == "Auth - Login" for t in builtins)


def test_case_template_crud_and_builtin_immutable(app, client, no_rate_limit):
    headers = _auth_headers(client, "ct")
    resp = client.post("/api/v1/test-case-templates", headers=headers, json={
        "name": "我的下单模板", "description": "自定义", "category": "业务",
        "method": "POST", "endpoint": "{{base_url}}/api/order",
        "headers": '{"Content-Type": "application/json"}',
        "body": '{"sku": "{{sku}}"}', "assertions": "status=201",
    })
    assert resp.status_code == 200
    tid = resp.json()["data"]["id"]
    assert resp.json()["data"]["is_builtin"] is False

    # 内置模板不可改/删
    builtin = db.session.scalar(
        select(TestCaseTemplate).filter_by(user_id=None).limit(1)
    )
    assert builtin is not None
    resp = client.put(f"/api/v1/test-case-templates/{builtin.id}", headers=headers, json={"name": "x"})
    assert resp.json()["code"] == 404
    resp = client.delete(f"/api/v1/test-case-templates/{builtin.id}", headers=headers)
    assert resp.json()["code"] == 404

    # 自己的可改可删
    resp = client.put(f"/api/v1/test-case-templates/{tid}", headers=headers, json={"name": "改名"})
    assert resp.json()["data"]["name"] == "改名"
    resp = client.delete(f"/api/v1/test-case-templates/{tid}", headers=headers)
    assert resp.json()["code"] == 200


from sqlalchemy import select  # noqa: E402
