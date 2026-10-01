"""
扩展实例

集中管理跨模块共享的基础设施实例，避免循环导入：
- db: SQLAlchemy 2.0 数据层（app/database.py）
- celery: Celery 实例（配置由 app/celery_app.make_celery 在启动时装入）
"""

from celery import Celery

from .database import db

# Celery 实例 - 配置由 celery_app.make_celery() 在启动时装入
celery = Celery(
    __name__,
    include=["app.tasks"],  # 自动导入任务模块
)
