#!/usr/bin/env python3
"""
管理员密码重置工具

用法:
    cd backend
    python ../scripts/reset_password.py --user admin --password NewP@ssw0rd

说明:
    此脚本用于在邮件服务未配置时，管理员通过命令行重置用户密码。
    仅限管理员或拥有服务器访问权限的人员使用。
"""
import argparse
import os
import sys
from pathlib import Path

# 将 backend 目录加入 Python 路径
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'backend'))

# 先加载 backend/.env（override=False：外部环境变量优先，与 init_db.py 一致）
from dotenv import load_dotenv

_env_path = Path(__file__).resolve().parents[1] / "backend" / ".env"
if _env_path.exists():
    load_dotenv(dotenv_path=_env_path, override=False)
else:
    load_dotenv(override=False)


def main():
    parser = argparse.ArgumentParser(description='重置用户密码（管理员工具）')
    parser.add_argument('--user', '-u', required=True, help='用户名或邮箱')
    parser.add_argument('--password', '-p', required=True, help='新密码（至少8位）')
    parser.add_argument('--env', default='development', help='运行环境 (development/production)')
    args = parser.parse_args()

    if len(args.password) < 8:
        print('错误: 密码长度至少为 8 位')
        sys.exit(1)

    from app.core.runtime import init_runtime

    init_runtime(args.env)

    from datetime import datetime, timezone

    from app.core.passwords import generate_password_hash
    from app.extensions import db
    import app.models  # noqa: F401 — 确保所有模型注册
    from app.models.user import User

    # 按用户名或邮箱查找用户
    user = select(User).filter(
        (User.username == args.user) | (User.email == args.user)
    ).first()

    if not user:
        print(f'错误: 未找到用户 "{args.user}"')
        sys.exit(1)

    # 重置密码（scrypt 新格式，check_password_hash 兼容存量 werkzeug 哈希）
    user.password_hash = generate_password_hash(args.password)
    user.password_changed_at = datetime.now(timezone.utc)
    # 清除可能存在的重置 token
    user.reset_token = None
    user.reset_token_expires = None
    db.session.commit()

    print(f'成功: 用户 "{user.username}" (ID: {user.id}) 的密码已重置')
    print(f'密码修改时间: {user.password_changed_at.isoformat()}')


if __name__ == '__main__':
    main()
