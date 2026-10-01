"""
触发器与定时任务模块 (CI/CD) - FastAPI 平迁（自 app/api/triggers.py，蓝图挂载前缀 /api/v1）

路径/方法/状态码/响应字段与原 v1 完全一致（/api/v1/webhooks、
/api/v1/trigger-rules、/api/v1/triggers/{token}、/api/v1/schedules），
前端与 CI 脚本零改动。全部端点为同步 def，运行于 RequestContextMiddleware
push 的 app context 内，直接复用 数据库会话（app/database.py ContextVar 作用域） 与 services 层。

鉴权映射：
- @jwt_required() → Depends(_current_user)（内部复用 deps.get_current_user）
- 公开端点（v1 即无鉴权，保持公开并注明）：
  * POST/GET /api/v1/triggers/{token} — Webhook Token 触发执行（CI/CD 回调入口，
    靠 token 唯一性 + 可选 HMAC 签名保护，无 JWT）

会话生命周期：
- 同步端点统一加 @release_session（deps.release_session）
- 鉴权依赖包装为 async _current_user（与 api_test.py/reports.py 范例一致）

IDOR 修复（沿用源文件已有的属主校验 + 补齐同类缺口，越权不泄露资源存在性）：
- PUT/DELETE /trigger-rules/{id}：按 created_by 过滤（源文件已修复，越权/不存在 404）
- POST /trigger-rules：create 校验 project 归属（源文件已修复，403）
- GET /trigger-rules：补 created_by 过滤（v1 仅按 project_id，可枚举他人规则）
- GET /webhooks：补 project 归属校验（越权 404，与 mock-servers 列表一致）
- POST /webhooks：补 project 归属校验（防止向他人项目注入 Webhook，403）
- GET /schedules：补 project 归属校验（越权 404）
- POST /schedules：补 project 归属校验（防止向他人项目注入定时任务，403）
- DELETE /webhooks、PUT/DELETE /schedules：project.owner_id 校验（源文件已有，403）
"""

import uuid
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..deps import get_current_user, json_body, query_int, release_session
from ....core.logging import get_logger
from ....extensions import db
from ....models.project import Project
from ....models.scheduled_task import ScheduledTask
from ....models.trigger_rule import TriggerRule
from ....models.user import User
from ....models.webhook_token import WebhookToken
from ....utils.security import verify_hmac_signature
from sqlalchemy import select

logger = get_logger(__name__)

router = APIRouter(tags=["triggers"])


# ---------------------------------------------------------------------------
# 鉴权包装 / 响应构造（等价 app/utils/response.py 的 JSON 结构）
# ---------------------------------------------------------------------------

async def _current_user(request: Request) -> User:
    """鉴权依赖：在事件循环线程内执行同步 get_current_user，session 随 app context 回收"""
    return get_current_user(request)


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


def _owned_project(user_id: int, project_id: Any):
    """按 id 取项目并校验属主：不存在/越权一律 None"""
    try:
        pid = int(project_id)
    except (TypeError, ValueError):
        return None
    return db.session.scalar(select(Project).filter_by(id=pid, owner_id=user_id))


# ==================== Webhook 触发器 ====================

@router.get("/api/v1/webhooks")
@release_session
def get_webhooks(request: Request, user: User = Depends(_current_user)):
    """获取项目的 Webhook 列表（IDOR 修复：补 project 归属校验，越权 404）"""
    project_id = query_int(request, "project_id", 0)
    if not project_id:
        return _error(400, "缺少 project_id 参数")

    # IDOR 修复：v1 仅按 project_id 过滤，可枚举他人项目的 Webhook
    if not _owned_project(user.id, project_id):
        return _error(404, "项目不存在")

    webhooks = db.session.scalars(select(WebhookToken).filter_by(project_id=project_id)).all()
    return _success(data=[w.to_dict() for w in webhooks])


@router.post("/api/v1/webhooks")
@release_session
def create_webhook(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """创建 Webhook（IDOR 修复：补 project 归属校验，防止向他人项目注入）"""
    data = data or {}
    project_id = data.get("project_id")
    name = data.get("name")
    target_type = data.get("target_type")
    target_id = data.get("target_id")

    if not all([project_id, name, target_type, target_id]):
        return _error(400, "参数不完整")

    # IDOR 修复：v1 未校验项目归属，可向他人项目注入 Webhook（与 trigger-rules create 同类校验）
    if not _owned_project(user.id, project_id):
        return _error(403, "无权在该项目下创建 Webhook")

    webhook = WebhookToken(
        project_id=project_id,
        name=name,
        target_type=target_type,
        target_id=target_id,
        token=uuid.uuid4().hex,
    )
    db.session.add(webhook)
    db.session.commit()
    return _success(data=webhook.to_dict(), message="Webhook 创建成功")


@router.delete("/api/v1/webhooks/{webhook_id}")
@release_session
def delete_webhook(webhook_id: int, user: User = Depends(_current_user)):
    """删除 Webhook（project.owner_id 校验，源文件已有）"""
    user_id = user.id
    webhook = db.session.get(WebhookToken, webhook_id)
    if not webhook:
        return _error(404, "Webhook 不存在")

    # 校验权限：Webhook 所属项目必须属于当前用户
    project = db.session.scalar(select(Project).filter_by(id=webhook.project_id, owner_id=user_id))
    if not project:
        return _error(403, "无权删除该 Webhook")

    db.session.delete(webhook)
    db.session.commit()
    return _success(message="Webhook 删除成功")


# ==================== 触发规则 ====================

@router.get("/api/v1/trigger-rules")
@release_session
def get_trigger_rules(request: Request, user: User = Depends(_current_user)):
    """获取项目的触发规则列表（IDOR 修复：补 created_by 过滤，只返回自己创建的规则）"""
    project_id = query_int(request, "project_id", 0)
    if not project_id:
        return _error(400, "缺少 project_id 参数")

    # IDOR 修复：v1 仅按 project_id 查询（get_rules_by_project），可枚举他人规则；
    # 平迁补 created_by 过滤（与 update/delete 的 created_by 校验同一属主边界）
    rules = (
        db.session.scalars(select(TriggerRule).filter_by(project_id=project_id, created_by=user.id).order_by(TriggerRule.created_at.desc())).all())
    return _success(data=[r.to_dict() for r in rules])


@router.post("/api/v1/trigger-rules")
@release_session
def create_trigger_rule(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """创建触发规则（project 归属校验，源文件已修复）"""
    from ....services.trigger_rule_service import create_rule

    data = data or {}
    user_id = user.id

    required_fields = ["project_id", "name", "trigger_event", "target_type"]
    if not all(data.get(f) for f in required_fields):
        return _error(400, "参数不完整")

    # 校验项目归属当前用户，防止向他人项目注入触发规则
    project = db.session.scalar(select(Project).filter_by(id=data.get("project_id"), owner_id=user_id))
    if not project:
        return _error(403, "无权在该项目下创建触发规则")

    rule = create_rule(
        project_id=data.get("project_id"),
        name=data.get("name"),
        trigger_event=data.get("trigger_event"),
        target_type=data.get("target_type"),
        description=data.get("description"),
        target_branches=data.get("target_branches"),
        target_tags=data.get("target_tags"),
        include_paths=data.get("include_paths"),
        exclude_paths=data.get("exclude_paths"),
        test_types=data.get("test_types"),
        tags=data.get("tags"),
        target_id=data.get("target_id"),
        created_by=user_id,
    )
    return _success(data=rule.to_dict(), message="触发规则创建成功")


@router.put("/api/v1/trigger-rules/{rule_id}")
@release_session
def update_trigger_rule(
    rule_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """更新触发规则（created_by 属主校验，源文件已修复，越权/不存在 404）"""
    from ....services.trigger_rule_service import update_rule

    data = data or {}
    user_id = user.id

    # 属主校验：只能更新自己创建的规则，查不到统一按 404 处理
    rule = db.session.scalar(select(TriggerRule).filter_by(id=rule_id, created_by=user_id))
    if not rule:
        return _error(404, "规则不存在")

    rule = update_rule(rule_id, **data)
    if not rule:
        return _error(404, "规则不存在")

    return _success(data=rule.to_dict(), message="触发规则更新成功")


@router.delete("/api/v1/trigger-rules/{rule_id}")
@release_session
def delete_trigger_rule(rule_id: int, user: User = Depends(_current_user)):
    """删除触发规则（created_by 属主校验，源文件已修复，越权/不存在 404）"""
    from ....services.trigger_rule_service import delete_rule

    user_id = user.id

    # 属主校验：只能删除自己创建的规则，查不到统一按 404 处理
    rule = db.session.scalar(select(TriggerRule).filter_by(id=rule_id, created_by=user_id))
    if not rule:
        return _error(404, "规则不存在")

    success = delete_rule(rule_id)
    if not success:
        return _error(404, "规则不存在")

    return _success(message="触发规则删除成功")


# 公开执行端点，不需要认证（v1 无 @jwt_required，保持公开并注明）

async def _raw_body(request: Request) -> bytes:
    """读取原始请求体（同步端点无法 await，经依赖在事件循环线程完成读取，供 HMAC 校验用）"""
    return await request.body()


@router.api_route("/api/v1/triggers/{token}", methods=["POST", "GET"])
@release_session
def trigger_webhook(token: str, request: Request, raw_body: bytes = Depends(_raw_body)):
    """
    通过 Webhook Token 触发执行（公开端点：v1 无鉴权，供 CI/CD 回调直接调用）。

    安全边界与 v1 一致：token 唯一性 + 可选 WEBHOOK_SECRET 的 HMAC 签名校验。
    """
    from ....core.runtime import get_config

    webhook = db.session.scalar(select(WebhookToken).filter_by(token=token))
    if not webhook:
        logger.warning("Webhook 触发失败: 无效的 Token")
        return _error(404, "无效的 Token")

    # 验证 HMAC 签名 (如果配置了密钥)
    webhook_secret = get_config().get("WEBHOOK_SECRET")
    if webhook_secret and request.method == "POST":
        signature = request.headers.get("X-Hub-Signature-256") or request.headers.get("X-Signature-256")
        if not signature:
            logger.warning("Webhook 触发失败: 缺少签名头")
            return _error(401, "缺少签名头")

        payload = raw_body or b""
        if not verify_hmac_signature(payload, signature, webhook_secret):
            logger.warning("Webhook 触发失败: 签名验证失败")
            return _error(401, "签名验证失败")

    # 根据 target_type 调用相应的执行逻辑
    try:
        from ....tasks import run_api_collection_task, run_web_collection_task, run_perf_scenario_task

        task = None
        if webhook.target_type == "api_collection":
            task = run_api_collection_task.delay(webhook.target_id, None)
        elif webhook.target_type == "web_collection":
            task = run_web_collection_task.delay(webhook.target_id, None)
        elif webhook.target_type == "perf_scenario":
            task = run_perf_scenario_task.delay(webhook.target_id)
        else:
            return _error(400, "不支持的 target_type")

        logger.info("Webhook 触发成功", webhook_name=webhook.name, task_id=task.id if task else None)
        return _success(data={"task_id": task.id if task else None}, message="任务已触发")
    except Exception as e:
        logger.error("Webhook 触发异常", error=str(e))
        return _error(500, f"触发失败: {str(e)}")


# ==================== 定时任务 ====================

@router.get("/api/v1/schedules")
@release_session
def get_schedules(request: Request, user: User = Depends(_current_user)):
    """获取项目的定时任务列表（IDOR 修复：补 project 归属校验，越权 404）"""
    project_id = query_int(request, "project_id", 0)
    if not project_id:
        return _error(400, "缺少 project_id 参数")

    # IDOR 修复：v1 仅按 project_id 过滤，可枚举他人项目的定时任务
    if not _owned_project(user.id, project_id):
        return _error(404, "项目不存在")

    tasks = db.session.scalars(select(ScheduledTask).filter_by(project_id=project_id)).all()
    return _success(data=[t.to_dict() for t in tasks])


@router.post("/api/v1/schedules")
@release_session
def create_schedule(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """创建定时任务（IDOR 修复：补 project 归属校验，防止向他人项目注入）"""
    data = data or {}

    required_fields = ["project_id", "name", "cron_expression", "target_type", "target_id"]
    if not all(data.get(f) for f in required_fields):
        return _error(400, "参数不完整")

    # IDOR 修复：v1 未校验项目归属，可向他人项目注入定时任务
    if not _owned_project(user.id, data.get("project_id")):
        return _error(403, "无权在该项目下创建定时任务")

    task = ScheduledTask(
        project_id=data.get("project_id"),
        name=data.get("name"),
        cron_expression=data.get("cron_expression"),
        target_type=data.get("target_type"),
        target_id=data.get("target_id"),
        notify_webhook=data.get("notify_webhook"),
        notify_events=data.get("notify_events", "all"),
    )
    db.session.add(task)
    db.session.commit()

    # 这里应调用 APScheduler 添加任务（与 v1 保持原调用）
    from ....scheduler import add_or_update_job
    add_or_update_job(task)

    return _success(data=task.to_dict(), message="定时任务创建成功")


@router.put("/api/v1/schedules/{task_id}")
@release_session
def update_schedule(
    task_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """更新定时任务（project.owner_id 校验，源文件已有，越权 403）"""
    user_id = user.id
    task = db.session.get(ScheduledTask, task_id)
    if not task:
        return _error(404, "任务不存在")

    # 校验权限
    project = db.session.scalar(select(Project).filter_by(id=task.project_id, owner_id=user_id))
    if not project:
        return _error(403, "无权修改该定时任务")

    data = data or {}
    if "name" in data:
        task.name = data["name"]
    if "cron_expression" in data:
        task.cron_expression = data["cron_expression"]
    if "is_active" in data:
        task.is_active = data["is_active"]
    if "notify_webhook" in data:
        task.notify_webhook = data["notify_webhook"]
    if "notify_events" in data:
        task.notify_events = data["notify_events"]

    db.session.commit()

    # 更新 APScheduler（与 v1 保持原调用）
    from ....scheduler import add_or_update_job, remove_job
    if task.is_active:
        add_or_update_job(task)
    else:
        remove_job(task.id)

    return _success(data=task.to_dict(), message="定时任务更新成功")


@router.delete("/api/v1/schedules/{task_id}")
@release_session
def delete_schedule(task_id: int, user: User = Depends(_current_user)):
    """删除定时任务（project.owner_id 校验，源文件已有，越权 403）"""
    user_id = user.id
    task = db.session.get(ScheduledTask, task_id)
    if not task:
        return _error(404, "任务不存在")

    # 校验权限
    project = db.session.scalar(select(Project).filter_by(id=task.project_id, owner_id=user_id))
    if not project:
        return _error(403, "无权删除该定时任务")

    db.session.delete(task)
    db.session.commit()

    # 从 APScheduler 移除（与 v1 保持原调用）
    from ....scheduler import remove_job
    remove_job(task_id)

    return _success(message="定时任务删除成功")
