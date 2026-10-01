"""
FastAPI v2 测试共享基建

依赖父级 tests/conftest.py 的 `app`（FastAPI 应用实例，SQLite 临时库）与
`v2_client`（FastAPI TestClient，同一应用实例/数据库）fixtures。
"""

import pytest


@pytest.fixture()
def make_user(app):
    """创建测试用户工厂：make_user(username, password='Passw0rd!123', role='member')"""

    def _make(username: str, password: str = "Passw0rd!123", role: str = "member", **extra):
        from app.extensions import db
        from app.models.user import User
        from app.core.passwords import generate_password_hash as _gen

        user = User(
            username=username,
            email=extra.pop("email", f"{username}@test.local"),
            password_hash=_gen(password),
            role=role,
            is_active=True,
        )
        db.session.add(user)
        db.session.commit()
        return user.id

    return _make


@pytest.fixture()
def auth_headers(app):
    """为指定用户生成 Bearer 认证头：auth_headers(user_id)"""

    def _headers(user_id: int) -> dict:
        from app.core.jwt import create_access_token

        token = create_access_token(str(user_id))
        return {"Authorization": f"Bearer {token}"}

    return _headers


@pytest.fixture()
def login_user(v2_client):
    """走完整登录流程获取认证头（验证登录端点本身可用）"""

    def _login(username: str, password: str = "Passw0rd!123") -> dict:
        resp = v2_client.post(
            "/api/v1/auth/login",
            json={"username": username, "password": password},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        return {
            "Authorization": f"Bearer {data['data']['access_token'] if 'data' in data else data['access_token']}",
            "data": data,
        }

    return _login
