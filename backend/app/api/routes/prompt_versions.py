"""
Prompt 版本管理模块 - FastAPI 平迁（自 app/api/prompt_versions.py，蓝图挂载前缀 /api/v1）

路径/方法/状态码/响应字段与原 v1 完全一致（/api/v1/ai/prompt-versions/...），
前端与 CI 脚本零改动。全部端点为同步 def，运行于 RequestContextMiddleware
push 的 app context 内，直接复用 数据库会话（app/database.py ContextVar 作用域） 与
services/ai/prompt_version_service 层。

v1 全量路由清单（grep "@.*_bp.route" app/api/prompt_versions.py）：
- GET    /api/v1/ai/prompt-versions
- POST   /api/v1/ai/prompt-versions                    （响应信封 code=200）
- GET    /api/v1/ai/prompt-versions/{version_id}
- PUT    /api/v1/ai/prompt-versions/{version_id}
- DELETE /api/v1/ai/prompt-versions/{version_id}       （软删除/停用）
- POST   /api/v1/ai/prompt-versions/select             （A/B 流量权重选择）
- POST   /api/v1/ai/prompt-versions/refresh-stats      （从 AIInvocationLog 重算统计）

注：app/api/ai_prompt_versions.py 与本文件存在 5 条同路径同方法路由
（GET/POST /ai/prompt-versions、GET/PUT/DELETE /ai/prompt-versions/{id}），
但该蓝图在 v1 从未被注册（app/api/__init__.py 未 import，且 view 函数与本文件
同名，import 会直接触发 Flask duplicate endpoint AssertionError），其独有路由
GET /ai/prompt-versions/{id}/stats 迁移至 ai_prompt_versions.py 路由文件。
静态路径 /select、/refresh-stats 必须注册在 /{version_id} 之前，避免被路径参数吞掉。

鉴权映射：
- @jwt_required() → Depends(_current_user)（内部复用 deps.get_current_user）
- 本模块无公开端点（v1 全部端点均带 @jwt_required）

IDOR 修复（属主字段：PromptVersion.created_by）：
- v1 任意登录用户可读/改/停用任意他人创建的 Prompt 版本（含 system_prompt 内容）。
  平迁后按 created_by 过滤，越权/不存在一律 404：
  * GET  列表 / 详情 / select：可见域 = 当前用户创建 + 平台全局预置（created_by IS NULL）
  * PUT / DELETE / refresh-stats：仅限当前用户创建（全局预置版本不允许普通用户改停）；
    refresh-stats 只刷新可见域内的版本并返回相应计数
- POST 创建的版本 created_by = 当前用户

会话生命周期：
- 同步端点统一加 @release_session（deps.release_session）
- 鉴权依赖包装为 async _current_user：在事件循环线程执行 get_current_user
"""

import random
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import or_

from app.api.v2.deps import get_current_user, json_body, query_int, query_str, release_session
from app.core.logging import get_logger
from app.models.prompt_version import PromptVersion
from app.models.user import User
from app.services.ai.prompt_version_service import prompt_version_service
from sqlalchemy import select
from app.database import paginate
from app.extensions import db

logger = get_logger(__name__)

# 与 v1 一致的合法 feature 集合
VALID_FEATURES = {
    "copilot",
    "script_gen",
    "script_gen_web",
    "script_gen_perf",
    "swagger_gen",
    "dedup",
}

router = APIRouter(tags=["prompt-versions"])


# ---------------------------------------------------------------------------
# 鉴权包装 / 响应构造 / 属主过滤
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


def _paginate(items, total: int, page: int, per_page: int, message: str = "success") -> JSONResponse:
    """等价 v1 paginate_response"""
    return JSONResponse(
        status_code=200,
        content={
            "code": 200,
            "message": message,
            "data": {
                "items": items,
                "pagination": {
                    "total": total,
                    "page": page,
                    "per_page": per_page,
                    "pages": (total + per_page - 1) // per_page,
                },
            },
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
        },
    )


def _visible_versions_query(user_id: int):
    """可见域查询：当前用户创建 + 平台全局预置（created_by IS NULL）（IDOR 修复）"""
    return select(PromptVersion).filter(
        or_(PromptVersion.created_by == user_id, PromptVersion.created_by.is_(None))
    )


def _get_visible_version(user_id: int, version_id: int) -> Optional[PromptVersion]:
    """按 id 取可见版本（own 或全局预置）：不存在/越权一律 None → 404"""
    return db.session.scalar(_visible_versions_query(user_id).filter_by(id=version_id))


def _get_owned_version(user_id: int, version_id: int) -> Optional[PromptVersion]:
    """按 id 取当前用户自有的版本（写路径用）：不存在/越权/全局预置一律 None → 404"""
    return db.session.scalar(select(PromptVersion).filter_by(id=version_id, created_by=user_id))


def _select_visible_active_version(feature: str, user_id: int) -> Optional[PromptVersion]:
    """
    在可见域（own + 全局预置）的激活版本中按 traffic_weight 做 A/B 选择。
    权重选择逻辑与 PromptVersionService.select_version_for_ab_test 一致，
    仅在候选集上补属主过滤（IDOR 修复）。
    """
    candidates = [
        v
        for v in prompt_version_service.get_active_versions(feature)
        if v.created_by is None or v.created_by == user_id
    ]
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    total_weight = sum(v.traffic_weight for v in candidates)
    if total_weight <= 0:
        return random.choice(candidates)

    rand = random.uniform(0, total_weight)
    cumulative = 0.0
    for v in candidates:
        cumulative += v.traffic_weight
        if rand <= cumulative:
            return v

    return candidates[-1]


# ==================== 列表 / 创建 ====================


@router.get("/api/v1/ai/prompt-versions")
@release_session
def list_prompt_versions(request: Request, user: User = Depends(_current_user)):
    """
    获取 Prompt 版本列表（IDOR 修复：仅返回 当前用户创建 + 平台全局预置 的版本）

    查询参数:
        feature: 按功能模块过滤（copilot / script_gen / swagger_gen / dedup 等）
        is_active: 按激活状态过滤（true / false）
        page: 页码（默认 1）
        per_page: 每页数量（默认 20）
    """
    feature = query_str(request, "feature").strip() or None
    is_active_str = query_str(request, "is_active").strip().lower()
    is_active: Optional[bool] = None
    if is_active_str == "true":
        is_active = True
    elif is_active_str == "false":
        is_active = False

    page = max(1, query_int(request, "page", 1))
    per_page = max(1, query_int(request, "per_page", 20))

    query = _visible_versions_query(user.id)

    if feature:
        query = query.filter_by(feature=feature)
    if is_active is not None:
        query = query.filter_by(is_active=is_active)

    query = query.order_by(
        PromptVersion.feature.asc(),
        PromptVersion.version.desc(),
    )
    pagination = paginate(query, page=page, per_page=per_page)

    return _paginate(
        items=[v.to_dict() for v in pagination.items],
        total=pagination.total,
        page=page,
        per_page=per_page,
    )


@router.post("/api/v1/ai/prompt-versions")
@release_session
def create_prompt_version(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """
    创建新的 Prompt 版本（created_by = 当前用户）

    请求体:
        feature: 功能模块（必填）
        name: 版本名称（必填）
        system_prompt: 系统提示词（必填）
        user_prompt_template: 用户提示词模板（可选）
        temperature: 温度参数（默认 0.3）
        model_name: 指定模型（可选，为空使用全局默认）
        is_active: 是否激活（默认 false）
        traffic_weight: 流量权重（0.0-1.0，默认 1.0）
        change_notes: 变更说明（可选）
    """
    data = data or {}

    feature = (data.get("feature") or "").strip()
    name = (data.get("name") or "").strip()
    system_prompt = (data.get("system_prompt") or "").strip()

    if not feature:
        return _error(400, "feature is required")
    if not name:
        return _error(400, "name is required")
    if not system_prompt:
        return _error(400, "system_prompt is required")

    # 验证 feature 合法值
    if feature not in VALID_FEATURES:
        return _error(400, f"feature must be one of: {', '.join(sorted(VALID_FEATURES))}")

    user_id = user.id

    pv = prompt_version_service.create_version(
        feature=feature,
        name=name,
        system_prompt=system_prompt,
        user_prompt_template=data.get("user_prompt_template"),
        temperature=float(data.get("temperature", 0.3)),
        model_name=data.get("model_name"),
        is_active=bool(data.get("is_active", False)),
        traffic_weight=float(data.get("traffic_weight", 1.0)),
        change_notes=data.get("change_notes"),
        created_by=user_id,
    )

    logger.info("PromptVersion created", id=pv.id, feature=feature, version=pv.version)
    return _success(data=pv.to_dict(), message="Prompt 版本创建成功", code=200)


# ==================== A/B 选择 / 统计刷新（静态路径，先于 /{version_id} 注册） ====================


@router.post("/api/v1/ai/prompt-versions/select")
@release_session
def select_prompt_version(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """
    基于 A/B 测试流量权重选择一个激活的 Prompt 版本
    （IDOR 修复：仅从 当前用户创建 + 平台全局预置 的激活版本中选择）

    请求体:
        feature: 功能模块（必填）
    """
    data = data or {}
    feature = (data.get("feature") or "").strip()

    if not feature:
        return _error(400, "feature is required")

    pv = _select_visible_active_version(feature, user.id)
    if not pv:
        return _error(404, f"没有找到 feature={feature} 的激活版本")

    return _success(data=pv.to_dict())


@router.post("/api/v1/ai/prompt-versions/refresh-stats")
@release_session
def refresh_prompt_version_stats(request: Request, user: User = Depends(_current_user)):
    """
    刷新 Prompt 版本的统计数据（从 AIInvocationLog 重新聚合）
    （IDOR 修复：仅刷新 当前用户创建 + 平台全局预置 的版本）

    查询参数:
        feature: 可选，只刷新指定 feature 的版本
    """
    feature = query_str(request, "feature").strip() or None

    visible = _visible_versions_query(user.id)
    if feature:
        visible = visible.filter_by(feature=feature)

    count = 0
    for pv in db.session.scalars(visible):
        prompt_version_service.refresh_stats(pv.id)
        count += 1

    return _success(
        data={"refreshed_count": count},
        message=f"已刷新 {count} 个 Prompt 版本的统计数据",
    )


# ==================== 详情 / 更新 / 停用 ====================


@router.get("/api/v1/ai/prompt-versions/{version_id}")
@release_session
def get_prompt_version(version_id: int, user: User = Depends(_current_user)):
    """获取单个 Prompt 版本详情（IDOR 修复：仅可见 own + 全局预置，越权 404）"""
    pv = _get_visible_version(user.id, version_id)
    if not pv:
        return _error(404, "Prompt 版本不存在")
    return _success(data=pv.to_dict())


@router.put("/api/v1/ai/prompt-versions/{version_id}")
@release_session
def update_prompt_version(
    version_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """
    更新 Prompt 版本（IDOR 修复：仅限当前用户创建的版本，越权/全局预置 404）

    请求体（所有字段可选）:
        name, system_prompt, user_prompt_template, temperature,
        model_name, is_active, traffic_weight, change_notes
    """
    # IDOR 修复：先做属主校验（v1 直接进 service，可改他人版本）
    if not _get_owned_version(user.id, version_id):
        return _error(404, "Prompt 版本不存在")

    data = data or {}

    pv = prompt_version_service.update_version(
        version_id,
        name=data.get("name"),
        system_prompt=data.get("system_prompt"),
        user_prompt_template=data.get("user_prompt_template"),
        temperature=data.get("temperature"),
        model_name=data.get("model_name"),
        is_active=data.get("is_active"),
        traffic_weight=data.get("traffic_weight"),
        change_notes=data.get("change_notes"),
    )

    if not pv:
        return _error(404, "Prompt 版本不存在")

    logger.info("PromptVersion updated", id=pv.id, feature=pv.feature)
    return _success(data=pv.to_dict(), message="Prompt 版本更新成功")


@router.delete("/api/v1/ai/prompt-versions/{version_id}")
@release_session
def deactivate_prompt_version(version_id: int, user: User = Depends(_current_user)):
    """
    停用（软删除）Prompt 版本（IDOR 修复：仅限当前用户创建的版本，越权/全局预置 404）

    将 is_active 设为 False，记录停用时间。不会物理删除数据，保留历史记录。
    """
    # IDOR 修复：先做属主校验（v1 可停用任意他人的版本）
    if not _get_owned_version(user.id, version_id):
        return _error(404, "Prompt 版本不存在")

    ok = prompt_version_service.deactivate_version(version_id)
    if not ok:
        return _error(404, "Prompt 版本不存在")

    logger.info("PromptVersion deactivated", id=version_id)
    return _success(message="Prompt 版本已停用")
