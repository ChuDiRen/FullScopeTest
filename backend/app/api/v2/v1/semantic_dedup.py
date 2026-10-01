"""
测试用例语义去重模块 - FastAPI 平迁（自 app/api/semantic_dedup.py，蓝图挂载前缀 /api/v1）

路径/方法/状态码/响应字段与原 v1 完全一致（/api/v1/ai/find-duplicates）。
同步端点，运行于 RequestContextMiddleware push 的 app context 内，直接复用
services/ai/semantic_dedup_service.find_duplicates 层。

v1 全量路由清单（grep "@.*_bp.route" app/api/semantic_dedup.py）：
- POST /api/v1/ai/find-duplicates

鉴权映射：
- @jwt_required() → Depends(_current_user)（内部复用 deps.get_current_user）
- 本模块无公开端点（v1 带 @jwt_required）

IDOR 修复（属主字段：Project.owner_id / Project.organization_id）：
- v1 对 body.project_id 完全不做归属校验，任意登录用户可对任意项目发起去重扫描
  并拿回该项目用例的名称/方法/URL（信息泄露）。平迁后校验项目属于当前用户
  可访问域（自有项目 + 所在组织的项目），越权/不存在一律 404。
- 项目可访问后，项目内用例对组织成员可见（与 api_test 模块的访问域口径一致）。

AI 调用：
- 沿用 services/ai/semantic_dedup_service.find_duplicates（Embedding API 优先，
  失败自动降级 TF-IDF），零改动复用；测试通过 monkeypatch 路由模块属性
  mock 掉 AI 客户端（零真实外呼、零 API key 依赖）

会话生命周期：
- 同步端点统一加 @release_session（deps.release_session）
- 鉴权依赖包装为 async _current_user：在事件循环线程执行 get_current_user
"""

from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..deps import get_current_user, json_body, release_session
from ....core.logging import get_logger
from ....models.organization import OrganizationMember
from ....models.project import Project
from ....models.user import User
from ....services.ai.semantic_dedup_service import find_duplicates
from sqlalchemy import select
from ....extensions import db

logger = get_logger(__name__)

router = APIRouter(tags=["semantic-dedup"])


# ---------------------------------------------------------------------------
# 鉴权包装 / 响应构造 / 项目可访问域
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


def _accessible_project_ids(user_id: int) -> list:
    """用户可访问的项目 ID（自己创建的 + 所在组织的），等价 v1/api_test 的访问域"""
    org_ids = [
        om.organization_id
        for om in db.session.scalars(select(OrganizationMember).filter_by(user_id=user_id, is_active=True)).all()]
    owned_ids = [p.id for p in db.session.scalars(select(Project).filter_by(owner_id=user_id)).all()]
    org_proj_ids = (
        [p.id for p in db.session.scalars(select(Project).filter(Project.organization_id.in_(org_ids))).all()]
        if org_ids
        else []
    )
    return list(set(owned_ids + org_proj_ids))


def _ensure_project_accessible(user_id: int, project_id: Any) -> bool:
    """校验 project_id 属于用户可访问域（IDOR 修复）"""
    try:
        pid = int(project_id)
    except (TypeError, ValueError):
        return False
    return pid in _accessible_project_ids(user_id)


# ==================== 语义去重 ====================


@router.post("/api/v1/ai/find-duplicates")
@release_session
def find_test_duplicates(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """
    查找项目中语义相似的测试用例（IDOR 修复：project_id 可访问域校验，越权 404）

    请求体:
        project_id: 项目 ID（必填）
        threshold: 相似度阈值（可选，默认 0.85，范围 0.0-1.0）
        case_type: 用例类型（可选，默认 'api'，可选 'web'）
        limit: 最多分析的用例数量（可选，默认 500）

    Returns:
        重复用例对列表，按相似度降序排列
    """
    data = data or {}

    project_id = data.get("project_id")
    if not project_id:
        return _error(400, "project_id is required")

    threshold = data.get("threshold", 0.85)
    if not isinstance(threshold, (int, float)) or not (0.0 <= threshold <= 1.0):
        return _error(400, "threshold must be a number between 0.0 and 1.0")

    case_type = data.get("case_type", "api")
    if case_type not in ("api", "web"):
        return _error(400, 'case_type must be "api" or "web"')

    limit = data.get("limit", 500)
    if not isinstance(limit, int) or limit < 1:
        return _error(400, "limit must be a positive integer")

    # IDOR 修复：v1 未校验项目归属，任意用户可扫描任意项目并取回用例信息
    if not _ensure_project_accessible(user.id, project_id):
        return _error(404, "项目不存在")

    # 获取运行时 AI 配置
    from ....core.runtime import get_config

    config = {
        "AI_ASSISTANT_BASE_URL": get_config().get("AI_ASSISTANT_BASE_URL", ""),
        "AI_ASSISTANT_API_KEY": get_config().get("AI_ASSISTANT_API_KEY", ""),
        "AI_ASSISTANT_MODEL": get_config().get("AI_ASSISTANT_MODEL", ""),
    }

    # 允许前端覆盖配置
    if data.get("embedding_base_url"):
        config["AI_EMBEDDING_BASE_URL"] = str(data["embedding_base_url"]).strip()
    if data.get("embedding_api_key"):
        config["AI_EMBEDDING_API_KEY"] = str(data["embedding_api_key"]).strip()
    if data.get("embedding_model"):
        config["AI_EMBEDDING_MODEL"] = str(data["embedding_model"]).strip()

    try:
        result = find_duplicates(
            project_id=project_id,
            threshold=threshold,
            case_type=case_type,
            config=config,
            limit=limit,
        )

        return _success(
            data=result,
            message=f'发现 {result["summary"]["duplicate_count"]} 组重复用例',
        )

    except Exception as exc:
        logger.error("Dedup scan failed", error=str(exc), project_id=project_id)
        return _error(500, f"去重检测失败: {str(exc)}")
