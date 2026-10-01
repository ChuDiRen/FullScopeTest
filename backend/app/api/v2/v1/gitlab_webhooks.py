"""
GitLab Webhook 接收模块 - FastAPI 平迁（自 app/api/webhooks/gitlab.py）

v1 中该模块经 app/api/__init__.py 的 `from .webhooks import gitlab` 挂载到
api_bp（url_prefix=/api/v1），路由为 POST /api/v1/webhooks/gitlab。

路径/方法/状态码/响应字段与原 v1 完全一致。

鉴权映射：
- 公开端点（v1 即无 @jwt_required，保持公开并注明）：
  * POST /api/v1/webhooks/gitlab — GitLab 服务器回调，无法携带用户 JWT；
    安全边界与 v1 一致：配置 GITLAB_WEBHOOK_SECRET 时校验 X-Gitlab-Token
    的 HMAC-SHA256 签名。

会话生命周期：
- 同步端点统一加 @release_session（deps.release_session），防 NullPool 连接泄漏

安全说明：
- 签名校验在读取原始请求体上进行（经 async 依赖在事件循环线程读取），
  与 v1 的 request.get_data() 语义一致

其他必要修正：
- v1 的 _handle_push_event/_handle_merge_request_event 创建 TestRun 时传了
  name/trigger_source/trigger_metadata/created_by_user_id —— TestRun 模型
  并无这些列（v1 实际命中触发规则时必然 500 "invalid keyword argument"）。
  平迁时映射到真实列：name → test_object_name，trigger_source → triggered_by("ci")，
  元数据并入 test_object_name 语义，保留成功响应 {"test_run_id","triggered_by"} 契约。
"""

import hashlib
import hmac
import json
from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..deps import release_session
from ....core.logging import get_logger
from ....extensions import db
from ....models.project import Project
from ....models.test_run import TestRun
from ....services.trigger_rule_service import evaluate_push_event, evaluate_pr_event
from sqlalchemy import select

logger = get_logger(__name__)

router = APIRouter(tags=["gitlab-webhooks"])


# ---------------------------------------------------------------------------
# 响应构造 / 原始请求体
# ---------------------------------------------------------------------------

def _request_id() -> str:
    """读取 RequestContextMiddleware 写入 scope state 的 request_id"""
    try:
        from ....core.runtime import ctx

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
            "timestamp": datetime_now_iso(),
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
            "timestamp": datetime_now_iso(),
        },
    )


def datetime_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat() + "Z"


async def _raw_body(request: Request) -> bytes:
    """读取原始请求体（同步端点无法 await，经依赖在事件循环线程完成读取，供签名校验用）"""
    return await request.body()


def _verify_gitlab_signature(payload: bytes, signature: str, secret: str) -> bool:
    """校验 X-Gitlab-Token 携带的 HMAC-SHA256 签名（与 v1 一致）"""
    if not signature or not secret:
        return False
    if signature.startswith("sha256="):
        signature = signature[7:]
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


# ==================== GitLab Webhook 回调（公开端点） ====================

@router.post("/api/v1/webhooks/gitlab")
@release_session
def gitlab_webhook(request: Request, raw_body: bytes = Depends(_raw_body)):
    """
    处理 GitLab push / merge request webhook（公开端点：v1 无鉴权，保持公开）。

    配置了 GITLAB_WEBHOOK_SECRET 时校验 X-Gitlab-Token 签名，缺失/不匹配 → 401。
    """
    from ....core.runtime import get_config

    webhook_secret = get_config().get("GITLAB_WEBHOOK_SECRET", "")
    if webhook_secret:
        signature = request.headers.get("X-Gitlab-Token", "")
        if not signature:
            return _error(401, "Missing signature")
        if not _verify_gitlab_signature(raw_body or b"", signature, webhook_secret):
            return _error(401, "Signature verification failed")

    event_type = request.headers.get("X-Gitlab-Event", "")
    try:
        payload = json.loads(raw_body.decode("utf-8")) if raw_body else {}
    except (ValueError, UnicodeDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    try:
        if event_type == "Push Hook":
            return _handle_push_event(payload)
        elif event_type == "Merge Request Hook":
            return _handle_merge_request_event(payload)
        else:
            return _success(message=f"Event {event_type} ignored")
    except Exception as exc:
        logger.error("Failed to handle GitLab webhook", error=str(exc))
        return _error(500, str(exc))


def _handle_push_event(payload: Dict[str, Any]):
    project = payload.get("project", {})
    ref = payload.get("ref", "")
    commits = payload.get("commits", [])
    changed_files = []
    for commit in commits:
        changed_files.extend(commit.get("added", []))
        changed_files.extend(commit.get("modified", []))
        changed_files.extend(commit.get("removed", []))

    repo_name = project.get("path_with_namespace", "")
    project_obj = _find_or_create_project(repo_name)
    if not project_obj:
        return _error(500, "Failed to find or create project")

    trigger_result = evaluate_push_event(
        ref=ref,
        changed_files=changed_files,
        commit_message=commits[0].get("message", "") if commits else "",
        repository=repo_name,
        project_id=project_obj.id,
    )
    if not trigger_result.get("should_trigger"):
        return _success(message="No trigger matched")

    test_run = TestRun(
        project_id=project_obj.id,
        test_object_name="Push Test - " + repo_name,
        test_type=trigger_result.get("test_type", "api"),
        status="pending",
        triggered_by="ci",
    )
    db.session.add(test_run)
    db.session.commit()
    _schedule_test_execution(test_run, trigger_result)
    return _success(data={"test_run_id": test_run.id, "triggered_by": "push"})


def _handle_merge_request_event(payload: Dict[str, Any]):
    attrs = payload.get("object_attributes", {})
    action = attrs.get("action", "")
    project = payload.get("project", {})
    if action not in ("open", "update", "reopen"):
        return _success(message=f"Ignored action: {action}")

    repo_name = project.get("path_with_namespace", "")
    project_obj = _find_or_create_project(repo_name)
    if not project_obj:
        return _error(500, "Failed to find or create project")

    trigger_result = evaluate_pr_event(
        action=action,
        head_branch=attrs.get("source_branch", ""),
        base_branch=attrs.get("target_branch", ""),
        pr_number=attrs.get("iid", 0),
        pr_title=attrs.get("title", ""),
        repository=repo_name,
        changed_files=[],
        project_id=project_obj.id,
    )
    if not trigger_result.get("should_trigger"):
        return _success(message="No trigger matched")

    mr_iid = attrs.get("iid", 0)
    mr_title = attrs.get("title", "")
    test_run = TestRun(
        project_id=project_obj.id,
        test_object_name="MR !" + str(mr_iid) + " Test - " + mr_title,
        test_type=trigger_result.get("test_type", "api"),
        status="pending",
        triggered_by="ci",
    )
    db.session.add(test_run)
    db.session.commit()
    _schedule_test_execution(test_run, trigger_result)
    return _success(data={"test_run_id": test_run.id, "triggered_by": "merge_request"})


def _find_or_create_project(repo_name: str):
    """按仓库名查找（owner_id=1 的自动项目）或创建项目（与 v1 一致）"""
    name = repo_name.split("/")[-1] if "/" in repo_name else repo_name
    project = db.session.scalar(select(Project).filter_by(name=name, owner_id=1))
    if project:
        return project
    project = Project(
        name=name,
        description=f"Auto-created from GitLab: {repo_name}",
        owner_id=1,
    )
    db.session.add(project)
    db.session.commit()
    return project


def _schedule_test_execution(test_run, trigger_result):
    """按触发结果调度测试执行（Celery 不可用时静默降级，与 v1 一致）"""
    try:
        from ....tasks import run_api_collection_task

        test_type = trigger_result.get("test_type", "api")
        target_id = trigger_result.get("target_id")
        if test_type == "api" and target_id:
            task = run_api_collection_task.delay(
                collection_id=target_id, environment_id=None, test_run_id=test_run.id
            )
            test_run.celery_task_id = task.id
            db.session.commit()
            logger.info(
                "Scheduled API test", test_run_id=test_run.id, task_id=task.id
            )
    except Exception as exc:
        logger.error("Failed to schedule test", error=str(exc))
