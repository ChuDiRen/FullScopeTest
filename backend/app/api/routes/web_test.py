"""
Web 自动化测试模块 - FastAPI 平迁（自 app/api/web_test.py，蓝图挂载前缀 /api/v1）

路径/方法/状态码/响应字段与原 v1 完全一致（/api/v1/web-test/...），
前端与 CI 脚本零改动。全部端点为同步 def，运行于 RequestContextMiddleware
push 的 app context 内，直接复用 数据库会话（app/database.py ContextVar 作用域） 与 services 层。

鉴权映射：
- @jwt_required() → Depends(_current_user)（内部复用 deps.get_current_user）
- 公开端点（v1 即无鉴权，保持公开并注明）：
  * GET /api/v1/web-test/health — 模块健康检查

会话生命周期：
- 同步端点统一加 @release_session（deps.release_session）：视图返回后
  rollback/commit/remove 本 worker 线程的 scoped session，防 NullPool 连接泄漏
- 鉴权依赖包装为 async _current_user：在事件循环线程执行 get_current_user，
  其 session 由 teardown_appcontext 正常回收（与 api_test.py/reports.py 范例一致）

IDOR 防护（按 id 取对象一律带属主过滤，越权/不存在 → 404）：
- GET/PUT/DELETE /api/v1/web-test/scripts/{id} 与 /collections/{id}：
  service 层本就按 user_id 过滤（WebTestScript/WebTestCollection.user_id），沿用
- POST /api/v1/web-test/scripts（body.collection_id）：_get_collection_or_404
  带 filter_by_org_projects + user_id 属主过滤，沿用
- POST /api/v1/web-test/collections/{id}/run：集合属主过滤 + 集合内脚本按
  user_id 过滤，沿用
- GET /api/v1/web-test/scripts/{id}/snapshots/{type}/{name}（视觉基准/actual/diff
  图片）：按脚本 user_id 过滤，沿用
- POST /api/v1/web-test/scripts/{id}/run：按脚本 user_id 过滤，沿用
- POST /api/v1/web-test/ai/analyze-error（body.script_id）：按脚本 user_id 过滤，沿用
- 说明：本模块 v1 无按 id 取 TestRun / 视觉基线记录的端点（TestRun.triggered_user_id
  过滤在 reports/api_test 模块已补），现有按 id 取对象全部已带属主过滤

SSRF 防护（接受用户 URL 的路由，校验先于外呼/Popen）：
- POST /api/v1/web-test/ai/explore、ai/explore/stream：start_url 经 is_safe_url 校验（v1 已有，保持）
- POST /api/v1/web-test/record/start：url 经 is_safe_url 校验后才 Popen 启动
  playwright codegen（v1 已有，保持顺序）
- live view allocator：allocator_url 经 validate_url_safety 校验后才 requests.post
  （v1 已有校验，但校验失败时误把 error_response 当 dict 返回导致调用方崩溃；
  平迁修正为走模板回退分支，安全语义不变：永不向不安全 allocator 发起请求）

其他保持原行为：
- Celery 派发沿用 run_web_test_task.apply_async(args=[script_id, user_id],
  task_id=f'web_test_{script_id}_{user_id}')（与 v1 完全一致）
- 录制进程句柄（subprocess.Popen 无法序列化）仅在本地 _recording_process_objects
  持有引用，元数据存 SessionStore（Redis/内存回退），照抄 v1
"""
from pathlib import Path
from sqlalchemy import select

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from queue import Empty, Queue
from typing import Any, Dict, Optional
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from app.api.v2.deps import get_current_user, json_body, query_int, release_session
from app.core.logging import get_logger
from app.extensions import db
from app.models.user import User
from app.models.web_test_collection import WebTestCollection
from app.models.web_test_script import WebTestScript
from app.services.session_store import (
    LIVE_VIEW_KEY_PREFIX,
    LIVE_VIEW_TTL,
    RECORDING_KEY_PREFIX,
    RECORDING_TTL,
    get_session_store,
)
from app.services.web_test_service import WebTestService
from app.tasks import run_web_test_task
from app.utils.ai_script_generator import generate_test_script
from app.utils.ai_script_healer import analyze_test_error
from app.utils.ai_web_explorer import run_exploration_task
from app.utils.exceptions import NotFoundError, ValidationError
from app.utils.org_filter import filter_by_org_projects
from app.utils.sandbox import validate_url_safety
from app.utils.url_safety import is_safe_url
from app.utils.validators import validate_required

logger = get_logger(__name__)

# 初始化 Service（与 v1 一致，路由只做参数处理与响应组装）
web_test_service = WebTestService()

router = APIRouter(tags=["web-test"])

# 录制进程对象（subprocess.Popen 无法序列化，仅在本地持有引用；与 v1 一致）
_recording_process_objects = {}


# ---------------------------------------------------------------------------
# 鉴权包装 / 响应构造 / 通用助手
# ---------------------------------------------------------------------------

async def _current_user(request: Request) -> User:
    """鉴权依赖：在事件循环线程内执行同步 get_current_user，session 随 app context 回收"""
    return get_current_user(request)


def _request_id() -> str:
    """读取 RequestContextMiddleware 写入 scope state 的 request_id"""
    try:
        from app.core.runtime import ctx

        return ctx.get_request_id() or ""
    except Exception:
        return ""


def _success(data=None, message: str = "success", code: int = 200) -> JSONResponse:
    """等价 v1 success_response：{"code","message","data","timestamp"}"""
    return JSONResponse(
        status_code=code,
        content={
            "code": code,
            "message": message,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
        },
    )


def _error(code: int, message: str, errors=None) -> JSONResponse:
    """等价 v1 error_response：{"code","message","errors","request_id","timestamp"}"""
    return JSONResponse(
        status_code=code,
        content={
            "code": code,
            "message": message,
            "errors": errors,
            "request_id": _request_id(),
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
        },
    )


def _config_get(key: str, default: Any = None) -> Any:
    """读取 Flask config（流式生成器线程里 app context 可能已弹栈，容错返回默认值）"""
    try:
        from app.core.runtime import get_config

        return get_config().get(key, default)
    except Exception:
        return default


# ---------------------------------------------------------------------------
# AI 探索 / Live View 会话分配（自 v1 平迁，保持原逻辑）
# ---------------------------------------------------------------------------

def _build_runtime_ai_config(data: dict) -> dict:
    runtime_config = {
        "AI_ASSISTANT_ENABLED": _config_get("AI_ASSISTANT_ENABLED", True),
        "AI_ASSISTANT_BASE_URL": _config_get("AI_ASSISTANT_BASE_URL", ""),
        "AI_ASSISTANT_API_KEY": _config_get("AI_ASSISTANT_API_KEY", ""),
        "AI_ASSISTANT_MODEL": _config_get("AI_ASSISTANT_MODEL", ""),
        "AI_VISION_BASE_URL": _config_get("AI_VISION_BASE_URL", ""),
        "AI_VISION_API_KEY": _config_get("AI_VISION_API_KEY", ""),
        "AI_VISION_MODEL": _config_get("AI_VISION_MODEL", ""),
        "AI_EXPLORE_BROWSER_HEADLESS": _config_get("AI_EXPLORE_BROWSER_HEADLESS", "true"),
        "AI_EXPLORE_BROWSER_SLOW_MO": _config_get("AI_EXPLORE_BROWSER_SLOW_MO", 0),
    }

    if data.get("base_url"):
        runtime_config["AI_ASSISTANT_BASE_URL"] = str(data.get("base_url")).strip()
    if data.get("model"):
        runtime_config["AI_ASSISTANT_MODEL"] = str(data.get("model")).strip()
    if data.get("api_key"):
        runtime_config["AI_ASSISTANT_API_KEY"] = str(data.get("api_key")).strip()
    if data.get("vision_base_url"):
        runtime_config["AI_VISION_BASE_URL"] = str(data.get("vision_base_url")).strip()
    if data.get("vision_model"):
        runtime_config["AI_VISION_MODEL"] = str(data.get("vision_model")).strip()
    if data.get("vision_api_key"):
        runtime_config["AI_VISION_API_KEY"] = str(data.get("vision_api_key")).strip()
    if "explore_browser_headless" in data:
        runtime_config["AI_EXPLORE_BROWSER_HEADLESS"] = data.get("explore_browser_headless")
    if data.get("explore_browser_slow_mo") is not None:
        runtime_config["AI_EXPLORE_BROWSER_SLOW_MO"] = int(data.get("explore_browser_slow_mo"))

    return runtime_config


def _format_sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _resolve_live_view_url(data: dict, start_url: str) -> str:
    direct_url = str(data.get("live_view_url") or "").strip()
    if direct_url.startswith("http://") or direct_url.startswith("https://"):
        return direct_url
    template = str(
        data.get("live_view_url_template")
        or _config_get("AI_EXPLORE_LIVE_VIEW_URL_TEMPLATE")
        or ""
    ).strip()
    if not template:
        return ""
    return (
        template
        .replace("{start_url}", quote_plus(start_url))
        .replace("{start_url_raw}", start_url)
    )


def _build_internal_live_view_url(start_url: str, session_id: str) -> str:
    template = str(_config_get("AI_EXPLORE_LIVE_VIEW_INTERNAL_URL_TEMPLATE") or "").strip()
    if not template:
        return ""
    return (
        template
        .replace("{session_id}", quote_plus(session_id))
        .replace("{session_id_raw}", session_id)
        .replace("{start_url}", quote_plus(start_url))
        .replace("{start_url_raw}", start_url)
    )


def _allocate_internal_live_view_session(start_url: str, objective: str, max_steps: int, user_id: int) -> dict:
    session_id = str(uuid.uuid4())
    url = _build_internal_live_view_url(start_url, session_id)
    if not url:
        return {}
    # 存入 SessionStore（Redis 或内存），TTL 30 分钟
    store = get_session_store()
    store.set(
        f"{LIVE_VIEW_KEY_PREFIX}{session_id}",
        {
            "session_id": session_id,
            "user_id": user_id,
            "start_url": start_url,
            "objective": objective,
            "max_steps": max_steps,
            "created_at": time.time(),
        },
        ttl=LIVE_VIEW_TTL,
    )
    return {
        "url": url,
        "source": "internal",
        "session_id": session_id,
    }


def _allocate_live_view_session(data: dict, start_url: str, objective: str, max_steps: int, user_id: int) -> dict:
    direct_url = str(data.get("live_view_url") or "").strip()
    if direct_url.startswith("http://") or direct_url.startswith("https://"):
        return {"url": direct_url, "source": "manual"}
    allocator_url = str(
        data.get("live_view_allocator_url")
        or _config_get("AI_EXPLORE_LIVE_VIEW_ALLOCATOR_URL")
        or ""
    ).strip()
    if not allocator_url:
        internal_session = _allocate_internal_live_view_session(start_url, objective, max_steps, user_id)
        if internal_session:
            return internal_session
        fallback_url = _resolve_live_view_url(data, start_url)
        return {"url": fallback_url, "source": "template"} if fallback_url else {}
    allocator_token = str(
        data.get("live_view_allocator_token")
        or _config_get("AI_EXPLORE_LIVE_VIEW_ALLOCATOR_TOKEN")
        or ""
    ).strip()
    timeout_seconds = int(
        data.get("live_view_allocator_timeout")
        or _config_get("AI_EXPLORE_LIVE_VIEW_ALLOCATOR_TIMEOUT")
        or 15
    )
    headers = {"Content-Type": "application/json"}
    if allocator_token:
        headers["Authorization"] = f"Bearer {allocator_token}"
    payload = {
        "start_url": start_url,
        "objective": objective,
        "max_steps": max_steps,
        "user_id": user_id,
        "requested_by": "fullscopetest-web-explorer",
    }
    # SSRF 防护：向 allocator 发起外呼前先做 URL 安全校验
    # （v1 校验失败时误把 error_response 当 dict 返回，调用方 .get('url') 直接
    # AttributeError → 500；平迁修正为走模板回退分支，安全语义不变）
    safe, reason = validate_url_safety(allocator_url)
    if not safe:
        logger.warning("live view allocator url failed safety check", reason=str(reason))
        fallback_url = _resolve_live_view_url(data, start_url)
        return {"url": fallback_url, "source": "template"} if fallback_url else {}
    try:
        import requests

        response = requests.post(
            allocator_url,
            json=payload,
            headers=headers,
            timeout=timeout_seconds,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"allocator http {response.status_code}")
        body = response.json() if response.text else {}
        url = str(body.get("url") or body.get("live_view_url") or "").strip()
        if not url:
            raise RuntimeError("allocator missing url")
        return {
            "url": url,
            "source": str(body.get("source") or "allocator"),
            "session_id": body.get("session_id"),
            "release_url": body.get("release_url"),
        }
    except Exception as exc:
        logger.warning("allocate live view session failed", error=str(exc))
        fallback_url = _resolve_live_view_url(data, start_url)
        return {"url": fallback_url, "source": "template"} if fallback_url else {}


def _release_live_view_session(session: dict):
    if not isinstance(session, dict):
        return
    session_id = str(session.get("session_id") or "").strip()
    # 从 SessionStore 删除会话记录
    if session_id:
        store = get_session_store()
        store.delete(f"{LIVE_VIEW_KEY_PREFIX}{session_id}")
    release_url = str(session.get("release_url") or "").strip()
    if not release_url and session_id:
        release_template = str(_config_get("AI_EXPLORE_LIVE_VIEW_RELEASE_URL") or "").strip()
        if release_template:
            release_url = release_template.replace("{session_id}", quote_plus(session_id)).replace(
                "{session_id_raw}", session_id
            )
    if not release_url:
        return
    timeout_seconds = int(_config_get("AI_EXPLORE_LIVE_VIEW_RELEASE_TIMEOUT") or 6)
    try:
        import requests

        response = requests.delete(release_url, timeout=timeout_seconds)
        if response.status_code >= 400:
            requests.post(release_url, timeout=timeout_seconds)
    except Exception as exc:
        logger.warning("release live view session failed", error=str(exc))


def _get_collection_or_404(collection_id: int, user_id: int):
    """按 id 取集合并做属主过滤（IDOR）：不存在/越权 → (None, 404 响应)"""
    query = filter_by_org_projects(select(WebTestCollection), WebTestCollection)
    collection = db.session.scalar(query.filter_by(id=collection_id, user_id=user_id))
    if not collection:
        return None, _error(404, "用例集不存在")
    return collection, None


# ==================== 健康检查 ====================

@router.get("/api/v1/web-test/health")
@release_session
def web_test_health():
    """Web 测试模块健康检查（公开端点，v1 无 @jwt_required，保持公开）"""
    return _success(message="Web 测试模块正常")


# ==================== AI 能力 ====================

@router.post("/api/v1/web-test/ai/generate")
@release_session
def generate_web_script(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """AI 生成 Web 测试脚本"""
    data = data or {}
    prompt = (data.get("prompt") or "").strip()

    if not prompt:
        return _error(400, "prompt is required")

    try:
        from app.api.routes.api_test import _build_ai_runtime_config

        runtime_config = _build_ai_runtime_config(data)

        script_content = generate_test_script(prompt, "web", runtime_config, user_id=user.id)
        return _success(data={"script_content": script_content}, message="AI 脚本生成成功")
    except Exception as exc:
        return _error(500, f"AI 脚本生成失败: {str(exc)}")


@router.post("/api/v1/web-test/ai/analyze-error")
@release_session
def analyze_web_test_error(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """AI 智能诊断测试错误并提供修复建议（IDOR：脚本属主过滤，越权 404）"""
    data = data or {}
    script_id = data.get("script_id")
    error_log = data.get("error_log")

    if not script_id or not error_log:
        return _error(400, "script_id and error_log are required")

    query = filter_by_org_projects(select(WebTestScript), WebTestScript)
    script = db.session.scalar(query.filter_by(id=script_id, user_id=user.id))
    if not script:
        return _error(404, "脚本不存在")

    try:
        from app.api.routes.api_test import _build_ai_runtime_config

        runtime_config = _build_ai_runtime_config(data)

        result = analyze_test_error(
            script_content=script.script_content,
            error_log=error_log,
            test_type="web",
            config=runtime_config,
        )
        return _success(data=result, message="AI 诊断完成")
    except Exception as exc:
        return _error(500, f"AI 诊断失败: {str(exc)}")


@router.post("/api/v1/web-test/ai/explore")
@release_session
def explore_web_app(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """AI 探索性测试 (Autonomous Web Explorer)；SSRF：start_url 先过 is_safe_url"""
    data = data or {}
    start_url = data.get("start_url")
    objective = data.get("objective", "尽可能多地点击不同页面并寻找报错")
    max_steps = int(data.get("max_steps", 10))

    if not start_url:
        return _error(400, "start_url is required")

    # SSRF 防护：校验目标 URL
    safe, reason = is_safe_url(start_url)
    if not safe:
        return _error(400, reason)

    try:
        runtime_config = _build_runtime_ai_config(data)

        report = run_exploration_task(start_url, max_steps, objective, runtime_config)

        return _success(data=report, message="AI 探索测试完成")
    except Exception as exc:
        return _error(500, f"AI 探索测试失败: {str(exc)}")


@router.post("/api/v1/web-test/ai/explore/stream")
@release_session
def explore_web_app_stream(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """AI 探索性测试（SSE 流式）：SSRF 校验与 live view 分配先于流式响应"""
    data = data or {}
    user_id = user.id
    start_url = data.get("start_url")
    objective = data.get("objective", "尽可能多地点击不同页面并寻找报错")
    max_steps = int(data.get("max_steps", 10))

    if not start_url:
        return _error(400, "start_url is required")

    # SSRF 防护：校验目标 URL
    safe, reason = is_safe_url(start_url)
    if not safe:
        return _error(400, reason)

    runtime_config = _build_runtime_ai_config(data)
    live_view_session = _allocate_live_view_session(data, start_url, objective, max_steps, user_id)

    def generate():
        log_queue: Queue = Queue()
        progress_queue: Queue = Queue()
        state = {"report": None, "error": None}
        done_event = threading.Event()

        def push_log(line: str):
            log_queue.put(line)

        def push_progress(payload: dict):
            progress_queue.put(payload)

        def worker():
            try:
                state["report"] = run_exploration_task(
                    start_url=start_url,
                    max_steps=max_steps,
                    objective=objective,
                    config=runtime_config,
                    log_callback=push_log,
                    progress_callback=push_progress,
                )
            except Exception as exc:
                state["error"] = str(exc)
            finally:
                done_event.set()

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        if live_view_session.get("url"):
            yield _format_sse(
                "live_view",
                {
                    "url": live_view_session.get("url"),
                    "source": live_view_session.get("source") or "allocator",
                    "session_id": live_view_session.get("session_id"),
                },
            )
        yield _format_sse("log", {"line": f"探索任务已创建，目标 URL: {start_url}"})
        yield _format_sse("log", {"line": f"探索目标: {objective}"})
        yield _format_sse("log", {"line": f"最大步数: {max_steps}"})

        while not done_event.is_set() or not log_queue.empty() or not progress_queue.empty():
            try:
                line = log_queue.get(timeout=0.2)
                yield _format_sse("log", {"line": str(line)})
            except Empty:
                pass
            try:
                progress_payload = progress_queue.get_nowait()
                if isinstance(progress_payload, dict):
                    yield _format_sse("progress", progress_payload)
            except Empty:
                pass

        try:
            if state["error"]:
                yield _format_sse("error", {"message": state["error"]})
            else:
                yield _format_sse("report", state["report"] or {})
            yield _format_sse("done", {"ok": state["error"] is None})
        finally:
            _release_live_view_session(live_view_session)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ==================== 用例集管理 ====================

@router.get("/api/v1/web-test/collections")
@release_session
def get_web_collections(request: Request, user: User = Depends(_current_user)):
    """获取 Web 用例集列表（service 层按 user_id 过滤）"""
    user_id = user.id
    project_id = query_int(request, "project_id", 0) or None
    try:
        data = web_test_service.get_collections(user_id, project_id)
        return _success(data=data)
    except Exception as exc:
        logger.error("get web collections failed", error=str(exc))
        return _error(500, f"获取集合失败: {str(exc)}")


@router.post("/api/v1/web-test/collections")
@release_session
def create_web_collection(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """创建 Web 用例集"""
    user_id = user.id
    data = data or {}
    try:
        result = web_test_service.create_collection(user_id, data)
        return _success(data=result, message="创建成功")
    except ValidationError as exc:
        return _error(400, str(exc))
    except Exception as exc:
        logger.error("create web collection failed", error=str(exc))
        return _error(500, f"创建集合失败: {str(exc)}")


@router.put("/api/v1/web-test/collections/{collection_id}")
@release_session
def update_web_collection(
    collection_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """更新 Web 用例集（service 层按 user_id 过滤，越权 404）"""
    user_id = user.id
    data = data or {}
    try:
        result = web_test_service.update_collection(collection_id, user_id, data)
        return _success(data=result, message="更新成功")
    except NotFoundError as exc:
        return _error(404, str(exc))
    except Exception as exc:
        logger.error("update web collection failed", error=str(exc))
        return _error(500, f"更新集合失败: {str(exc)}")


@router.delete("/api/v1/web-test/collections/{collection_id}")
@release_session
def delete_web_collection(collection_id: int, user: User = Depends(_current_user)):
    """删除 Web 用例集（service 层按 user_id 过滤，越权 404）"""
    user_id = user.id
    try:
        web_test_service.delete_collection(collection_id, user_id)
        return _success(message="删除成功")
    except NotFoundError as exc:
        return _error(404, str(exc))
    except Exception as exc:
        logger.error("delete web collection failed", error=str(exc))
        return _error(500, f"删除集合失败: {str(exc)}")


@router.post("/api/v1/web-test/collections/{collection_id}/run")
@release_session
def run_web_collection(collection_id: int, user: User = Depends(_current_user)):
    """批量运行用例集内脚本（逐脚本异步提交；集合与脚本均按属主过滤，越权 404）"""
    user_id = user.id
    collection, err = _get_collection_or_404(collection_id, user_id)
    if err:
        return err

    scripts = db.session.scalars(select(WebTestScript).filter_by(
        user_id=user_id,
        collection_id=collection.id,
        is_enabled=True,
    )).all()
    if not scripts:
        return _error(400, "用例集内没有可执行脚本")

    submitted = []
    skipped = []

    for script in scripts:
        if script.status == "running":
            skipped.append({"script_id": script.id, "reason": "running"})
            continue
        try:
            task = run_web_test_task.apply_async(
                args=[script.id, user_id],
                task_id=f"web_test_{script.id}_{user_id}",
            )
            script.status = "running"
            script.last_status = "running"
            script.last_run_at = datetime.now(timezone.utc).replace(tzinfo=None)
            submitted.append({"script_id": script.id, "task_id": task.id})
        except Exception as exc:
            skipped.append({"script_id": script.id, "reason": str(exc)})

    db.session.commit()

    return _success(
        data={
            "collection_id": collection.id,
            "collection_name": collection.name,
            "submitted_count": len(submitted),
            "submitted": submitted,
            "skipped": skipped,
        },
        message="批量提交完成",
    )


# ==================== 脚本管理 ====================

@router.get("/api/v1/web-test/scripts")
@release_session
def get_scripts(request: Request, user: User = Depends(_current_user)):
    """获取 Web 测试脚本列表（service 层按 user_id 过滤）"""
    user_id = user.id
    project_id = query_int(request, "project_id", 0) or None
    collection_id = query_int(request, "collection_id", 0) or None
    try:
        data = web_test_service.get_scripts(user_id, collection_id, project_id)
        return _success(data=data)
    except Exception as exc:
        logger.error("get web scripts failed", error=str(exc))
        return _error(500, f"获取脚本失败: {str(exc)}")


# 默认的 Playwright 脚本模板（与 v1 完全一致，含空行的行尾空格）
DEFAULT_SCRIPT_CODE = '''"""
Playwright 自动化测试脚本
"""
from playwright.sync_api import sync_playwright, expect
from fst_vision import assert_snapshot  # 导入视觉回归测试断言
from sqlalchemy import select

def run():
    with sync_playwright() as p:
        # 启动浏览器
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
\u0020\u0020\u0020\u0020\u0020\u0020\u0020\u0020
        # 访问页面
        page.goto("https://example.com")
\u0020\u0020\u0020\u0020\u0020\u0020\u0020\u0020
        # 获取标题
        title = page.title()
        print(f"页面标题: {title}")
\u0020\u0020\u0020\u0020\u0020\u0020\u0020\u0020
        # 视觉回归测试 (第一次运行会生成基线图片，后续运行会进行像素级对比)
        assert_snapshot(page, "example_homepage", mismatch_tolerance=0.01)
\u0020\u0020\u0020\u0020\u0020\u0020\u0020\u0020
        # 关闭浏览器
        browser.close()
\u0020\u0020\u0020\u0020\u0020\u0020\u0020\u0020
        return {"status": "success", "title": title}

if __name__ == "__main__":
    result = run()
    print(result)
'''


@router.post("/api/v1/web-test/scripts")
@release_session
def create_script(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """创建 Web 测试脚本（collection_id 带属主过滤，越权 404）"""
    user_id = user.id
    data = data or {}

    error = validate_required(data, ["name"])
    if error:
        return _error(400, error)

    project_id = data.get("project_id")
    collection_id = data.get("collection_id")
    if collection_id is not None:
        collection, err = _get_collection_or_404(collection_id, user_id)
        if err:
            return err
        if collection.project_id and project_id and collection.project_id != project_id:
            return _error(400, "collection_id 与 project_id 不匹配")
        if project_id is None:
            project_id = collection.project_id
    else:
        collection = None

    script = WebTestScript(
        name=data["name"],
        description=data.get("description", ""),
        script_content=data.get("script_content", DEFAULT_SCRIPT_CODE),
        target_url=data.get("target_url", ""),
        browser=data.get("browser", "chromium"),
        headless=data.get("headless", True),
        timeout=data.get("timeout", 30000),
        collection_id=collection.id if collection else None,
        project_id=project_id,
        user_id=user_id,
    )

    db.session.add(script)
    db.session.commit()

    return _success(data=script.to_dict(), message="创建成功")


@router.get("/api/v1/web-test/scripts/{script_id}")
@release_session
def get_script(script_id: int, user: User = Depends(_current_user)):
    """获取脚本详情（service 层按 user_id 过滤，越权 404）"""
    user_id = user.id
    try:
        result = web_test_service.get_script(script_id, user_id)
        return _success(data=result)
    except NotFoundError as exc:
        return _error(404, str(exc))
    except Exception as exc:
        logger.error("get web script failed", error=str(exc))
        return _error(500, f"获取脚本失败: {str(exc)}")


@router.put("/api/v1/web-test/scripts/{script_id}")
@release_session
def update_script(
    script_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """更新 Web 测试脚本（service 层按 user_id 过滤，越权 404）"""
    user_id = user.id
    data = data or {}
    try:
        result = web_test_service.update_script(script_id, user_id, data)
        return _success(data=result, message="更新成功")
    except NotFoundError as exc:
        return _error(404, str(exc))
    except Exception as exc:
        logger.error("update web script failed", error=str(exc))
        return _error(500, f"更新脚本失败: {str(exc)}")


@router.delete("/api/v1/web-test/scripts/{script_id}")
@release_session
def delete_script(script_id: int, user: User = Depends(_current_user)):
    """删除 Web 测试脚本（service 层按 user_id 过滤，越权 404）"""
    user_id = user.id
    try:
        web_test_service.delete_script(script_id, user_id)
        return _success(message="删除成功")
    except NotFoundError as exc:
        return _error(404, str(exc))
    except Exception as exc:
        logger.error("delete web script failed", error=str(exc))
        return _error(500, f"删除脚本失败: {str(exc)}")


# ==================== 执行脚本 ====================

@router.get("/api/v1/web-test/scripts/{script_id}/snapshots/{image_type}/{snapshot_name}")
@release_session
def get_snapshot_image(
    script_id: int,
    image_type: str,
    snapshot_name: str,
    user: User = Depends(_current_user),
):
    """获取视觉回归测试截图 (baseline, actual, diff)；IDOR：按脚本 user_id 过滤"""
    user_id = user.id
    script = db.session.scalar(select(WebTestScript).filter_by(id=script_id, user_id=user_id))

    if not script:
        return _error(404, "脚本不存在")

    if image_type not in ["baseline", "actual", "diff"]:
        return _error(400, "无效的图片类型")

    if not snapshot_name.endswith(".png"):
        snapshot_name += ".png"

    # 路径穿越防护（等价 v1 send_from_directory 的 NotFound 行为：
    # 单段文件名 + realpath 必须落在 image_dir 内）
    if os.path.basename(snapshot_name) != snapshot_name:
        return _error(404, "图片不存在")

    work_dir = os.path.join(os.path.dirname(_app_root_path()), "data", "web_tests", str(script_id))
    image_dir = os.path.join(work_dir, "snapshots", image_type)

    image_path = os.path.join(image_dir, snapshot_name)
    if not os.path.exists(image_path):
        return _error(404, "图片不存在")

    try:
        real_dir = os.path.realpath(image_dir)
        real_path = os.path.realpath(image_path)
        if os.path.commonpath([real_dir, real_path]) != real_dir:
            return _error(404, "图片不存在")
    except (ValueError, OSError):
        return _error(404, "图片不存在")

    return FileResponse(path=image_path, media_type="image/png", filename=snapshot_name)


def _app_root_path() -> str:
    """等价原 Flask current_app.root_path（app 包目录，零 Flask：由 __file__ 推导，parents[2]=app）"""
    return str(Path(__file__).resolve().parents[2])


@router.post("/api/v1/web-test/scripts/{script_id}/run")
@release_session
def run_script(script_id: int, user: User = Depends(_current_user)):
    """运行 Web 测试脚本（异步 Celery 任务；IDOR：按脚本 user_id 过滤，越权 404）"""
    user_id = user.id
    script = db.session.scalar(select(WebTestScript).filter_by(id=script_id, user_id=user_id))

    if not script:
        return _error(404, "脚本不存在")

    # 检查是否已在运行
    if script.status == "running":
        return _error(400, "脚本正在运行中")

    try:
        # 异步执行测试任务
        task = run_web_test_task.apply_async(
            args=[script_id, user_id],
            task_id=f"web_test_{script_id}_{user_id}",
        )

        # 提交成功后立即更新为 running，前端可及时感知状态
        script.status = "running"
        script.last_status = "running"
        script.last_run_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.session.commit()

        return _success(
            data={
                "message": "测试已提交，正在后台执行",
                "task_id": task.id,
                "script_id": script_id,
            }
        )

    except Exception as e:
        return _error(500, f"提交失败: {str(e)}")


# ==================== Playwright 录制 ====================

@router.post("/api/v1/web-test/record/start")
@release_session
def start_recording(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """
    启动 Playwright 录制模式

    注意：这需要在本地环境运行，远程服务器可能不支持
    SSRF 防护：is_safe_url 校验先于 Popen 启动 playwright codegen
    """
    user_id = user.id
    data = data or {}
    url = data.get("url", "https://example.com")
    browser = data.get("browser", "chromium")

    # SSRF 防护：校验目标 URL（必须在 Popen 之前）
    safe, reason = is_safe_url(url)
    if not safe:
        return _error(400, reason)

    # 检查是否已有录制进程在运行（优先查 SessionStore 元数据）
    rec_key = f"{RECORDING_KEY_PREFIX}{user_id}"
    store = get_session_store()
    existing_rec = store.get(rec_key)
    if existing_rec:
        # 检查本地进程对象是否仍在运行
        local_proc = _recording_process_objects.get(user_id)
        if local_proc is not None and local_proc.poll() is None:
            return _error(400, "已有录制进程在运行，请先停止")
        # 本地进程已结束或不存在，清理残留记录
        store.delete(rec_key)
        _recording_process_objects.pop(user_id, None)

    try:
        # 获取当前 Python 解释器路径（支持虚拟环境）
        python_path = sys.executable

        # 构建命令
        cmd = [python_path, "-m", "playwright", "codegen"]

        # 添加浏览器参数
        if browser != "chromium":
            cmd.extend(["--browser", browser])

        # 添加目标 URL
        cmd.append(url)

        # 启动 codegen（不使用 PIPE，避免缓冲区问题导致进程退出）
        # 使用 DEVNULL 忽略输出，或者不捕获输出让其显示在控制台
        if sys.platform == "win32":
            # Windows: 创建新的控制台窗口，不捕获输出
            process = subprocess.Popen(
                cmd,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
                # 不使用 PIPE，让输出显示在新控制台
                stdout=None,
                stderr=None,
            )
        else:
            # Linux/Mac: 使用 DEVNULL 或不捕获
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        # 等待一小段时间确认进程启动成功
        time.sleep(1)
        if process.poll() is not None:
            return _error(500, "录制器启动失败，进程立即退出，请检查 Playwright 是否正确安装")

        # 保存进程元数据到 SessionStore（TTL 1 小时）
        store.set(
            rec_key,
            {
                "user_id": user_id,
                "pid": process.pid,
                "browser": browser,
                "url": url,
                "started_at": time.time(),
            },
            ttl=RECORDING_TTL,
        )
        # 本地持有进程对象引用（subprocess 无法序列化）
        _recording_process_objects[user_id] = process

        return _success(
            data={
                "message": "录制器已启动，请在打开的浏览器窗口中进行操作",
                "pid": process.pid,
                "browser": browser,
                "url": url,
            }
        )

    except FileNotFoundError:
        return _error(500, "Playwright 未安装，请先运行: pip install playwright && playwright install")
    except Exception as e:
        return _error(500, f"启动录制失败: {str(e)}")


@router.post("/api/v1/web-test/record/stop")
@release_session
def stop_recording(user: User = Depends(_current_user)):
    """停止 Playwright 录制"""
    user_id = user.id

    # 先查 SessionStore 元数据，确认是否有录制
    rec_key = f"{RECORDING_KEY_PREFIX}{user_id}"
    store = get_session_store()
    if not store.exists(rec_key):
        return _error(400, "没有正在运行的录制进程")

    process = _recording_process_objects.get(user_id)

    try:
        # 终止进程
        if process is not None:
            process.terminate()
            process.wait(timeout=5)

        # 清理：本地进程对象 + SessionStore 元数据
        _recording_process_objects.pop(user_id, None)
        store.delete(rec_key)

        return _success(message="录制已停止")

    except Exception as e:
        return _error(500, f"停止录制失败: {str(e)}")


@router.get("/api/v1/web-test/record/status")
@release_session
def recording_status(user: User = Depends(_current_user)):
    """获取录制状态"""
    user_id = user.id
    rec_key = f"{RECORDING_KEY_PREFIX}{user_id}"
    store = get_session_store()
    rec_meta = store.get(rec_key)

    if not rec_meta:
        return _success(
            data={
                "is_recording": False,
                "python_path": sys.executable,
            }
        )

    # 通过本地进程对象检查实际运行状态
    process = _recording_process_objects.get(user_id)
    is_running = process is not None and process.poll() is None

    if not is_running:
        # 进程已结束，获取退出码并清理
        exit_code = process.returncode if process is not None else None
        _recording_process_objects.pop(user_id, None)
        store.delete(rec_key)

        return _success(
            data={
                "is_recording": False,
                "exit_code": exit_code,
                "message": f"进程已退出，退出码: {exit_code}",
                "python_path": sys.executable,
            }
        )

    return _success(
        data={
            "is_recording": is_running,
            "pid": process.pid,
            "python_path": sys.executable,
        }
    )
