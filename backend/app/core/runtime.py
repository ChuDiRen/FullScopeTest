"""
运行时核心（零 Flask）

替代原 Flask create_app/current_app/g 的职责：
- get_config(): 全局配置（来自 app/config.py 的类层级，dict 形状）
- ctx: 请求级上下文（request_id / organization_id，contextvar 实现）
- init_runtime(): 初始化数据库引擎 / Celery 配置 / 结构化日志
- session_teardown(): 请求/任务结束时提交-回滚-释放 scoped session
- ensure_runtime(): 幂等初始化入口（FastAPI lifespan 与 Celery worker 共用）
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

from .logging import configure_structlog, get_logger
from sqlalchemy import select
from ..extensions import db
from sqlalchemy import func

# 加载 backend/.env（override=False：外部环境变量优先）。
# runtime 是所有入口（FastAPI lifespan / Celery / init_db / CLI）的公共路径，
# 在此加载一次即可让 REDIS_URL 等本地配置对全部进程生效。
try:
    from dotenv import load_dotenv

    load_dotenv(dotenv_path=Path(__file__).resolve().parents[2] / ".env", override=False)
except ImportError:  # pragma: no cover
    pass

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# 请求上下文（替代 flask.g）
# ---------------------------------------------------------------------------

_ctx_request_id: ContextVar[str] = ContextVar("request_id", default="")
_ctx_org_id: ContextVar[Optional[int]] = ContextVar("organization_id", default=None)

ctx = SimpleNamespace(
    get_request_id=lambda: _ctx_request_id.get(),
    set_request_id=lambda v: _ctx_request_id.set(v or ""),
    get_organization_id=lambda: _ctx_org_id.get(),
    set_organization_id=lambda v: _ctx_org_id.set(v),
)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

_config_cache: Optional[Dict[str, Any]] = None
_config_name: Optional[str] = None


def _build_config(config_name: str) -> Dict[str, Any]:
    """从 app/config.py 的类层级构建配置字典（该模块零 Flask 依赖）"""
    from ..config import config as config_classes

    cls = config_classes[config_name]
    cfg: Dict[str, Any] = {}
    for klass in reversed(cls.__mro__):
        for key, value in vars(klass).items():
            if key.isupper() and not key.startswith("_"):
                cfg[key] = value
    cfg["CONFIG_NAME"] = config_name

    # 生产环境密钥强制校验（原 _validate_production_secrets 逻辑）
    if config_name == "production":
        missing = [k for k in ("SECRET_KEY", "JWT_SECRET_KEY") if not os.environ.get(k)]
        if missing:
            raise RuntimeError(
                f"生产环境缺少必需配置: {', '.join(missing)}。请在环境变量中设置这些值。"
            )
        # 生产 CORS_ORIGINS 必须显式配置
        if not cfg.get("CORS_ORIGINS"):
            raise RuntimeError("生产环境必须显式配置 CORS_ORIGINS 环境变量")
    return cfg


def get_config() -> Dict[str, Any]:
    """全局配置字典（首次访问时按 APP_ENV 初始化）"""
    global _config_cache, _config_name
    if _config_cache is None:
        env = os.environ.get("APP_ENV") or "development"
        _config_name = env
        _config_cache = _build_config(env)
    return _config_cache


def reset_config() -> None:
    """测试用：清空配置缓存（下次 get_config 重新构建）"""
    global _config_cache, _config_name
    _config_cache = None
    _config_name = None


# ---------------------------------------------------------------------------
# 运行时初始化（数据库 / 日志 / 插件 / 种子）
# ---------------------------------------------------------------------------

_runtime_lock = threading.Lock()
_runtime_ready = False


def _init_database(cfg: Dict[str, Any]) -> None:
    from ..extensions import db
    if db.engine is not None:
        return  # 已初始化（测试里可能先 init 过）

    uri = cfg.get("SQLALCHEMY_DATABASE_URI") or os.environ.get(
        "DATABASE_URL", "sqlite:///fullscopetest.db"
    )
    engine_options = dict(cfg.get("SQLALCHEMY_ENGINE_OPTIONS") or {})
    if uri.startswith("sqlite"):
        from sqlalchemy.pool import NullPool

        engine_options.setdefault("poolclass", NullPool)
    db.init(uri, **engine_options)


def _seed_defaults() -> None:
    """启动种子逻辑（原 create_app 内的 _create_init_admin / AUTO_SEED，幂等）"""
    from ..extensions import db
    init_username = os.environ.get("INIT_ADMIN_USERNAME", "").strip()
    init_email = os.environ.get("INIT_ADMIN_EMAIL", "").strip()
    init_password = os.environ.get("INIT_ADMIN_PASSWORD", "").strip()
    if all([init_username, init_email, init_password]):
        from ..models.user import User
        from .passwords import generate_password_hash

        if db.session.scalar(select(func.count()).select_from(select(User).subquery()))== 0:
            db.session.add(
                User(
                    username=init_username,
                    email=init_email,
                    password_hash=generate_password_hash(init_password),
                    role="admin",
                    is_active=True)
            )
            db.session.commit()
            logger.info(f"Initial admin created: {init_username}")

    # 系统角色种子（幂等）
    try:
        from sqlalchemy import inspect as sa_inspect

        inspector = sa_inspect(db.engine)
        if inspector.has_table("roles"):
            from ..services.permission_service import seed_system_roles

            seed_system_roles()
    except Exception as exc:
        logger.warning("Failed to seed system roles", error=str(exc))


def _init_plugins() -> None:
    try:
        from ..plugins.registry import plugin_registry

        plugin_registry.auto_discover()
        try:
            from ..plugins.custom.slack_notify import SlackNotifyPlugin

            plugin_registry.register(SlackNotifyPlugin())
        except Exception:
            pass
        # 插件以 namespace.config 方式读取配置（替代 Flask app 对象）
        plugin_registry.init_all(SimpleNamespace(config=get_config()))
    except Exception as exc:
        logger.warning("插件系统初始化失败", error=str(exc))


def init_runtime(config_name: Optional[str] = None) -> Dict[str, Any]:
    """
    初始化运行时（幂等，进程级）。

    FastAPI lifespan 与 Celery worker 启动时各调用一次。
    """
    global _runtime_ready
    with _runtime_lock:
        if config_name:
            os.environ["APP_ENV"] = config_name
            reset_config()
        cfg = get_config()
        if _runtime_ready:
            return cfg

        log_level = os.environ.get("LOG_LEVEL", "INFO")
        configure_structlog(
            log_level=log_level,
            json_format=cfg.get("CONFIG_NAME") in ("production", "staging"),
        )
        _init_database(cfg)
        # 建表（checkfirst 幂等，只补缺失表）：全新库可直接启动/初始化；
        # 生产表结构由 Alembic 管理，create_all 不会改动已有表
        import app.models  # noqa: F401  确保全部模型注册到 metadata
        from ..extensions import db as _db
        _db.create_all()
        _seed_defaults()
        _init_plugins()
        _runtime_ready = True
        logger.info("Runtime initialized", config=cfg.get("CONFIG_NAME"))
        return cfg


def ensure_runtime() -> Dict[str, Any]:
    """给 Celery 任务等非 HTTP 入口用：保证运行时已初始化"""
    return init_runtime()


def session_teardown(exc: Optional[BaseException] = None) -> None:
    """
    请求/任务结束时释放 scoped session：
    异常回滚，正常提交，最后 remove。
    """
    from ..extensions import db
    try:
        if exc is None:
            db.session.commit()
        else:
            db.session.rollback()
    except Exception:
        db.session.rollback()
    finally:
        try:
            db.session.remove()
        except Exception:
            pass


@contextmanager
def task_session_scope():
    """后台线程（Celery worker / APScheduler）的独立数据库会话作用域。

    db.session 的 scopefunc 基于 _session_scope ContextVar；ASGI 中间件每请求
    set 新令牌，但后台线程池拿到的都是默认值 None——threads 池下并发任务会
    共享同一个 scoped session，互相 commit/rollback 踩踏（IllegalStateChangeError）
    且写入丢失。与中间件同款：执行前放置新令牌，结束后复位。
    """
    from ..database import _session_scope

    token = _session_scope.set(object())
    try:
        yield
    finally:
        _session_scope.reset(token)


def shutdown_runtime() -> None:
    """进程退出前释放引擎连接池"""
    global _runtime_ready
    try:
        from ..extensions import db
        if db.engine is not None:
            db.engine.dispose()
    except Exception:
        pass
    _runtime_ready = False
