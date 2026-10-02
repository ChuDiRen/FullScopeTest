"""
v1 auth 平迁路由测试（app/api/routes/auth.py）

覆盖：
1. 未登录访问受保护端点（GET /api/v1/auth/me）→ 401
2. 注册 → 登录 → 拿 access_token 访问 /me 成功（含 httpOnly Cookie 下发）
3. 错误密码登录 401；连续 5 次后触发账户锁定 423（或 IP 限流 429）
4. refresh 端点用 access token 调用 → 401（token 类型校验）
5. logout 后原 token 再访问受保护端点 → 401（token 黑名单生效）
另附：注册校验规则、资料修改、改密、忘记/重置密码、SSO 端点的行为保持用例。

环境说明：
- 测试环境 REDIS_URL 未设置时，本机 6379 若无可用 Redis（未启动/需认证），
  服务层限流/黑名单按既有设计降级（fail-open / 不写黑名单）。
- 为避免对不可用 Redis 反复建连（Windows 上偶发 connect 长阻塞），
  离线时向 token_blacklist 注入确定性失败客户端，用例内不再发起真实连接。
"""

import os
import threading

import pytest
from sqlalchemy import update

LOGIN_URL = "/api/v1/auth/login"
REGISTER_URL = "/api/v1/auth/register"


# ---------------------------------------------------------------------------
# Redis 可用性（带时间预算，只探测一次）
# ---------------------------------------------------------------------------

_REDIS_ALIVE = None


def _redis_alive() -> bool:
    """探测本机 Redis 是否真正可用（PING 成功）；结果缓存"""
    global _REDIS_ALIVE
    if _REDIS_ALIVE is None:
        result = {}

        def _check():
            try:
                import redis as redis_lib

                url = os.environ.get("REDIS_URL") or "redis://localhost:6379/0"
                r = redis_lib.from_url(
                    url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2
                )
                result["ok"] = bool(r.ping())
            except Exception:
                result["ok"] = False

        t = threading.Thread(target=_check, daemon=True)
        t.start()
        t.join(5)
        _REDIS_ALIVE = bool(result.get("ok"))
    return _REDIS_ALIVE


@pytest.fixture(scope="session", autouse=True)
def _offline_redis_guard():
    """
    Redis 不可用（未启动/需认证）时，向 token_blacklist 注入确定性失败客户端：
    - is_token_blacklisted / is_token_version_valid 走异常分支 → False / 0（放行），
      与真实 Redis 故障时的降级行为一致
    - 杜绝用例内反复真实建连，保证测试确定性
    """
    import app.services.token_blacklist as tb

    if _redis_alive():
        yield
        return

    class _BrokenRedis:
        def __getattr__(self, name):
            def _raise(*args, **kwargs):
                raise RuntimeError("redis unavailable in test env")

            return _raise

    original = tb._redis_client
    tb._redis_client = _BrokenRedis()
    yield
    tb._redis_client = original


@pytest.fixture(autouse=True)
def _flush_rate_limit_state():
    """Redis 可用时，每个用例前清空限流相关键，避免 60s 滑动窗口跨用例串扰"""
    if _redis_alive():
        try:
            from app.services.rate_limit_service import _get_redis

            r = _get_redis()
            if r is not None:
                keys = []
                for pattern in (
                    "rate_limit:login_ip:*",
                    "rate_limit:register_ip:*",
                    "rate_limit:forgot_password_ip:*",
                    "rate_limit:reset_password_ip:*",
                    "rate_limit:ldap_login_ip:*",
                    "rate_limit:ip:*",
                    "token_blacklist:*",
                    "oidc_state:*",
                ):
                    keys.extend(r.scan_iter(pattern))
                if keys:
                    r.delete(*keys)
        except Exception:
            pass
    yield


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _register(client, username, password="Passw0rd!123", **extra):
    payload = {
        "username": username,
        "email": f"{username}@test.local",
        "password": password,
    }
    payload.update(extra)
    return client.post(REGISTER_URL, json=payload)


# ---------------------------------------------------------------------------
# 必覆盖场景 1：未登录访问受保护端点
# ---------------------------------------------------------------------------

def test_me_requires_auth(v2_client):
    resp = v2_client.get("/api/v1/auth/me")
    assert resp.status_code == 401
    assert resp.json()["code"] == 401


# ---------------------------------------------------------------------------
# 必覆盖场景 2：注册 → 登录 → access_token 访问 /me
# ---------------------------------------------------------------------------

def test_register_login_me_flow(v2_client):
    # 注册
    resp = _register(v2_client, "v1auth_alice")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["code"] == 200
    assert body["message"] == "注册成功"
    assert body["data"]["username"] == "v1auth_alice"
    assert body["data"]["user_id"] > 0

    # 登录
    resp = v2_client.post(
        LOGIN_URL, json={"username": "v1auth_alice", "password": "Passw0rd!123"}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["access_token"]
    assert data["refresh_token"]
    assert data["user"]["username"] == "v1auth_alice"
    # 登录成功同时下发 httpOnly Cookie（与 v1 前端行为一致）
    assert "access_token_cookie" in resp.headers.get("set-cookie", "")

    # 拿 access_token 访问 me
    me = v2_client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {data['access_token']}"}
    )
    assert me.status_code == 200, me.text
    assert me.json()["data"]["username"] == "v1auth_alice"


def test_me_with_factory_user(v2_client, make_user, auth_headers):
    """make_user 直建用户 + auth_headers 访问受保护端点"""
    uid = make_user("v1auth_me_direct")
    resp = v2_client.get("/api/v1/auth/me", headers=auth_headers(uid))
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["id"] == uid


# ---------------------------------------------------------------------------
# 必覆盖场景 3：错误密码 401，连续 5 次后 423/429
# ---------------------------------------------------------------------------

def test_wrong_password_then_lockout_or_rate_limit(v2_client, make_user, monkeypatch):
    # 测试 conftest 为隔离性把 MAX_LOGIN_FAILURES 调到了 999999；
    # 这里运行时改回策略值 5，验证真实的锁定路径（Redis 离线时限流 fail-open，
    # 6 次请求命中账户锁定 423；Redis 在线时第 6 次也可能被 IP 限流 429 拦下）。
    import app.services.password_policy as pp

    monkeypatch.setattr(pp, "MAX_LOGIN_FAILURES", 5)
    monkeypatch.setattr(pp, "LOCKOUT_DURATION", 900)

    uid = make_user("v1auth_lock")
    # Redis 真实可用后：测试库重建时 user_id 会复用，而上一次运行的锁定
    # 状态仍留在共享 Redis（TTL 30 分钟）——开测前先清自己的键
    pp.reset_login_failures(uid)
    for i in range(5):
        resp = v2_client.post(
            LOGIN_URL, json={"username": "v1auth_lock", "password": "WrongPass!1"}
        )
        assert resp.status_code == 401, (i, resp.text)
        assert resp.json()["message"] == "用户名或密码错误"

    # 第 6 次：账户锁定（423）或 IP 限流（429）至少命中其一
    resp = v2_client.post(
        LOGIN_URL, json={"username": "v1auth_lock", "password": "WrongPass!1"}
    )
    assert resp.status_code in (423, 429), resp.text

    # 清理锁定键，避免污染（可能复用同 user_id 的）后续测试运行
    pp.reset_login_failures(uid)


# ---------------------------------------------------------------------------
# 必覆盖场景 4：refresh 拒绝 access token（类型校验）
# ---------------------------------------------------------------------------

def test_refresh_rejects_access_token(v2_client, make_user, auth_headers):
    uid = make_user("v1auth_refresh")
    resp = v2_client.post("/api/v1/auth/refresh", headers=auth_headers(uid))
    assert resp.status_code == 401, resp.text


def test_refresh_accepts_refresh_token(v2_client, app, make_user):
    """正向对照：refresh token 刷新成功并返回新 access_token"""
    uid = make_user("v1auth_refresh_ok")
    from app.core.jwt import create_refresh_token

    rtoken = create_refresh_token(str(uid))

    resp = v2_client.post(
        "/api/v1/auth/refresh", headers={"Authorization": f"Bearer {rtoken}"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["access_token"]


# ---------------------------------------------------------------------------
# 必覆盖场景 5：logout 后原 token 失效（黑名单）
# ---------------------------------------------------------------------------

def test_logout_blacklists_token(v2_client, make_user, auth_headers):
    if not _redis_alive():
        pytest.xfail(
            "测试环境 Redis 不可用：blacklist_token 无法写入黑名单，"
            "登出后 token 仍被放行（与 v1 的降级策略一致）"
        )

    uid = make_user("v1auth_logout")
    headers = auth_headers(uid)

    # 登出前可用
    me = v2_client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200, me.text

    # 登出
    resp = v2_client.post("/api/v1/auth/logout", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["message"] == "已成功登出"

    # 原 token 再访问受保护端点 → 401（黑名单生效）
    me2 = v2_client.get("/api/v1/auth/me", headers=headers)
    assert me2.status_code == 401, me2.text


# ---------------------------------------------------------------------------
# 注册校验规则保持
# ---------------------------------------------------------------------------

def test_register_rejects_weak_password(v2_client):
    resp = _register(v2_client, "v1auth_weak", password="weakpass")
    assert resp.status_code == 400
    assert "密码" in resp.json()["message"]


def test_register_rejects_bad_username_length(v2_client):
    resp = _register(v2_client, "ab")
    assert resp.status_code == 400
    assert resp.json()["message"] == "用户名长度应为 3-50 个字符"


def test_register_rejects_duplicate_username(v2_client):
    first = _register(v2_client, "v1auth_dup")
    assert first.status_code == 200, first.text

    second = _register(v2_client, "v1auth_dup")
    assert second.status_code == 400
    assert second.json()["message"] == "用户名已被使用"


def test_register_missing_fields_returns_400(v2_client):
    resp = v2_client.post(REGISTER_URL, json={"username": "v1auth_nofield"})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# 资料修改 / 密码管理
# ---------------------------------------------------------------------------

def test_update_profile(v2_client, make_user, auth_headers):
    uid = make_user("v1auth_profile")
    resp = v2_client.put(
        "/api/v1/auth/me",
        headers=auth_headers(uid),
        json={"username": "v1auth_profile2"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["username"] == "v1auth_profile2"


def test_update_profile_rejects_duplicate_username(v2_client, make_user, auth_headers):
    uid = make_user("v1auth_p1")
    make_user("v1auth_p2")
    resp = v2_client.put(
        "/api/v1/auth/me",
        headers=auth_headers(uid),
        json={"username": "v1auth_p2"},
    )
    assert resp.status_code == 400
    assert resp.json()["message"] == "用户名已被使用"


def test_change_password_wrong_old(v2_client, make_user, auth_headers):
    uid = make_user("v1auth_chpw")
    resp = v2_client.put(
        "/api/v1/auth/password",
        headers=auth_headers(uid),
        json={"old_password": "WrongOld!1", "new_password": "NewPass!234"},
    )
    assert resp.status_code == 400
    assert resp.json()["message"] == "原密码错误"


def test_change_password_then_login_with_new(v2_client, make_user, auth_headers):
    uid = make_user("v1auth_chpw_ok")
    resp = v2_client.put(
        "/api/v1/auth/password",
        headers=auth_headers(uid),
        json={"old_password": "Passw0rd!123", "new_password": "NewPass!234"},
    )
    assert resp.status_code == 200, resp.text

    login = v2_client.post(
        LOGIN_URL, json={"username": "v1auth_chpw_ok", "password": "NewPass!234"}
    )
    assert login.status_code == 200, login.text


def test_avatar_requires_file(v2_client, make_user, auth_headers):
    uid = make_user("v1auth_avatar")
    resp = v2_client.post("/api/v1/auth/me/avatar", headers=auth_headers(uid))
    assert resp.status_code == 400
    assert resp.json()["message"] == "未找到文件"


# ---------------------------------------------------------------------------
# 忘记密码 / 重置密码
# ---------------------------------------------------------------------------

def test_forgot_password_unknown_email_no_enumeration(v2_client):
    resp = v2_client.post(
        "/api/v1/auth/forgot-password", json={"email": "nobody-v1auth@test.local"}
    )
    assert resp.status_code == 200
    assert resp.json()["message"] == "如果该邮箱已注册，重置链接已发送"


def test_reset_password_invalid_token(v2_client):
    resp = v2_client.post(
        "/api/v1/auth/reset-password",
        json={"token": "bogus-token", "new_password": "NewPass!234"},
    )
    assert resp.status_code == 400
    assert resp.json()["message"] == "重置 Token 无效或已过期"


# ---------------------------------------------------------------------------
# SSO 端点
# ---------------------------------------------------------------------------

def test_sso_providers_public(v2_client):
    resp = v2_client.get("/api/v1/auth/sso/providers")
    assert resp.status_code == 200
    assert isinstance(resp.json()["data"], list)


def test_oidc_login_not_configured(v2_client, monkeypatch):
    from app.services.sso_service import oidc_provider

    monkeypatch.setattr(oidc_provider, "issuer_url", "")
    resp = v2_client.get("/api/v1/auth/sso/oidc/login")
    assert resp.status_code == 400
    assert resp.json()["message"] == "OIDC 未配置"


def test_ldap_login_not_configured(v2_client, monkeypatch):
    from app.services.sso_service import ldap_provider

    monkeypatch.setattr(ldap_provider, "is_configured", lambda: False)
    resp = v2_client.post(
        "/api/v1/auth/sso/ldap/login",
        json={"username": "ldapuser", "password": "Passw0rd!123"},
    )
    assert resp.status_code == 400
    assert resp.json()["message"] == "LDAP 未配置"


def test_sso_config_requires_admin(v2_client, make_user, auth_headers):
    uid = make_user("v1auth_member")
    resp = v2_client.get("/api/v1/auth/sso/config", headers=auth_headers(uid))
    assert resp.status_code == 403


def test_sso_config_admin(v2_client, make_user, auth_headers):
    uid = make_user("v1auth_admin", role="admin")
    resp = v2_client.get("/api/v1/auth/sso/config", headers=auth_headers(uid))
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert set(data.keys()) == {"oidc", "ldap"}
    assert "configured" in data["oidc"]
    assert "configured" in data["ldap"]
