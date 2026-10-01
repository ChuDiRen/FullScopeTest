"""
健康检查端点（自 app/core/health.py 平迁，路径不变）

- /health/live  — 存活探针
- /health/ready — 就绪探针（DB 关键 / Redis、Celery 非关键）
- /health       — 综合健康检查（等价 /health/ready）
"""

from datetime import datetime, timezone

import redis as redis_lib
from fastapi import APIRouter, Response
from sqlalchemy import text as sa_text

from ....extensions import db, celery
from ....core.logging import get_logger

logger = get_logger(__name__)

router = APIRouter(tags=["health"])

SERVICE_VERSION = __import__("os").environ.get("APP_VERSION", "1.0.0")


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z"


@router.get("/health/live")
def liveness_probe():
    """存活探针：进程存活即 200，不检查依赖"""
    return {
        "status": "ok",
        "service": "fullscopetest",
        "version": SERVICE_VERSION,
        "timestamp": _now_iso(),
    }


@router.get("/health/ready")
def readiness_probe():
    """就绪探针：database 关键组件失败返回 503，redis/celery 降级不阻断"""
    checks = {
        "database": _check_database(),
        "redis": _check_redis(),
        "celery": _check_celery(),
    }

    all_critical_ok = checks["database"].get("status") != "error"
    has_warning = any(
        c.get("status") in ("warning", "error") for c in checks.values()
    )

    if not all_critical_ok:
        overall_status = "error"
    elif has_warning:
        overall_status = "degraded"
    else:
        overall_status = "ok"

    body = {
        "status": overall_status,
        "service": "fullscopetest",
        "version": SERVICE_VERSION,
        "checks": checks,
        "timestamp": _now_iso(),
    }
    return Response(
        content=__import__("json").dumps(body),
        status_code=200 if all_critical_ok else 503,
        media_type="application/json",
    )


@router.get("/health")
def health_check():
    """综合健康检查（兼容旧版本，等同于 /health/ready）"""
    return readiness_probe()


# ── 组件检查 ──────────────────────────────────────────────────────────────

def _check_database() -> dict:
    try:
        db.session.execute(sa_text("SELECT 1"))
        result = {"status": "ok"}
        try:
            pool = db.engine.pool
            from sqlalchemy.pool import NullPool

            if isinstance(pool, NullPool):
                return result
            total_capacity = pool.size() + pool.overflow()
            result["pool"] = {
                "pool_size": pool.size(),
                "checked_in": pool.checkedin(),
                "checked_out": pool.checkedout(),
                "overflow": pool.overflow(),
            }
            if total_capacity > 0:
                usage = pool.checkedout() / total_capacity
                if usage > 0.8:
                    result["status"] = "warning"
                    result["message"] = f"连接池使用率过高: {usage:.0%}"
        except (AttributeError, NotImplementedError, TypeError):
            pass
        return result
    except Exception as e:
        logger.error("数据库健康检查失败", error=str(e))
        return {"status": "error", "message": str(e)}


def _check_redis() -> dict:
    import os

    try:
        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        r = redis_lib.from_url(redis_url, socket_timeout=2)
        r.ping()
        return {"status": "ok"}
    except Exception as e:
        logger.warning("Redis 健康检查失败", error=str(e))
        return {"status": "warning", "message": str(e)}


def _check_celery() -> dict:
    import os

    try:
        if os.environ.get("CELERY_ENABLE", "false").lower() != "true":
            return {"status": "disabled", "message": "Celery is disabled"}
        inspect = celery.control.inspect(timeout=2)
        active_workers = inspect.active() or {}
        if active_workers:
            return {"status": "ok", "workers": list(active_workers.keys())}
        return {"status": "warning", "message": "No active workers"}
    except Exception as e:
        logger.warning("Celery 健康检查失败", error=str(e))
        return {"status": "warning", "message": str(e)}
