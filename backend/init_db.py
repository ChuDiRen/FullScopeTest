#!/usr/bin/env python
"""Initialize database with all tables.

Admin password must be provided via --password, the INIT_ADMIN_PASSWORD
environment variable, or interactive getpass input. There is no default.
"""
import argparse
import getpass
import sys
import os
from pathlib import Path

# Add the backend directory to Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 先加载 backend 目录下的 .env（override=False：外部环境变量优先）
from dotenv import load_dotenv

_env_path = Path(__file__).resolve().parent / ".env"
if _env_path.exists():
    load_dotenv(dotenv_path=_env_path, override=False)
else:
    load_dotenv(override=False)

from app.core.runtime import init_runtime
from app.core.passwords import generate_password_hash
from app.extensions import db


def _resolve_password(args):
    """获取管理员密码：--password > INIT_ADMIN_PASSWORD 环境变量 > getpass 交互兜底"""
    if args.password:
        return args.password

    env_password = os.environ.get('INIT_ADMIN_PASSWORD', '')
    if env_password:
        return env_password

    password = getpass.getpass('请输入管理员密码: ')
    if not password:
        raise SystemExit('错误: 密码不能为空')
    confirm = getpass.getpass('请再次输入密码确认: ')
    if password != confirm:
        raise SystemExit('错误: 两次输入的密码不一致')
    return password


def init_database(args):
    """Create all database tables."""
    init_runtime()

    # 确保所有模型注册到 Base.metadata
    import app.models  # noqa: F401

    print("Creating database tables...")
    db.create_all()

# 内置用例模板种子（幂等）
from app.models.test_case_template import ensure_builtin_seed
seeded = ensure_builtin_seed()
if seeded:
    print(f"已插入 {seeded} 个内置用例模板")
    print("Database tables created successfully!")

    # Create admin user
    from sqlalchemy import select
    from app.models.user import User
    admin = db.session.scalar(select(User).filter_by(username='admin'))
    if not admin:
        print("Creating admin user...")
        admin = User(
            username='admin',
            email='admin@fullscopetest.com',
            role='admin',
            is_active=True,
            password_hash=generate_password_hash(_resolve_password(args)),
        )
        db.session.add(admin)
        db.session.commit()
        # 口令只允许打印一次确认行，不回显明文
        print("Admin user created: admin (password set from explicit source, not echoed)")
    else:
        print("Admin user already exists.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Initialize database and create admin user（密码必须显式提供，无默认值）')
    parser.add_argument(
        '--password',
        help='管理员密码（未提供时读取 INIT_ADMIN_PASSWORD 环境变量或交互输入）',
    )
    init_database(parser.parse_args())
