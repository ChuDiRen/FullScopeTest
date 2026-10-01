"""
Celery Worker 启动脚本（零 Flask）

启动 Celery 工作进程来执行异步任务。
模块级 `celery` 实例供 compose healthcheck 使用：
    celery -A celery_worker.celery inspect ping
"""

import sys
import os
from pathlib import Path

# 保证 `import app` 可用（脚本可从任意 cwd 运行）
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.celery_app import make_celery
from app.utils.celery_worker_options import build_worker_argv

# 创建并初始化 Celery 实例（内部会 ensure_runtime，加载 broker 配置）
celery = make_celery()

# 显式导入任务模块（确保任务被注册）
import app.tasks  # noqa: E402,F401

if __name__ == '__main__':
    # 启动 Celery worker
    celery.start(argv=build_worker_argv(loglevel='info'))
