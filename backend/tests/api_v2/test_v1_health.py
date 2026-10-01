"""健康检查端点测试（零 Flask 后端）"""


def test_liveness(v2_client):
    r = v2_client.get("/health/live")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert r.json()["service"] == "fullscopetest"


def test_health_ready_sqlite_ok(v2_client):
    r = v2_client.get("/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["checks"]["database"]["status"] == "ok"


def test_health_alias(v2_client):
    r = v2_client.get("/health")
    assert r.status_code == 200
    assert "checks" in r.json()
