"""共享工具函数"""
from sqlalchemy import select
from ..extensions import db

import subprocess
import tempfile
import sys
import time
import os
import json
import threading
import queue
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List


_backend_root_cache = None


from contextlib import contextmanager


@contextmanager
def runtime_context():
    """任务执行上下文（原 Flask app_context 的零依赖替代）。

    运行时由 ContextTask.__call__ 统一 ensure + session_teardown；
    此管理器仅保留旧代码的 with 缩进结构。
    """
    from app.core.runtime import ensure_runtime

    ensure_runtime()
    yield


def parse_target_url(url: str) -> tuple:
    """解析目标 URL → (base_host, endpoint_path)（原 v1 perf_test 同名纯函数）"""
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
        base_host = f"{parsed.scheme}://{parsed.netloc}"
        endpoint_path = parsed.path or "/"
        return base_host, endpoint_path
    except Exception:
        return url.rstrip("/"), "/"


def get_backend_root() -> str:
    """backend 目录绝对路径（原 Flask app.root_path 的等价物，缓存复用）"""
    global _backend_root_cache
    if _backend_root_cache is None:
        _backend_root_cache = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return _backend_root_cache


def _minimal_locust_env() -> dict:
    """Locust 子进程最小环境。

    旧实现未传 env=，子进程继承后端全部环境变量（DATABASE_URL、SECRET_KEY、
    OSS/AI 密钥等），任何能创建性能测试场景的用户都能窃取。此处只保留子进程
    运行必需项，业务密钥一律不透传。
    """
    env = {
        'PATH': os.environ.get('PATH', ''),
        'SYSTEMROOT': os.environ.get('SYSTEMROOT', ''),   # Windows 必需
        'COMSPEC': os.environ.get('COMSPEC', ''),         # Windows subprocess 必需
        'TEMP': os.environ.get('TEMP', ''),
        'TMP': os.environ.get('TMP', ''),
        'LANG': os.environ.get('LANG', ''),
    }
    return {k: v for k, v in env.items() if v}


def sweep_stale_running_tasks(stale_hours: int = 2) -> dict:
    """把长时间停留在 running 状态的执行记录标记为 failed，防止僵尸任务。

    - TestRun: 无 updated_at 字段，按 created_at 判定；错误信息写入 error_message
    - PerformanceTestResult: 按 updated_at 判定；错误信息写入 raw_result JSON

    Returns:
        dict: 各表清扫的记录数
    """
    from app.extensions import db
    from app.models.test_run import TestRun
    from app.models.perf_test_result import PerformanceTestResult

    cutoff = datetime.utcnow() - timedelta(hours=stale_hours)
    swept = {'test_runs': 0, 'performance_test_results': 0}

    stale_runs = db.session.scalars(select(TestRun).filter(
        TestRun.status == 'running',
        TestRun.created_at < cutoff,
    )).all()
    for run in stale_runs:
        run.status = 'failed'
        run.error_message = 'task timeout - swept'
        run.finished_at = datetime.utcnow()
    swept['test_runs'] = len(stale_runs)

    stale_results = db.session.scalars(select(PerformanceTestResult).filter(
        PerformanceTestResult.status == 'running',
        PerformanceTestResult.updated_at < cutoff,
    )).all()
    for result in stale_results:
        result.status = 'failed'
        result.raw_result = {
            'error': 'task timeout - swept',
            'swept_at': datetime.utcnow().isoformat() + 'Z',
        }
        result.finished_at = datetime.utcnow()
    swept['performance_test_results'] = len(stale_results)

    if stale_runs or stale_results:
        db.session.commit()

    return swept


class RealtimeStatsCollector:
    """实时统计数据收集器"""

    def __init__(self):
        self.request_count = 0
        self.failure_count = 0
        self.response_times = []
        self.lock = threading.Lock()
        self.last_update = time.time()

    def record_request(self, response_time, success=True):
        """记录请求数据"""
        with self.lock:
            self.request_count += 1
            if not success:
                self.failure_count += 1
            self.response_times.append(response_time)
            self.last_update = time.time()

    def get_stats(self):
        """获取当前统计数据"""
        with self.lock:
            if self.request_count == 0:
                return {
                    'request_count': 0,
                    'failure_count': 0,
                    'error_rate': 0,
                    'avg_response_time': 0,
                    'min_response_time': 0,
                    'max_response_time': 0,
                    'throughput': 0
                }

            avg_response_time = sum(self.response_times) / len(self.response_times)
            min_response_time = min(self.response_times)
            max_response_time = max(self.response_times)
            error_rate = (self.failure_count / self.request_count) * 100

            # 计算吞吐量（请求/秒）
            elapsed = time.time() - self.last_update
            throughput = self.request_count / elapsed if elapsed > 0 else 0

            return {
                'request_count': self.request_count,
                'failure_count': self.failure_count,
                'error_rate': error_rate,
                'avg_response_time': avg_response_time,
                'min_response_time': min_response_time,
                'max_response_time': max_response_time,
                'throughput': throughput
            }


def _build_step_stages(user_count, step_users, step_duration, run_time):
    """Build staged load plan where step_users means incremental users per step."""
    if user_count <= 0 or step_users <= 0 or step_duration <= 0 or run_time <= 0:
        return []

    stages = []
    step_spawn_rate = max(1, (step_users + step_duration - 1) // step_duration)
    current_users = 0
    stage_start = 0

    while stage_start < run_time:
        if current_users < user_count:
            current_users = min(current_users + step_users, user_count)
        stage_end = min(stage_start + step_duration, run_time)
        stages.append({
            'start': int(stage_start),
            'end': int(stage_end),
            'users': int(current_users),
            'spawn_rate': int(step_spawn_rate),
        })
        stage_start += step_duration

    return stages


def _inject_step_load_shape(script_content, stages):
    if not stages:
        return script_content

    shape_script = f'''

from locust import LoadTestShape

class StepLoadShape(LoadTestShape):
    stages = {json.dumps(stages)}

    def tick(self):
        run_time = self.get_run_time()
        for stage in self.stages:
            if run_time < stage["end"]:
                return (stage["users"], stage["spawn_rate"])
        return None
'''
    return script_content.rstrip() + shape_script + '\n'


def _build_locust_command(locustfile, base_host, csv_prefix, run_time, user_count, spawn_rate, step_load_enabled):
    cmd = [
        sys.executable, '-m', 'locust',
        '-f', locustfile,
        '--host', base_host,
        '--run-time', f'{run_time}s',
        '--headless',
        '--csv', csv_prefix,
        '--loglevel', 'WARNING',
        '--only-summary',
        '--csv-full-history'
    ]

    if not step_load_enabled:
        cmd.extend([
            '--users', str(user_count),
            '--spawn-rate', str(spawn_rate),
        ])

    return cmd

