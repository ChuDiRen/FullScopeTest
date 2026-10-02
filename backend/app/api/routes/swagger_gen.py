"""
Swagger 智能用例生成模块 - FastAPI 平迁（自 app/api/swagger_gen.py，蓝图挂载前缀 /api/v1）

路径/方法/状态码/响应字段与原 v1 完全一致（/api/v1/ai/generate-cases-from-swagger*）。

鉴权映射：
- @jwt_required() → Depends(_current_user)（内部复用 deps.get_current_user）
- 公开端点：无（swagger_gen 全部端点在 v1 均要求 JWT）

会话生命周期：
- 同步端点统一加 @release_session（deps.release_session），防 NullPool 连接泄漏
- 鉴权依赖包装为 async _current_user：在事件循环线程执行 get_current_user

IDOR 修复（按项目归属过滤，越权/不存在 → 404）：
- POST /ai/generate-cases-from-swagger（body.project_id）：
  v1 未校验项目归属即可向任意项目写入用例集合，平迁补齐——
  project_id 不在"自有项目 + 所在组织项目"可访问域内 → 404
- POST /ai/generate-cases-from-swagger/save（body.project_id）：同上

业务校验与 v1 完全一致（swagger_content/content_type/cases/project_id 必填、
content_type 仅 json/yaml），不放宽。
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from app.api.v2.deps import get_current_user, json_body, release_session
from app.core.logging import get_logger
from app.extensions import db
from app.models.organization import OrganizationMember
from app.models.project import Project
from app.models.user import User
from app.services.ai.swagger_case_generator import swagger_case_generator
from sqlalchemy import select

logger = get_logger(__name__)

router = APIRouter(tags=["swagger-gen"])


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


def _accessible_project_ids(user_id: int) -> list:
    """用户可访问的项目 ID：自有项目 + 所在组织项目（与 reports.py 平迁一致）"""
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


def _ensure_project_accessible(user_id: int, project_id: Optional[int]) -> bool:
    """指定 project_id 时校验其属于用户可访问域（IDOR 修复）"""
    if not project_id:
        return True
    return project_id in _accessible_project_ids(user_id)


# ==================== Swagger 用例生成 ====================

@router.post("/api/v1/ai/generate-cases-from-swagger")
@release_session
def generate_cases_from_swagger(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """
    基于 Swagger/OpenAPI 规范生成测试用例

    请求体:
        swagger_content: Swagger JSON 或 YAML 字符串（必填）
        content_type: 'json' 或 'yaml'（默认 'json'）
        project_id: 可选，关联的项目 ID（须在可访问域内，否则 404）
        collection_name: 可选，创建的测试集合名称（默认使用 Swagger 标题）
        save: 是否直接保存到数据库（默认 false，仅返回生成结果）

    Returns:
        生成的测试用例列表
    """
    data = data or {}
    user_id = user.id

    swagger_content = data.get("swagger_content", "")
    if not swagger_content or not str(swagger_content).strip():
        return _error(400, "swagger_content is required")

    content_type = data.get("content_type", "json")
    if content_type not in ("json", "yaml"):
        return _error(400, 'content_type must be "json" or "yaml"')

    project_id = data.get("project_id")
    collection_name = data.get("collection_name")
    save_to_db = data.get("save", False)

    # IDOR 修复：project_id 必须在可访问域内
    if not _ensure_project_accessible(user_id, project_id):
        logger.warning(
            "IDOR attempt blocked on swagger case generation",
            user_id=user_id,
            project_id=project_id,
        )
        return _error(404, "项目不存在")

    try:
        # 生成用例
        result = swagger_case_generator.generate_cases(
            swagger_content=swagger_content,
            content_type=content_type,
            user_id=user_id,
        )

        # 如果需要保存到数据库
        saved_count = 0
        if save_to_db:
            saved_count = _save_cases_to_db(
                result=result,
                user_id=user_id,
                project_id=project_id,
                collection_name=collection_name,
            )

        response_data = {
            "spec_info": result["spec_info"],
            "endpoints_count": result["endpoints_count"],
            "generated_cases": result["generated_cases"],
            "summary": result["summary"],
        }
        if save_to_db:
            response_data["saved_count"] = saved_count

        return _success(
            data=response_data,
            message=f"成功生成 {result['summary']['total_cases']} 个测试用例",
        )

    except ValueError as exc:
        return _error(400, str(exc))
    except Exception as exc:
        logger.error("Swagger case generation failed", error=str(exc))
        return _error(500, f"用例生成失败: {str(exc)}")


@router.post("/api/v1/ai/generate-cases-from-swagger/save")
@release_session
def save_generated_cases(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """
    将生成的测试用例保存到数据库

    请求体:
        cases: 测试用例数组（必填）
        project_id: 项目 ID（必填，须在可访问域内，否则 404）
        collection_name: 集合名称（可选）
        environment_id: 关联的环境 ID（可选）
    """
    data = data or {}
    user_id = user.id

    cases = data.get("cases", [])
    if not cases:
        return _error(400, "cases is required and must be a non-empty array")

    project_id = data.get("project_id")
    if not project_id:
        return _error(400, "project_id is required")

    # IDOR 修复：project_id 必须在可访问域内
    if not _ensure_project_accessible(user_id, project_id):
        logger.warning(
            "IDOR attempt blocked on swagger case save",
            user_id=user_id,
            project_id=project_id,
        )
        return _error(404, "项目不存在")

    collection_name = data.get("collection_name", "AI Generated Cases")
    environment_id = data.get("environment_id")

    try:
        saved_count = _save_cases_to_db(
            result={"generated_cases": cases, "spec_info": {}},
            user_id=user_id,
            project_id=project_id,
            collection_name=collection_name,
            environment_id=environment_id,
        )
        return _success(
            data={"saved_count": saved_count},
            message=f"成功保存 {saved_count} 个测试用例",
        )
    except Exception as exc:
        logger.error("Failed to save generated cases", error=str(exc))
        return _error(500, f"保存失败: {str(exc)}")


def _save_cases_to_db(
    result: dict,
    user_id: int,
    project_id=None,
    collection_name=None,
    environment_id=None,
) -> int:
    """将生成的用例保存到数据库，返回保存数量（与 v1 一致）"""
    from app.models.api_test_case import ApiTestCollection, ApiTestCase

    cases = result.get("generated_cases", [])
    if not cases:
        return 0

    # 创建或获取集合
    if not collection_name:
        spec_info = result.get("spec_info", {})
        collection_name = spec_info.get("title", "AI Generated Cases")

    collection = ApiTestCollection(
        project_id=project_id,
        user_id=user_id,
        name=collection_name,
        description=f"AI generated from OpenAPI spec - {collection_name}",
    )
    db.session.add(collection)
    db.session.flush()  # 获取 ID

    saved_count = 0
    for case_data in cases:
        try:
            # 构建 URL
            url = case_data.get("url", "")
            if url.startswith("{baseUrl}"):
                url = url.replace("{baseUrl}", "", 1)

            # 构建断言
            assertions = []
            expected_status = case_data.get("expected_status")
            if expected_status:
                assertions.append({
                    "type": "status_code",
                    "operator": "equals",
                    "expected": expected_status,
                })
            for expected_text in case_data.get("expected_contains", []):
                assertions.append({
                    "type": "body_contains",
                    "operator": "contains",
                    "expected": expected_text,
                })

            # 构建标签
            tags = case_data.get("tags", [])
            category = case_data.get("category", "")
            if category and category not in tags:
                tags.append(f"ai-gen:{category}")
            if "ai-generated" not in tags:
                tags.append("ai-generated")

            test_case = ApiTestCase(
                collection_id=collection.id,
                project_id=project_id,
                user_id=user_id,
                environment_id=environment_id,
                name=case_data.get("name", f"{case_data.get('method', 'GET')} {url}"),
                description=case_data.get("description", ""),
                method=case_data.get("method", "GET"),
                url=url,
                headers=case_data.get("headers") or {},
                params=case_data.get("params") or {},
                body=case_data.get("body"),
                body_type=case_data.get("body_type") or "json",
                assertions=assertions,
                tags=tags,
                priority=case_data.get("priority", 2),
                is_enabled=True,
            )
            db.session.add(test_case)
            saved_count += 1
        except Exception as exc:
            logger.warning(
                "Failed to save individual test case",
                case_name=case_data.get("name", ""),
                error=str(exc),
            )
            continue

    db.session.commit()
    return saved_count
