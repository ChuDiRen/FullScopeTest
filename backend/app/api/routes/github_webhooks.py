"""
GitHub Webhook 接收端点（/api/v1/webhooks/github，公开）

背景：v1 中 app/api/webhooks/github.py 从未被 import，路由从未注册（死代码）；
GitLab 对应端点却在线。本模块把它补齐为可用实现，并修复 v1 源码中使用
TestRun 不存在列（trigger_source/trigger_metadata/created_by_user_id）导致的
必然 500 —— 映射到真实列：triggered_by / triggered_user_id（元数据记入日志）。

安全：配置 GITHUB_WEBHOOK_SECRET 时强制校验 X-Hub-Signature-256（HMAC-SHA256）。
"""

import hashlib
import hmac
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.extensions import db
from app.models.github_integration import GitHubIntegration
from app.models.test_run import TestRun
from app.models.project import Project
from app.core.logging import get_logger
from app.api.v2.deps import release_session
from sqlalchemy import select

logger = get_logger(__name__)

router = APIRouter(tags=["v1-github-webhooks"])


def _response(code: int, message: str = "", data=None) -> JSONResponse:
    """复刻 app/utils/response.py 信封"""
    import datetime

    body = {"code": code, "message": message, "timestamp": datetime.datetime.now().isoformat()}
    if data is not None:
        body["data"] = data
    return JSONResponse(status_code=200 if code == 200 else code, content=body)


def _verify_github_signature(payload: bytes, signature: str, secret: str) -> bool:
    if not signature or not secret:
        return False
    if signature.startswith("sha256="):
        signature = signature[7:]
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def _find_integration_for_repo(full_name: str):
    integrations = db.session.scalars(select(GitHubIntegration).filter_by(is_active=True)).all()
    for integration in integrations:
        if integration._is_token_valid():
            return integration
    return None


@router.post("/api/v1/webhooks/github")
@release_session
async def github_webhook(request: Request):
    """GitHub webhook 回调（公开；配置了 GITHUB_WEBHOOK_SECRET 时验签）"""
    from app.core.runtime import get_config

    webhook_secret = get_config().get("GITHUB_WEBHOOK_SECRET", "")
    raw = await request.body()
    if webhook_secret:
        signature = request.headers.get("X-Hub-Signature-256", "")
        if not signature:
            logger.warning("GitHub webhook missing signature")
            return _response(401, "Missing signature")
        if not _verify_github_signature(raw, signature, webhook_secret):
            logger.warning("GitHub webhook signature verification failed")
            return _response(401, "Signature verification failed")

    event_type = request.headers.get("X-GitHub-Event", "")
    delivery_id = request.headers.get("X-GitHub-Delivery", "")
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    logger.info(
        "GitHub webhook received",
        event_type=event_type,
        delivery_id=delivery_id,
        action=payload.get("action"),
    )
    try:
        if event_type == "push":
            return _handle_push_event(payload)
        if event_type == "pull_request":
            return _handle_pull_request_event(payload)
        if event_type == "ping":
            return _response(200, "pong")
        return _response(200, f"Event {event_type} ignored")
    except Exception as exc:
        logger.error("Failed to handle GitHub webhook", error=str(exc))
        return _response(500, str(exc))


def _handle_push_event(payload: dict):
    repo = payload.get("repository", {})
    head_commit = payload.get("head_commit", {})

    changed_files = []
    for commit in payload.get("commits", []):
        changed_files.extend(commit.get("added", []))
        changed_files.extend(commit.get("modified", []))
        changed_files.extend(commit.get("removed", []))

    integration = _find_integration_for_repo(repo.get("full_name", ""))
    if not integration:
        return _response(200, "No integration found")

    from app.services.trigger_rule_service import evaluate_push_event

    trigger_result = evaluate_push_event(
        ref=payload.get("ref", ""),
        changed_files=changed_files,
        commit_message=head_commit.get("message", ""),
        repository=repo.get("full_name", ""),
    )
    if not trigger_result.get("should_trigger"):
        return _response(200, "No trigger matched")

    test_run = _create_test_run(
        payload=payload,
        trigger_result=trigger_result,
        integration=integration,
        name=f'Push Test - {repo.get("full_name", "")}',
        triggered_by="github_push",
        metadata={
            "repository": repo.get("full_name"),
            "ref": payload.get("ref"),
            "commit_sha": head_commit.get("id"),
            "commit_message": head_commit.get("message"),
        },
    )
    return _response(
        200,
        data={"test_run_id": test_run.id if test_run else None, "triggered_by": "push"},
    )


def _handle_pull_request_event(payload: dict):
    action = payload.get("action", "")
    pr = payload.get("pull_request", {})
    repo = payload.get("repository", {})

    if action not in ("opened", "synchronize", "reopened"):
        return _response(200, f"Ignored action: {action}")

    integration = _find_integration_for_repo(repo.get("full_name", ""))
    if not integration:
        return _response(200, "No integration found")

    from app.services.trigger_rule_service import evaluate_pr_event

    trigger_result = evaluate_pr_event(
        action=action,
        head_branch=pr.get("head", {}).get("ref", ""),
        base_branch=pr.get("base", {}).get("ref", ""),
        pr_number=pr.get("number"),
        pr_title=pr.get("title", ""),
        repository=repo.get("full_name", ""),
        changed_files=[],
    )
    if not trigger_result.get("should_trigger"):
        return _response(200, "No trigger matched")

    test_run = _create_test_run(
        payload=payload,
        trigger_result=trigger_result,
        integration=integration,
        name=f'PR #{pr.get("number")} Test',
        triggered_by="github_pr",
        metadata={
            "repository": repo.get("full_name"),
            "pr_number": pr.get("number"),
            "pr_title": pr.get("title"),
            "head_branch": pr.get("head", {}).get("ref"),
            "base_branch": pr.get("base", {}).get("ref"),
        },
    )
    return _response(
        200,
        data={
            "test_run_id": test_run.id if test_run else None,
            "triggered_by": "pull_request",
        },
    )


def _create_test_run(payload, trigger_result, integration, name, triggered_by, metadata):
    """创建 TestRun 并调度执行（列名对齐 TestRun 模型真实字段）"""
    repo = payload.get("repository", {})
    project = _find_or_create_project(repo.get("full_name", ""), integration)
    if not project:
        return None

    logger.info(
        "github webhook trigger metadata",
        triggered_by=triggered_by,
        **{k: v for k, v in metadata.items() if v is not None},
    )

    test_run = TestRun(
        project_id=project.id,
        name=name,
        test_type=trigger_result.get("test_type", "api"),
        status="pending",
        triggered_by=triggered_by,
        triggered_user_id=integration.user_id,
    )
    db.session.add(test_run)
    db.session.commit()
    _schedule_test_execution(test_run, trigger_result)
    return test_run


def _find_or_create_project(repo_name: str, integration):
    project = db.session.scalar(select(Project).filter_by(
        github_repository=repo_name,
        owner_id=integration.user_id,
    ))
    if project:
        return project

    repo_parts = repo_name.split("/")
    project_name = repo_parts[-1] if len(repo_parts) > 1 else repo_name
    project = Project(
        name=project_name,
        description=f"Auto-created from {repo_name}",
        owner_id=integration.user_id,
        github_repository=repo_name,
    )
    db.session.add(project)
    db.session.commit()
    return project


def _schedule_test_execution(test_run, trigger_result):
    try:
        from app.tasks import run_api_collection_task

        test_type = trigger_result.get("test_type", "api")
        target_id = trigger_result.get("target_id")
        if test_type == "api" and target_id:
            task = run_api_collection_task.delay(
                collection_id=target_id,
                environment_id=None,
                test_run_id=test_run.id,
            )
            test_run.celery_task_id = task.id
            db.session.commit()
            logger.info("Scheduled API test", test_run_id=test_run.id, task_id=task.id)
    except Exception as exc:
        logger.error("Failed to schedule test", error=str(exc))
