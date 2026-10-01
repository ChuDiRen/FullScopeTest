"""Alembic 迁移环境（零 Flask）。

- 数据库连接：backend/.env 或环境变量 DATABASE_URL（与 init_db.py、core/runtime 一致）
- 模型元数据：app.database.db（import app.models 完成注册）

用法（在 backend 目录下）：
    alembic -c migrations/alembic.ini upgrade head
    DATABASE_URL=postgresql+psycopg2://... alembic revision --autogenerate -m "..."
"""
import logging
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

# 保证可 import app.*（alembic 从 backend 目录运行时已在 sys.path，双保险）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 先加载 backend/.env（override=False：外部环境变量优先，与 init_db.py 一致）
from dotenv import load_dotenv

_env_path = Path(__file__).resolve().parents[1] / ".env"
if _env_path.exists():
    load_dotenv(dotenv_path=_env_path, override=False)
else:
    load_dotenv(override=False)

config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
fileConfig(config.config_file_name)
logger = logging.getLogger('alembic.env')


def _database_url() -> str:
    """连接串统一走应用配置（含 SQLite 相对路径到 backend 目录的解析规则）。"""
    from app.core.runtime import get_config

    return get_config().get("SQLALCHEMY_DATABASE_URI") or "sqlite:///fullscopetest.db"


url = _database_url()
config.set_main_option('sqlalchemy.url', url.replace('%', '%%'))

# add your model's MetaData object here
# for 'autogenerate' support
import app.models  # noqa: F401 — 确保所有模型注册到 metadata
from app.database import db

target_metadata = db.metadata


def run_migrations_offline():
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    context.configure(
        url=url, target_metadata=target_metadata, literal_binds=True,
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """

    # this callback is used to prevent an auto-migration from being generated
    # when there are no changes to the schema
    # reference: http://alembic.zzzcomputing.com/en/latest/cookbook.html
    def process_revision_directives(context, revision, directives):
        if getattr(config.cmd_opts, 'autogenerate', False):
            script = directives[0]
            if script.upgrade_ops.is_empty():
                directives[:] = []
                logger.info('No changes in schema detected.')

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix='sqlalchemy.',
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            process_revision_directives=process_revision_directives,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
