"""
GitHub Check Run 模块 - FastAPI 平迁（自 app/api/github_checks.py，蓝图挂载前缀 /api/v1）

路径/方法/状态码/响应字段与原 v1 完全一致（/api/v1/github-checks/...），
前端与 CI 脚本零改动。全部端点为同步 def，运行于 RequestContextMiddleware
push 的 app context 内，直接复用 数据库会话（app/database.py ContextVar 作用域） 与 services 层。

鉴权映射：
- @jwt_required() → Depends(_current_user)（内部复用 deps.get_current_user）
- 公开端点：无（本模块 3 个端点在 v1 全部有 @jwt_required）

IDOR / 权限（沿用 v1 的 project 归属校验，越权/不存在一律 404）：
- POST /api/v1/github-checks/{test_run_id}/create|update|complete：
  _get_test_run_owned_by_user 校验 TestRun 所属 Project 的 owner_id == 当前用户，
  否则记 IDOR 日志并返回 404

GitHub API 调用：
- 与 v1 一致全部走 services.github_check_service（create_check_service → GitHubCheckService），
  路由本身零外呼；测试通过 monkeypatch create_check_service 打桩，零真实外呼
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..deps import get_current_user, json_body, release_session
from ....core.logging import get_logger
from ....extensions import db
from ....models.github_integration import GitHubIntegration
from ....models.project import Project
from ....models.test_run import TestRun
from ....models.user import User
from ....services.github_check_service import create_check_service
from sqlalchemy import select

logger = get_logger(__name__)

router = APIRouter(tags=["github-checks"])


# ---------------------------------------------------------------------------
# 鉴权包装 / 响应构造 / 归属校验
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


def _get_test_run_owned_by_user(test_run_id: int, user_id: int) -> Tuple[Optional[TestRun], Optional[JSONResponse]]:
    """获取 TestRun 并验证项目所有者，返回 (test_run, None) 或 (None, error)"""
    test_run = db.session.scalar(select(TestRun).filter_by(id=test_run_id))
    if not test_run:
        return None, _error(404, "测试运行记录不存在")
    project = db.session.get(Project, test_run.project_id)
    if not project or project.owner_id != user_id:
        logger.warning(
            "IDOR attempt blocked on github_checks",
            user_id=user_id,
            test_run_id=test_run_id,
        )
        return None, _error(404, "测试运行记录不存在")
    return test_run, None


def _get_active_integration(user_id: int) -> Optional[GitHubIntegration]:
    """获取当前用户的活跃 GitHub 集成"""
    return db.session.scalar(select(GitHubIntegration).filter_by(user_id=user_id, is_active=True))


# ==================== Check Run 管理 ====================

@router.post("/api/v1/github-checks/{test_run_id}/create")
@release_session
def create_check_run(
    test_run_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """
    为测试运行创建 GitHub Check Run

    请求体:
        repo_full_name: 仓库全名 (owner/repo)
        head_sha: 提交 SHA
    """
    user_id = user.id
    data = data or {}

    repo_full_name = data.get("repo_full_name")
    head_sha = data.get("head_sha")

    if not repo_full_name or not head_sha:
        return _error(400, "缺少 repo_full_name 或 head_sha 参数")

    # 获取测试运行记录（验证所有权）
    test_run, err = _get_test_run_owned_by_user(test_run_id, user_id)
    if err:
        return err

    # 获取 GitHub 集成信息
    integration = _get_active_integration(user_id)
    if not integration:
        return _error(404, "未找到 GitHub 集成信息")

    # 创建 Check Run（GitHub API 调用在 service 层，测试中 mock）
    service = create_check_service(integration)
    result = service.start_test_check_run(test_run, repo_full_name, head_sha)

    if not result:
        return _error(500, "创建 Check Run 失败")

    # 更新测试运行记录
    test_run.check_run_id = result.get("id")
    test_run.check_run_repo = repo_full_name
    db.session.commit()

    return _success(data=result, message="Check Run 创建成功")


@router.post("/api/v1/github-checks/{test_run_id}/update")
@release_session
def update_check_run(
    test_run_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """
    更新 Check Run 进度

    请求体:
        current_step: 当前步骤描述
    """
    user_id = user.id
    data = data or {}

    test_run, err = _get_test_run_owned_by_user(test_run_id, user_id)
    if err:
        return err

    if not test_run.check_run_id or not test_run.check_run_repo:
        return _error(400, "此测试运行没有关联的 Check Run")

    integration = _get_active_integration(user_id)
    if not integration:
        return _error(404, "未找到 GitHub 集成信息")

    service = create_check_service(integration)
    result = service.update_test_progress(
        test_run.check_run_repo,
        test_run.check_run_id,
        test_run,
        current_step=data.get("current_step"),
    )

    if not result:
        return _error(500, "更新 Check Run 失败")

    return _success(data=result, message="Check Run 更新成功")


@router.post("/api/v1/github-checks/{test_run_id}/complete")
@release_session
def complete_check_run(
    test_run_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """
    完成 Check Run

    请求体:
        report_url: 报告链接（可选）
    """
    user_id = user.id
    data = data or {}

    test_run, err = _get_test_run_owned_by_user(test_run_id, user_id)
    if err:
        return err

    if not test_run.check_run_id or not test_run.check_run_repo:
        return _error(400, "此测试运行没有关联的 Check Run")

    integration = _get_active_integration(user_id)
    if not integration:
        return _error(404, "未找到 GitHub 集成信息")

    service = create_check_service(integration)
    result = service.complete_test_check_run(
        test_run.check_run_repo,
        test_run.check_run_id,
        test_run,
        report_url=data.get("report_url"),
    )

    if not result:
        return _error(500, "完成 Check Run 失败")

    return _success(data=result, message="Check Run 已完成")
