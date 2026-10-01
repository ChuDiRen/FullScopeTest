"""
pytest 共享 fixtures（零 Flask）

- `app` fixture：初始化运行时（testing 配置 + SQLite 临时库），返回 FastAPI 应用实例
- `client` / `v2_client`：原生 httpx TestClient
"""

import os
import sys
import tempfile

import pytest

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

_db_fd, _db_path = tempfile.mkstemp(prefix="fullscopetest_test_", suffix=".db")
os.close(_db_fd)

os.environ.setdefault("TEST_DATABASE_URL", f"sqlite:///{_db_path}")

_fastapi_app = None


def _get_test_fastapi_app():
    global _fastapi_app
    if _fastapi_app is None:
        from app.fastapi_app import create_fastapi_app

        _fastapi_app = create_fastapi_app("testing")
    return _fastapi_app


@pytest.fixture(scope="session")
def app():
    os.environ.setdefault("APP_ENV", "testing")
    os.environ["CELERY_ENABLE"] = "false"
    # 禁用登录锁定，防止测试间状态泄漏
    os.environ["MAX_LOGIN_FAILURES"] = "999999"
    os.environ["LOCKOUT_DURATION_SECONDS"] = "1"

    from app.core.runtime import init_runtime
    from app.extensions import db

    init_runtime("testing")

    import app.models  # noqa: F401 — 确保所有模型注册
    db.create_all()

    yield _get_test_fastapi_app()

    from app.extensions import db as _db

    _db.session.remove()
    _db.drop_all()
    _db.engine.dispose()

    try:
        os.remove(_db_path)
    except FileNotFoundError:
        pass


@pytest.fixture(autouse=True)
def _isolate_tests(app):
    """每个测试前清除内存状态，测试后回滚数据库事务"""
    import app.services.password_policy as _pp

    _pp._login_failure_store.clear()

    yield

    from app.extensions import db

    db.session.rollback()
    db.session.remove()
    _pp._login_failure_store.clear()


@pytest.fixture()
def no_rate_limit(monkeypatch):
    """按需禁用限流：全局 ASGI 中间件 + 登录/注册路由级限流一并禁用。

    auth.py 的路由级限流走自己的 _usable_redis 客户端（带 30s 探测缓存），
    只 stub rate_limit_service 盖不住它——Redis 真实可用时连续登录会被 429。
    """
    monkeypatch.setattr(
        "app.services.rate_limit_service.sliding_window_rate_limit",
        lambda key, limit, **kw: True,
    )
    monkeypatch.setattr(
        "app.api.v2.v1.auth._usable_redis",
        lambda: None,
    )


@pytest.fixture()
def client(app):
    """FastAPI TestClient（httpx 接口：resp.json() / resp.text / resp.content）"""
    from fastapi.testclient import TestClient

    return TestClient(app)


@pytest.fixture()
def v2_client(app):
    """FastAPI TestClient（tests/api_v2/ 套件使用，共享同一数据库）"""
    from fastapi.testclient import TestClient

    return TestClient(app)
