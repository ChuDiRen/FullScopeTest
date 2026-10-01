"""
创建管理员账户脚本

密码来源优先级：--password 参数 > INIT_ADMIN_PASSWORD 环境变量 > getpass 交互输入。
无任何默认口令。已存在 admin 账户时默认跳过，仅显式传入 --force 才删除重建。
"""

import argparse
import getpass
import os
from pathlib import Path
from dotenv import load_dotenv

# 先加载 backend 目录下的 .env，避免 config.py 提前读取到默认值
env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path, override=True)

# 保证 `import app` 可用（脚本可从任意 cwd 运行）
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _resolve_password(args) -> str:
    """获取管理员密码：--password > INIT_ADMIN_PASSWORD > getpass 交互兜底"""
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


def main():
    parser = argparse.ArgumentParser(description='创建管理员账户（密码必须显式提供，无默认值）')
    parser.add_argument(
        '--password',
        help='管理员密码（未提供时读取 INIT_ADMIN_PASSWORD 环境变量或交互输入）',
    )
    parser.add_argument(
        '--force',
        action='store_true',
        help='已存在 admin 账户时，允许删除旧账户后重建（危险操作）',
    )
    args = parser.parse_args()

    password = _resolve_password(args)

    print("DATABASE_URL =", os.getenv("DATABASE_URL"))
    print("CELERY_ENABLE =", os.getenv("CELERY_ENABLE"))

    from app.core.runtime import init_runtime
    from app.core.passwords import generate_password_hash
    from app.extensions import db
    from app.models import User

    init_runtime()

    # 删除已存在的 admin 用户（仅在显式 --force 时允许）
    existing = User.query.filter_by(username='admin').first()
    if existing:
        if not args.force:
            print('已存在 admin 账户，跳过创建。如需删除重建请显式传入 --force。')
            return
        print('警告: 已指定 --force，即将删除已存在的 admin 账户及其关联数据！')
        db.session.delete(existing)
        db.session.commit()
        print(f'已删除旧的管理员账户: {existing.username}')

    # 创建管理员
    user = User(
        username='admin',
        email='admin@example.com',
        password_hash=generate_password_hash(password),
    )
    db.session.add(user)
    db.session.commit()

    print('✅ 管理员账户创建成功!')
    print('   用户名: admin')
    print('   密码: 已设置（不回显，仅此确认一次）')
    print('   邮箱: admin@example.com')
    print(f'   ID: {user.id}')


if __name__ == '__main__':
    main()
