"""
限流中间件测试

通过 mock 控制限流服务行为，验证中间件的请求拦截和响应头逻辑。

TestingConfig 中 RATELIMIT_ENABLED=False，因此默认关闭限流。
需要测试限流行为时，临时启用 RATELIMIT_ENABLED 并 mock 底层函数。
"""
from app.core.runtime import get_config

import uuid
from unittest.mock import patch


def _auth_headers(client):
    username = f"rl_{uuid.uuid4().hex[:8]}"
    password = "Passw0rd!"
    email = f"{username}@example.com"
    client.post("/api/v1/auth/register", json={"username": username, "email": email, "password": password})
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    token = resp.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _mock_block_all(key, limit, **kw):
    return False


def _mock_allow_all(key, limit, **kw):
    return True


def _mock_retry_headers(key, limit, **kw):
    return {"Retry-After": "60", "X-RateLimit-Limit": str(limit)}


# ====================================================================
# 限流中间件行为测试
# ====================================================================

class TestRateLimitMiddleware:
    """限流中间件核心行为测试"""

    @patch("app.services.rate_limit_service.sliding_window_rate_limit", _mock_allow_all)
    def test_request_allowed_when_under_limit(self, client):
        """未触发限流时，请求正常返回"""
        headers = _auth_headers(client)
        resp = client.get("/api/v1/api-test/health", headers=headers)
        assert resp.status_code == 200

    def test_request_blocked_when_over_limit(self, client, app):
        """触发限流时，返回 429"""
        headers = _auth_headers(client)
        # 临时启用限流
        get_config()["RATELIMIT_ENABLED"] = True
        try:
            with patch("app.services.rate_limit_service.sliding_window_rate_limit", _mock_block_all), \
                 patch("app.services.rate_limit_service.get_rate_limit_headers", _mock_retry_headers):
                resp = client.get("/api/v1/api-test/health", headers=headers)
        finally:
            get_config()["RATELIMIT_ENABLED"] = False
        assert resp.status_code == 429
        data = resp.json()
        assert "message" in data

    def test_rate_limit_response_contains_retry_after(self, client, app):
        """限流响应包含 Retry-After 头"""
        headers = _auth_headers(client)
        get_config()["RATELIMIT_ENABLED"] = True
        try:
            with patch("app.services.rate_limit_service.sliding_window_rate_limit", _mock_block_all), \
                 patch("app.services.rate_limit_service.get_rate_limit_headers",
                       lambda k, l, **kw: {"Retry-After": "42", "X-RateLimit-Limit": str(l)}):
                resp = client.get("/api/v1/api-test/health", headers=headers)
        finally:
            get_config()["RATELIMIT_ENABLED"] = False
        assert resp.status_code == 429
        assert resp.headers.get("Retry-After") == "42"

    def test_health_endpoint_is_rate_limited(self, client, app):
        """健康检查端点也被限流中间件覆盖（当前实现）"""
        get_config()["RATELIMIT_ENABLED"] = True
        try:
            with patch("app.services.rate_limit_service.sliding_window_rate_limit", _mock_block_all), \
                 patch("app.services.rate_limit_service.get_rate_limit_headers", _mock_retry_headers):
                resp = client.get("/api/v1/web-test/health")
        finally:
            get_config()["RATELIMIT_ENABLED"] = False
        assert resp.status_code == 429

    def test_unauthenticated_user_uses_ip_key(self, client, app):
        """未认证用户使用 IP 作为限流键"""
        captured_keys = []

        def _capture(key, limit, **kw):
            captured_keys.append(key)
            return True

        get_config()["RATELIMIT_ENABLED"] = True
        try:
            with patch("app.services.rate_limit_service.sliding_window_rate_limit", _capture):
                client.get("/api/v1/web-test/health")
        finally:
            get_config()["RATELIMIT_ENABLED"] = False
        assert len(captured_keys) >= 1
        assert "rate_limit:ip:" in captured_keys[-1]

    def test_authenticated_user_uses_user_key(self, client, app):
        """认证用户使用 user_id 作为限流键"""
        headers = _auth_headers(client)
        captured_keys = []

        def _capture(key, limit, **kw):
            captured_keys.append(key)
            return True

        get_config()["RATELIMIT_ENABLED"] = True
        try:
            with patch("app.services.rate_limit_service.sliding_window_rate_limit", _capture):
                client.get("/api/v1/api-test/health", headers=headers)
        finally:
            get_config()["RATELIMIT_ENABLED"] = False
        assert any("rate_limit:user:" in k for k in captured_keys)


# ====================================================================
# 限流服务单元测试
# ====================================================================

class TestRateLimitService:
    """限流服务滑动窗口算法测试（mock Redis）"""

    def test_sliding_window_allows_under_limit(self, monkeypatch):
        from app.services.rate_limit_service import sliding_window_rate_limit

        class FakeRedis:
            def pipeline(self):
                return self
            def zremrangebyscore(self, *a): return self
            def zadd(self, *a, **k): return self
            def zcard(self, *a): return self
            def expire(self, *a): return self
            def execute(self):
                return [None, None, 5, None]  # 当前计数 5

        result = sliding_window_rate_limit("test:key", limit=100, redis_client=FakeRedis())
        assert result is True

    def test_sliding_window_blocks_over_limit(self):
        """计数超过限制时返回 False"""
        import time
        from app.services.rate_limit_service import sliding_window_rate_limit

        class FakeRedis:
            def pipeline(self):
                return self

            def zremrangebyscore(self, *a):
                return self

            def zadd(self, *a, **k):
                return self

            def zcard(self, *a):
                return 101

            def expire(self, *a):
                return self

            def execute(self):
                return [None, None, 101, None]

        now = time.time()
        allowed = sliding_window_rate_limit(
            "test:key", 100, window=60, redis_client=FakeRedis()
        )
        assert allowed is False
    def test_sliding_window_allows_when_redis_fails(self):
        from app.services.rate_limit_service import sliding_window_rate_limit

        class FailingRedis:
            def pipeline(self):
                raise ConnectionError("Redis down")

        result = sliding_window_rate_limit("test:key", limit=100, redis_client=FailingRedis())
        assert result is True  # 降级放行

    def test_get_user_rate_limit_default(self):
        from app.services.rate_limit_service import get_user_rate_limit
        assert get_user_rate_limit(1) == 100
        assert get_user_rate_limit(1, is_api_token=True) == 1000

    def test_get_rate_limit_headers(self):
        from app.services.rate_limit_service import get_rate_limit_headers
        headers = get_rate_limit_headers("test:key", limit=100)
        assert "X-RateLimit-Limit" in headers
        assert "X-RateLimit-Remaining" in headers
        assert "Retry-After" in headers
