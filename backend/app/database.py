"""
数据库层（SQLAlchemy 2.0，纯声明式）

- Base: 全部模型的声明式基类（表名由各模型显式 __tablename__ 声明）
- db: 引擎与请求作用域会话（scoped_session）
- paginate(): Select 语句分页助手

会话作用域：
- scopefunc 基于 ContextVar；FastAPI 中间件每请求放置新令牌，
  同步端点（线程池拷贝上下文）与异步端点共享同一会话；
  Celery 任务 / APScheduler 线程用默认全局作用域。
"""

import os
from contextvars import ContextVar
from typing import Any, List, NamedTuple

from sqlalchemy import create_engine, func, select, Select
from sqlalchemy.orm import DeclarativeBase, scoped_session, sessionmaker

# 请求作用域令牌：每请求由 DbSessionMiddleware set 一个新对象，
# scopefunc 返回它作为 scoped_session 的注册表键
_session_scope: ContextVar = ContextVar("db_session_scope", default=None)


def _scopefunc():
    return _session_scope.get()


class Base(DeclarativeBase):
    """全部模型的声明式基类"""
    pass


class Page(NamedTuple):
    """分页结果"""
    items: List[Any]
    total: int
    page: int
    per_page: int
    pages: int


def paginate(stmt: Select, page: int = 1, per_page: int = 20) -> Page:
    """对 Select 语句分页；越界页返回空 items"""
    page = max(int(page or 1), 1)
    per_page = max(int(per_page or 20), 1)
    total = db.session.scalar(
        select(func.count()).select_from(stmt.order_by(None).subquery())
    )
    items = list(
        db.session.scalars(stmt.limit(per_page).offset((page - 1) * per_page))
    )
    pages = (total + per_page - 1) // per_page
    return Page(items=items, total=total, page=page, per_page=per_page, pages=pages)


class Database:
    """引擎与会话管理（每进程 init 一次）"""

    def __init__(self):
        self._engine = None
        self._session = None

    def init(self, database_url: str = None, **engine_options):
        """创建引擎与会话工厂。SQLite 自动使用 NullPool（避免 database is locked）。"""
        if database_url is None:
            database_url = os.environ.get('DATABASE_URL', 'sqlite:///fullscopetest.db')

        if database_url.startswith('sqlite'):
            from sqlalchemy.pool import NullPool
            engine_options.setdefault('poolclass', NullPool)

        self._engine = create_engine(database_url, **engine_options)
        self._session = scoped_session(
            sessionmaker(bind=self._engine), scopefunc=_scopefunc
        )
        return self

    @property
    def engine(self):
        return self._engine

    @property
    def session(self):
        if self._session is None:
            raise RuntimeError("Database not initialized. Call db.init() first.")
        return self._session

    @property
    def metadata(self):
        """SQLAlchemy Metadata，用于 Alembic 迁移和 create_all"""
        return Base.metadata

    def create_all(self):
        """创建所有表"""
        Base.metadata.create_all(self._engine)

    def drop_all(self):
        """删除所有表"""
        Base.metadata.drop_all(self._engine)

    def remove(self):
        """移除当前作用域的会话（测试 teardown / 后台线程收尾用）"""
        if self._session is not None:
            self._session.remove()

    def __repr__(self):
        if self._engine is not None:
            return f'<Database engine={self._engine}>'
        return '<Database (not initialized)>'


# 全局数据库实例
db = Database()
