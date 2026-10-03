"""
定时任务调度器（纯 APScheduler，零 Flask）

- flock 文件锁防多进程重复启动（Windows 自动跳过锁直接启动）
- 内置任务：数据归档清理（每天 03:00）、僵尸任务清理（每 30 分钟）
- 由 FastAPI lifespan 启动/关闭（init_scheduler / shutdown_scheduler）
"""

import os
import sys
import atexit

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

# Windows 平台不支持 fcntl，需要特殊处理
try:
    import fcntl
except ImportError:
    fcntl = None

from .core.logging import get_logger
from sqlalchemy import select
from .extensions import db

scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
logger = get_logger(__name__)

# 全局文件锁句柄
_scheduler_lock = None
_scheduler_started = False


def init_scheduler():
    """初始化调度器（带防多进程重复启动机制），返回是否本进程持有调度器"""
    global _scheduler_lock, _scheduler_started

    lock_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "scheduler.lock")

    started = False
    try:
        if sys.platform == "win32" or not fcntl:
            # Windows 下多进程调度问题不明显（通常不用 Gunicorn），直接启动
            scheduler.start()
            started = True
        else:
            _scheduler_lock = open(lock_file, "w")
            # 尝试获取排他非阻塞锁
            fcntl.flock(_scheduler_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

            # 拿到锁，可以启动调度器
            scheduler.start()
            started = True
            logger.info("获取调度器锁成功，已启动 APScheduler")

            # 注册退出时释放锁
            def release_lock():
                try:
                    fcntl.flock(_scheduler_lock, fcntl.LOCK_UN)
                    _scheduler_lock.close()
                    if os.path.exists(lock_file):
                        os.remove(lock_file)
                except Exception:
                    pass

            atexit.register(release_lock)

        # 启动时加载数据库中所有激活的任务
        from .extensions import db
        from sqlalchemy import inspect

        try:
            inspector = inspect(db.engine)
            if inspector.has_table("scheduled_tasks"):
                from .models.scheduled_task import ScheduledTask

                active_tasks = db.session.scalars(select(ScheduledTask).filter_by(is_active=True)).all()
                for task in active_tasks:
                    add_or_update_job(task)
            else:
                logger.warning("scheduled_tasks 表不存在，跳过加载定时任务")
        except Exception as e:
            logger.warning("数据库未就绪，跳过加载定时任务", error=str(e))

        # 加载激活的报告调度
        try:
            inspector = inspect(db.engine)
            if inspector.has_table("report_schedules"):
                from .models.report_schedule import ReportSchedule

                active_schedules = db.session.scalars(
                    select(ReportSchedule).filter_by(is_active=True)
                ).all()
                for sched in active_schedules:
                    add_or_update_report_job(sched)
            else:
                logger.warning("report_schedules 表不存在，跳过加载报告调度")
        except Exception as e:
            logger.warning("数据库未就绪，跳过加载报告调度", error=str(e))

        # 注册内置定时任务
        _register_builtin_jobs()

        if started:
            _scheduler_started = True
        return started

    except IOError:
        # 获取锁失败，说明其他进程已经启动了调度器
        logger.info("调度器已在其他进程中启动，当前进程跳过初始化")
        # 将 scheduler 对象置于静默状态，提供空实现以防调用报错
        _patch_dummy_scheduler(scheduler)
        return False


def shutdown_scheduler():
    """关闭调度器（FastAPI lifespan shutdown 调用）"""
    global _scheduler_started
    if _scheduler_started:
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        _scheduler_started = False


def _patch_dummy_scheduler(sched):
    """提供空实现，防止其他没有拿到锁的进程调用 scheduler.add_job 时报错"""
    sched.get_job = lambda *args, **kwargs: None
    sched.add_job = lambda *args, **kwargs: None
    sched.modify_job = lambda *args, **kwargs: None
    sched.remove_job = lambda *args, **kwargs: None


def get_job_id(task_id):
    return f"scheduled_task_{task_id}"


def add_or_update_job(task):
    """添加或更新任务到调度器"""
    job_id = get_job_id(task.id)

    # 解析 cron 表达式（5 位：分 时 日 月 周）
    try:
        trigger = CronTrigger.from_crontab(task.cron_expression)

        if scheduler.get_job(job_id):
            scheduler.modify_job(job_id, trigger=trigger)
        else:
            scheduler.add_job(
                id=job_id,
                func=execute_scheduled_task,
                args=[task.id],
                trigger=trigger,
                replace_existing=True,
            )
        logger.info("成功加载定时任务", job_id=job_id, name=task.name)
    except Exception as e:
        logger.error("加载定时任务失败", job_id=job_id, error=str(e))


def remove_job(task_id):
    """从调度器移除任务"""
    job_id = get_job_id(task_id)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
        logger.info("已移除定时任务", job_id=job_id)


def execute_scheduled_task(task_id):
    """执行定时任务（调度器后台线程中运行）"""
    from .core.runtime import ensure_runtime, session_teardown, task_session_scope

    ensure_runtime()
    # APScheduler 线程同样需要独立 session 作用域（与 Celery worker 同因：
    # 线程池拿到的 ContextVar 都是默认 None，会共享同一个 scoped session）
    with task_session_scope():
        try:
            from .models.scheduled_task import ScheduledTask
            from .tasks import run_api_collection_task, run_web_collection_task, run_perf_scenario_task

            task = db.session.get(ScheduledTask, task_id)
            if not task or not task.is_active:
                return

            logger.info("开始执行定时任务", task_name=task.name, task_id=task.id)

            try:
                celery_task = None
                if task.target_type == "api_collection":
                    celery_task = run_api_collection_task.delay(task.target_id, None)
                elif task.target_type == "web_collection":
                    celery_task = run_web_collection_task.delay(task.target_id, None)
                elif task.target_type == "perf_scenario":
                    celery_task = run_perf_scenario_task.delay(task.target_id)
                else:
                    logger.error("未知的任务目标类型", target_type=task.target_type)
                    return

                # 发送通知
                send_notification(task, "started", celery_task.id if celery_task else None)
            except Exception as e:
                logger.error("定时任务执行失败", error=str(e))
                send_notification(task, "failed", error=str(e))
        finally:
            session_teardown()


def send_notification(task, status, task_id=None, error=None):
    """发送 Webhook 通知 (如钉钉/飞书)"""
    if not task.notify_webhook:
        return

    # 判断是否需要通知
    if task.notify_events != "all" and status != task.notify_events:
        return

    try:
        title = f"定时任务: {task.name} 执行状态 - {status}"
        content = (
            f"**任务名称:** {task.name}\n**目标类型:** {task.target_type}"
            f"\n**目标ID:** {task.target_id}\n**状态:** {status}"
        )
        if task_id:
            content += f"\n**任务ID:** {task_id}"
        if error:
            content += f"\n**错误信息:** {error}"

        payload = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": content},
        }

        headers = {"Content-Type": "application/json"}
        response = requests.post(task.notify_webhook, json=payload, headers=headers, timeout=5)
        logger.info("通知发送结果", status_code=response.status_code)
    except Exception as e:
        logger.error("发送通知失败", error=str(e))


def _register_builtin_jobs():
    """注册内置定时任务（数据归档、僵尸任务清理等）"""
    try:
        # 数据归档清理：每天凌晨 3:00 执行
        if not scheduler.get_job("builtin_data_retention"):
            scheduler.add_job(
                id="builtin_data_retention",
                func=_run_data_retention,
                trigger=CronTrigger.from_crontab("0 3 * * *"),
                replace_existing=True,
            )
            logger.info("已注册内置任务: 数据归档清理 (每天 03:00)")

        # 僵尸任务清理：每 30 分钟执行一次，把超时 running 的记录标记为 failed
        if not scheduler.get_job("builtin_stale_running_sweep"):
            scheduler.add_job(
                id="builtin_stale_running_sweep",
                func=_run_stale_running_sweep,
                trigger=CronTrigger.from_crontab("*/30 * * * *"),
                replace_existing=True,
            )
            logger.info("已注册内置任务: 僵尸任务清理 (每 30 分钟)")
    except Exception as exc:
        logger.warning("注册内置定时任务失败", error=str(exc))


def _run_data_retention():
    """执行数据归档清理（在调度器线程中运行）"""
    from .core.runtime import ensure_runtime, session_teardown

    ensure_runtime()
    try:
        from .services.data_retention_service import run_full_cleanup

        results = run_full_cleanup()
        logger.info("定时数据归档完成", results=results)
    except Exception as exc:
        logger.error("定时数据归档失败", error=str(exc))
    finally:
        session_teardown()


def _run_stale_running_sweep():
    """执行僵尸任务清理（在调度器线程中运行）"""
    from .core.runtime import ensure_runtime, session_teardown

    ensure_runtime()
    try:
        from .tasks.common import sweep_stale_running_tasks

        results = sweep_stale_running_tasks()
        logger.info("定时僵尸任务清理完成", results=results)
    except Exception as exc:
        logger.error("定时僵尸任务清理失败", error=str(exc))
    finally:
        session_teardown()


# ══════════════════════════════════════════════════════════════════════════════
# 定时报告调度（daily / weekly / monthly 测试统计聚合 + webhook 通知）
# ══════════════════════════════════════════════════════════════════════════════

_FREQUENCY_CRON = {
    "daily": "0 8 * * *",        # 每天 08:00
    "weekly": "0 8 * * 1",       # 每周一 08:00
    "monthly": "0 8 1 * *",      # 每月 1 日 08:00
}


def get_report_job_id(schedule_id):
    return f"report_schedule_{schedule_id}"


def add_or_update_report_job(schedule):
    """添加或更新报告调度到调度器"""
    job_id = get_report_job_id(schedule.id)
    cron = _FREQUENCY_CRON.get(schedule.frequency)
    if not cron:
        logger.error("未知的报告调度频率", frequency=schedule.frequency, job_id=job_id)
        return
    try:
        trigger = CronTrigger.from_crontab(cron)
        if scheduler.get_job(job_id):
            scheduler.modify_job(job_id, trigger=trigger)
        else:
            scheduler.add_job(
                id=job_id,
                func=execute_report_schedule,
                args=[schedule.id],
                trigger=trigger,
                replace_existing=True,
            )
        logger.info("成功加载报告调度", job_id=job_id, name=schedule.name, frequency=schedule.frequency)
    except Exception as e:
        logger.error("加载报告调度失败", job_id=job_id, error=str(e))


def remove_report_job(schedule_id):
    """从调度器移除报告调度"""
    job_id = get_report_job_id(schedule_id)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
        logger.info("已移除报告调度", job_id=job_id)


def execute_report_schedule(schedule_id):
    """执行报告调度：聚合统计并发送 webhook（调度器后台线程中运行）"""
    from .core.runtime import ensure_runtime, session_teardown

    ensure_runtime()
    try:
        from datetime import datetime, timedelta, timezone

        from .models.report_schedule import ReportSchedule
        from .models.test_run import TestRun

        schedule = db.session.get(ReportSchedule, schedule_id)
        if not schedule or not schedule.is_active:
            return

        logger.info("开始执行报告调度", name=schedule.name, schedule_id=schedule_id)

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        days = {"daily": 1, "weekly": 7, "monthly": 30}.get(schedule.frequency, 1)
        since = now - timedelta(days=days)

        query = select(TestRun).filter(TestRun.created_at >= since)
        if schedule.project_id:
            query = query.filter(TestRun.project_id == schedule.project_id)
        runs = db.session.scalars(query).all()

        summary = {
            "period_start": since.isoformat() + "Z",
            "period_end": now.isoformat() + "Z",
            "frequency": schedule.frequency,
            "total_runs": len(runs),
            "passed": len([r for r in runs if r.status == "passed"]),
            "failed": len([r for r in runs if r.status == "failed"]),
            "running": len([r for r in runs if r.status == "running"]),
        }
        summary["pass_rate"] = round(
            summary["passed"] / summary["total_runs"] * 100, 1
        ) if summary["total_runs"] else 0.0

        schedule.last_run_at = now
        schedule.last_result = summary
        db.session.commit()

        _send_report_notification(schedule, summary)
        logger.info("报告调度执行完成", schedule_id=schedule_id, total_runs=summary["total_runs"])
    except Exception as exc:
        logger.error("报告调度执行失败", schedule_id=schedule_id, error=str(exc))
    finally:
        session_teardown()


def _send_report_notification(schedule, summary):
    """推送报告摘要到 webhook（钉钉/飞书 markdown 格式）"""
    import requests as _requests

    if not schedule.webhook_url:
        return
    title = f"定时测试报告: {schedule.name}"
    content = (
        f"**统计周期:** {summary['frequency']}\n"
        f"**总执行:** {summary['total_runs']}\n"
        f"**通过:** {summary['passed']}  **失败:** {summary['failed']}\n"
        f"**通过率:** {summary['pass_rate']}%"
    )
    try:
        resp = _requests.post(
            schedule.webhook_url,
            json={"msgtype": "markdown", "markdown": {"title": title, "text": content}},
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        logger.info("报告通知发送结果", schedule_id=schedule.id, status_code=resp.status_code)
    except Exception as e:
        logger.error("报告通知发送失败", schedule_id=schedule.id, error=str(e))
