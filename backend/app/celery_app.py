"""
Celery 应用配置（零 Flask）

初始化 Celery 应用，配置任务队列、死信队列（DLQ）和任务可靠性保障。
任务基类 ContextTask 提供：运行时初始化、scoped session 生命周期、DLQ 告警日志。
"""

from .core.runtime import ensure_runtime, session_teardown
from sqlalchemy import update


def make_celery():
    """创建并按 runtime 配置初始化 Celery 实例"""
    from .extensions import celery
    cfg = ensure_runtime()

    celery.conf.update(
        broker_url=cfg.get("CELERY_BROKER_URL", "redis://localhost:6379/0"),
        result_backend=cfg.get("CELERY_RESULT_BACKEND", "redis://localhost:6379/0"),
        task_track_started=cfg.get("CELERY_TASK_TRACK_STARTED", True),
        task_time_limit=cfg.get("CELERY_TASK_TIME_LIMIT", 30 * 60),
        accept_content=cfg.get("CELERY_ACCEPT_CONTENT", ["json"]),
        task_serializer=cfg.get("CELERY_TASK_SERIALIZER", "json"),
        result_serializer=cfg.get("CELERY_RESULT_SERIALIZER", "json"),
        timezone="Asia/Shanghai",
        enable_utc=True,
        # 可靠性配置
        task_acks_late=cfg.get("CELERY_TASK_ACKS_LATE", True),
        task_reject_on_worker_lost=cfg.get("CELERY_TASK_REJECT_ON_WORKER_LOST", True),
        task_routes=cfg.get("CELERY_TASK_ROUTES", {"tasks.*": {"queue": "celery"}}),
        task_default_retry_delay=cfg.get("CELERY_TASK_DEFAULT_RETRY_DELAY", 60),
        task_max_retries=cfg.get("CELERY_TASK_MAX_RETRIES", 3),
        # 死信队列优先级配置
        task_queue_max_priority=10,
        task_default_priority=5,
    )

    class ContextTask(celery.Task):
        """任务基类：运行时初始化 + scoped session 生命周期 + DLQ 告警"""

        abstract = True
        max_retries = 3
        default_retry_delay = 60
        acks_late = True
        reject_on_worker_lost = True

        def __call__(self, *args, **kwargs):
            from .core.runtime import task_session_scope

            ensure_runtime()
            # threads/gevent 池下并发任务必须各自持有独立 session 作用域，
            # 否则共享 None 作用域互相踩踏（写入丢失/IllegalStateChangeError）
            with task_session_scope():
                try:
                    return self.run(*args, **kwargs)
                finally:
                    session_teardown()

        def on_failure(self, exc, task_id, args, kwargs, einfo):
            """任务最终失败时（重试耗尽）记录告警日志"""
            from .core.logging import get_logger

            task_logger = get_logger("celery.dlq")
            task_logger.error(
                "Task permanently failed — moved to dead letter queue",
                task_id=task_id,
                task_name=self.name,
                exception_type=type(exc).__name__,
                exception_message=str(exc),
                retries_exhausted=True,
                args=str(args),
                kwargs=str(kwargs),
            )

    celery.Task = ContextTask
    return celery
