"""
评论与讨论模块 - FastAPI 平迁（自 app/api/comments.py，蓝图挂载前缀 /api/v1）

路径/方法/状态码/响应字段与原 v1 完全一致（/api/v1/comments...）。

鉴权映射：
- @jwt_required() → Depends(_current_user)（内部复用 deps.get_current_user）
- 公开端点：无（comments 全部端点在 v1 均要求 JWT）

会话生命周期：
- 同步端点统一加 @release_session（deps.release_session），防 NullPool 连接泄漏
- 鉴权依赖包装为 async _current_user：在事件循环线程执行 get_current_user

@validate_json 复刻（app/utils/validators.py）：
- Content-Type 非 JSON → 400 '请求必须是 JSON 格式'
- body 为空 → 400 '请求体不能为空'
- 缺必需字段 → 400 '缺少必需字段: x, y'

IDOR 防护（comments 按 user_id 归属，越权 404）：
- GET /comments/{comment_id}：v1 无属主校验，平迁补齐——仅作者或管理员可见，他人 404
- PUT/DELETE /comments/{comment_id}：service 层已有"仅作者或管理员"校验
  （AppError PermissionError → 403，与 v1 状态码一致），沿用
- GET /comments/{resource_type}/{resource_id}：资源级评论列表（协作场景，
  v1 即对全体登录用户开放），沿用
"""

from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..deps import get_current_user, json_body, query_int, release_session
from ....core.logging import get_logger
from ....models.comment import Comment
from ....models.user import User
from ....services.comment_service import CommentService
from ....utils.exceptions import AppError
from sqlalchemy import select
from ....extensions import db

logger = get_logger(__name__)
comment_service = CommentService()

router = APIRouter(tags=["comments"])


# ---------------------------------------------------------------------------
# 鉴权包装 / 响应构造 / validate_json 复刻
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


def _validate_json(request: Request, data: Dict[str, Any], *required_fields) -> JSONResponse | None:
    """
    复刻 v1 @validate_json 装饰器的三段校验，返回错误响应或 None（校验通过）。
    """
    content_type = (request.headers.get("content-type") or "").lower()
    if "application/json" not in content_type and not content_type.endswith("+json"):
        return _error(400, "请求必须是 JSON 格式")
    if not data:
        return _error(400, "请求体不能为空")
    missing_fields = [field for field in required_fields if field not in data]
    if missing_fields:
        return _error(400, f'缺少必需字段: {", ".join(missing_fields)}')
    return None


# ==================== 评论 CRUD ====================

@router.post("/api/v1/comments")
@release_session
def create_comment(
    request: Request,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """
    创建评论

    请求体:
        resource_type: 资源类型 (必填: test_case/test_run/test_plan)
        resource_id: 资源 ID (必填)
        content: 评论内容 (必填, Markdown 格式)
        parent_id: 父评论 ID (可选, 用于回复)
    """
    user_id = int(user.id)

    err = _validate_json(request, data or {}, "resource_type", "resource_id", "content")
    if err:
        return err

    try:
        comment = comment_service.create_comment(
            user_id=user_id,
            resource_type=data["resource_type"],
            resource_id=data["resource_id"],
            content=data["content"],
            parent_id=data.get("parent_id"),
        )
        return _success(data=comment, message="评论创建成功", code=200)
    except AppError as e:
        return _error(e.code, e.message, errors=e.errors)


@router.get("/api/v1/comments/{resource_type}/{resource_id}")
@release_session
def list_comments(
    resource_type: str,
    resource_id: int,
    request: Request,
    user: User = Depends(_current_user),
):
    """
    获取资源的评论列表

    查询参数:
        page: 页码 (默认 1)
        per_page: 每页数量 (默认 50)
    """
    page = query_int(request, "page", 1)
    per_page = query_int(request, "per_page", 50)

    try:
        result = comment_service.get_comments(resource_type, resource_id, page, per_page)
        return _success(data=result)
    except AppError as e:
        return _error(e.code, e.message)


@router.get("/api/v1/comments/{comment_id}")
@release_session
def get_comment(comment_id: int, user: User = Depends(_current_user)):
    """
    获取单条评论详情

    IDOR 修复：v1 无属主校验，平迁补齐——仅作者或管理员可见，他人 404。
    """
    try:
        comment = comment_service.get_comment(comment_id)
    except AppError as e:
        return _error(e.code, e.message)

    # 属主校验：仅作者或管理员可见（不暴露他人评论的存在性，统一 404）
    comment_model = db.session.get(Comment, comment_id)
    if not comment_model or (comment_model.user_id != user.id and not user.is_admin()):
        logger.warning(
            "IDOR attempt blocked on comment read",
            user_id=user.id,
            comment_id=comment_id,
        )
        return _error(404, f"评论 (id={comment_id}) 不存在")

    return _success(data=comment)


@router.put("/api/v1/comments/{comment_id}")
@release_session
def update_comment(
    comment_id: int,
    request: Request,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """
    编辑评论（仅作者或管理员）

    请求体:
        content: 新的评论内容 (必填)
    """
    user_id = int(user.id)

    err = _validate_json(request, data or {}, "content")
    if err:
        return err

    try:
        comment = comment_service.update_comment(
            comment_id=comment_id,
            user_id=user_id,
            content=data["content"],
        )
        return _success(data=comment, message="评论已更新")
    except AppError as e:
        return _error(e.code, e.message)


@router.delete("/api/v1/comments/{comment_id}")
@release_session
def delete_comment(comment_id: int, user: User = Depends(_current_user)):
    """软删除评论（仅作者或管理员）"""
    user_id = int(user.id)

    try:
        comment_service.delete_comment(comment_id, user_id)
        return _success(message="评论已删除")
    except AppError as e:
        return _error(e.code, e.message)
