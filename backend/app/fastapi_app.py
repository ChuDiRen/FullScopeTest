"""
大熊AI测试平台 FastAPI 主应用（唯一生产后端，零 Flask）

- /api/v1/* : 原 Flask 蓝图的等价实现（app/api/routes/，路径 100% 兼容）
- /api/v2/* : FastAPI 原生增强接口（app/api/v2/）
- /health*  : 健康检查（app/api/routes/health.py，供 compose healthcheck）

运行：uvicorn app.fastapi_app:app --host 0.0.0.0 --port 5000 --workers 4
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse

from .core.logging import get_logger
from .api.v2.middleware import (
    BodySizeLimitMiddleware,
    DbSessionMiddleware,
    RateLimitMiddleware,
    RequestContextMiddleware,
    SecurityHeadersMiddleware,
)

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """进程生命周期：启动时初始化运行时（DB/日志/种子/插件/调度器），退出时清理"""
    from .core.runtime import init_runtime, shutdown_runtime
    from . import scheduler

    cfg = init_runtime()
    logger.info("FastAPI application starting...")

    # 内置用例模板种子（幂等：仅表空时插入）
    try:
        from .models.test_case_template import ensure_builtin_seed
        ensure_builtin_seed()
    except Exception as exc:
        logger.warning("Builtin case template seed failed", error=str(exc))

    # 定时调度器（环境变量可关；多副本部署只允许一个实例开启）
    scheduler_enabled = os.environ.get("SCHEDULER_ENABLED", "true").strip().lower() == "true"
    if scheduler_enabled and cfg.get("CONFIG_NAME") != "testing":
        try:
            scheduler.init_scheduler()
        except Exception as exc:
            logger.warning("Scheduler init failed", error=str(exc))

    yield

    try:
        scheduler.shutdown_scheduler()
    except Exception:
        pass
    shutdown_runtime()
    logger.info("FastAPI application shutting down...")


def create_fastapi_app(config_name: str = None) -> FastAPI:
    """
    创建 FastAPI 应用实例。

    Args:
        config_name: 配置环境名（development/testing/production），
                     默认读 APP_ENV 环境变量。
                     传入时写入环境，init_runtime 按它构建配置。
    """
    from .core.runtime import get_config
    from .api.v2.middleware import RateLimitMiddleware as _RLM  # noqa: F401

    if config_name:
        os.environ["APP_ENV"] = config_name
    # 配置在中间件注册阶段就需要（密钥/CORS），先构建（不触库）
    from .core.runtime import _build_config

    cfg = _build_config(config_name or os.environ.get("APP_ENV", "development"))
    is_production = cfg.get("CONFIG_NAME") == "production"

    app = FastAPI(
        title="大熊AI测试平台 API",
        description="大熊AI测试平台 自动化测试平台后端 API（v1 兼容 + v2 增强）",
        version="2.1.0",
        docs_url="/api/v2/docs",
        redoc_url="/api/v2/redoc",
        openapi_url="/api/v2/openapi.json",
        lifespan=lifespan,
    )

    # ---- 中间件（add_middleware 为 LIFO：最后加的在最外层）----
    app.add_middleware(DbSessionMiddleware)
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=int(cfg.get("MAX_CONTENT_LENGTH", 16 * 1024 * 1024)),
    )

    rate_fail_open_raw = os.environ.get("RATE_LIMIT_FAIL_OPEN", "")
    if rate_fail_open_raw:
        rate_fail_open = rate_fail_open_raw.strip().lower() == "true"
    else:
        rate_fail_open = not is_production
    app.add_middleware(
        RateLimitMiddleware,
        jwt_secret=cfg.get("JWT_SECRET_KEY") or "",
        enabled=True,  # 运行时开关由每请求的 RATELIMIT_ENABLED 配置治理
        fail_open=rate_fail_open,
    )
    app.add_middleware(SecurityHeadersMiddleware, is_production=is_production)
    app.add_middleware(GZipMiddleware, minimum_size=1024)
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.get("CORS_ORIGINS", ["http://localhost:3001"]),
        allow_credentials=True,
        allow_methods=cfg.get(
            "CORS_METHODS", ["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"]
        ),
        allow_headers=cfg.get(
            "CORS_ALLOW_HEADERS", ["Authorization", "Content-Type", "X-Request-ID"]
        ),
        max_age=cfg.get("CORS_MAX_AGE", 3600),
    )

    register_exception_handlers(app, is_production=is_production)
    register_routes(app)
    register_metrics(app)

    logger.info("FastAPI application created", config=cfg.get("CONFIG_NAME"))
    return app


# ---------------------------------------------------------------------------
# 异常处理（统一 {"code", "message", "errors"?, "request_id"} 信封）
# ---------------------------------------------------------------------------

def _request_id(request: Request) -> str:
    state = request.scope.get("state") or {}
    rid = state.get("request_id", "") if isinstance(state, dict) else ""
    if not rid:
        from .core.runtime import ctx

        rid = ctx.get_request_id()
    return rid or ""


def register_exception_handlers(app: FastAPI, is_production: bool = False):
    """统一异常 → JSON 响应"""

    @app.exception_handler(Exception)
    async def unhandled_error(request: Request, exc: Exception):
        logger.error(
            "Unhandled exception",
            error=str(exc),
            error_type=type(exc).__name__,
            request_id=_request_id(request),
        )
        message = "服务器内部错误"
        if not is_production:
            message = f"服务器内部错误: {exc}"
        return JSONResponse(
            status_code=500,
            content={"code": 500, "message": message, "request_id": _request_id(request)},
        )

    @app.exception_handler(HTTPException)
    async def http_exception(request: Request, exc: HTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            detail = detail.get("message") or str(detail)
        messages = {
            400: "请求参数错误",
            401: "未授权访问",
            403: "禁止访问",
            404: "资源不存在",
            405: "方法不允许",
            429: "请求频率超过限制",
        }
        message = (
            detail if isinstance(detail, str) and detail
            else messages.get(exc.status_code, str(detail))
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "code": exc.status_code,
                "message": message,
                "request_id": _request_id(request),
            },
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        errors = [
            {
                "loc": ".".join(str(x) for x in err.get("loc", [])),
                "msg": err.get("msg", ""),
            }
            for err in exc.errors()
        ]
        return JSONResponse(
            status_code=400,
            content={
                "code": 400,
                "message": "请求参数错误",
                "errors": errors,
                "request_id": _request_id(request),
            },
        )

    # 业务异常体系（app/utils/exceptions.py）
    try:
        from .utils.exceptions import AppError
    except ImportError:  # pragma: no cover
        return

    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError):
        logger.warning(
            "应用异常",
            error_type=type(exc).__name__,
            message=exc.message,
            code=exc.code,
            request_id=_request_id(request),
        )
        content = {
            "code": exc.code,
            "message": exc.message,
            "request_id": _request_id(request),
        }
        if getattr(exc, "errors", None):
            content["errors"] = exc.errors
        return JSONResponse(status_code=exc.code, content=content)


# ---------------------------------------------------------------------------
# 路由注册
# ---------------------------------------------------------------------------

def register_routes(app: FastAPI):
    """注册 v1（平迁自 Flask 蓝图）与 v2（FastAPI 原生）路由"""
    from .api import routes as v1_package

    registered = 0
    for router in v1_package.iter_routers():
        app.include_router(router)
        registered += len(router.routes)
    logger.info("v1 routes registered", count=registered)

    # v2 增强接口（FastAPI 原生）
    from .api.v2.auth import router as auth_router
    from .api.v2.test_cases import router as test_cases_router
    from .api.v2.api_tests import router as api_tests_router
    from .api.v2.ui_tests import router as ui_tests_router
    from .api.v2.perf_tests import router as perf_tests_router
    from .api.v2.openapi_docs import router as openapi_router

    app.include_router(auth_router, prefix="/api/v2/auth")
    app.include_router(test_cases_router, prefix="/api/v2/test-cases")
    app.include_router(api_tests_router, prefix="/api/v2/api-tests")
    app.include_router(ui_tests_router, prefix="/api/v2/ui-tests")
    app.include_router(perf_tests_router, prefix="/api/v2/perf-tests")
    app.include_router(openapi_router, prefix="/api/v2")

    @app.get("/api/v2/health")
    async def v2_health():
        return {"status": "ok", "version": "2.1.0"}


def register_metrics(app: FastAPI):
    """挂载 /metrics（Prometheus）。设置 METRICS_TOKEN 后需 ?token= 或 x-metrics-token 匹配"""
    try:
        from prometheus_client import make_asgi_app
    except ImportError:
        logger.warning("prometheus_client not available, /metrics disabled")
        return

    metrics_app = make_asgi_app()
    token = os.environ.get("METRICS_TOKEN", "").strip()

    async def guarded_metrics(scope, receive, send):
        if token:
            headers = dict(
                (k.decode("latin-1"), v.decode("latin-1"))
                for k, v in scope.get("headers", [])
            )
            query = scope.get("query_string", b"").decode("latin-1")
            provided = headers.get("x-metrics-token", "")
            for pair in query.split("&"):
                if pair.startswith("token="):
                    provided = provided or pair[len("token="):]
            if provided != token:
                await JSONResponse(
                    status_code=401,
                    content={"code": 401, "message": "未授权访问"},
                )(scope, receive, send)
                return
        await metrics_app(scope, receive, send)

    app.mount("/metrics", guarded_metrics)


# ASGI 入口：uvicorn app.fastapi_app:app（惰性创建，避免 import 即初始化运行时）
_app_instance = None


def _get_app():
    global _app_instance
    if _app_instance is None:
        _app_instance = create_fastapi_app()
    return _app_instance


def __getattr__(name):
    """PEP 562：支持 `uvicorn app.fastapi_app:app` 惰性实例化。

    注意：模块里不能出现 `app = None` 这样的显式赋值——那样 `app` 会进入模块
    dict，`__getattr__` 永远不会被触发，uvicorn 会拿到 None。
    """
    if name == "app":
        return _get_app()
    raise AttributeError(name)
