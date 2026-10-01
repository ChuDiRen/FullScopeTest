"""
环境管理模块 - FastAPI 平迁（自 app/api/environments.py，蓝图挂载前缀 /api/v1）

路径/方法/状态码/响应字段与原 v1 完全一致，前端与 CI 脚本零改动。
全部端点为同步 def，运行于 RequestContextMiddleware push 的 app context 内，
直接复用 数据库会话（app/database.py ContextVar 作用域） 与 services 层。

路由清单（11 条，与 v1 一一对应）：
- GET    /api/v1/environments                            全局环境列表（无 project_id 时读缓存）
- POST   /api/v1/environments                            全局创建（缺 project_id 时落默认项目）
- GET    /api/v1/projects/{project_id}/environments      项目环境列表
- POST   /api/v1/projects/{project_id}/environments      项目内创建（name/base_url 必填）
- GET    /api/v1/environments/{env_id}                   详情
- PUT    /api/v1/environments/{env_id}                   更新
- DELETE /api/v1/environments/{env_id}                   删除
- POST   /api/v1/environments/{env_id}/default           设为默认
- GET    /api/v1/environments/{env_id}/export            导出 JSON
- POST   /api/v1/projects/{project_id}/environments/import  导入（JSON / .env 格式）
- GET    /api/v1/environments/{env_id}/export-docker     导出 Docker 格式

鉴权映射：@jwt_required() → Depends(_current_user)（内部复用 deps.get_current_user）。

IDOR 修复（越权一律 404）：
- Environment 模型经 backend/app/models/environment.py 确认 **没有 user_id 字段**，
  属主过滤只能通过 Project.owner_id 关联实现：所有按 env_id 取环境的路由
  （详情/更新/删除/设默认/导出/导出 Docker）统一 join Project 做 owner_id 过滤，
  不存在/越权/非法 id 一律 None → 404（v1 对越权返回 403，平迁统一为 404）。
- 项目级路由（列表/创建/导入）校验 project 属主（v1 即有），越权 404。
- 全局列表沿用 v1 的 filter_by_owner_or_org（组织隔离），未改动。

v1 潜在 bug 修复（保持预期语义，不改变合法路径行为）：
- v1 源文件缺 `from datetime import datetime, timezone`，export/import 三个端点
  一到 datetime 就 NameError → 500；平迁补齐 import。
- v1 import_environment 创建/查询 Environment 时传 user_id（模型无此列，
  TypeError/AttributeError → 必然 400 '导入失败'）；平迁去掉 user_id，
  归属由 project_id + 属主校验保证。
- v1 .env 导入分支创建 Environment 未传 base_url（模型 NOT NULL，会
  IntegrityError；v1 被上一条 user_id 错误掩盖）；平迁补 base_url=""。
- v1 merge 分支 in-place update 同一个 variables dict 后赋回（同对象赋值），
  SQLAlchemy 判定无变更 → 合并不落库（静默丢数据，v1 被上一条错误掩盖）；
  平迁改为 copy-on-write。
- v1 override 分支赋值 existing.description（模型无此列 → AttributeError
  → 必然 400 '导入失败'）；平迁用 getattr 兜底。
- v1 export JSON 引用 env.description（模型无此列 → AttributeError）；
  平迁用 getattr(env, 'description', None) 输出该字段。

会话生命周期：同步端点统一加 @release_session（deps.release_session），
视图返回后 rollback/commit/remove 本 worker 线程的 scoped session。
鉴权依赖包装为 async _current_user：在事件循环线程执行 get_current_user，
其 session 由 teardown_appcontext 正常回收（与 api_test.py/reports.py 范例一致）。
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ..deps import get_current_user, json_body, query_int, release_session
from ....extensions import db
from ....models.environment import Environment
from ....models.project import Project
from ....models.user import User
from ....services.cache_service import environments_key, get_cache_service, ENVIRONMENTS_TTL
from ....utils.org_filter import filter_by_owner_or_org
from sqlalchemy import select, update

router = APIRouter(tags=["environments"])


# ---------------------------------------------------------------------------
# 鉴权包装 / 响应构造 / 属主过滤
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


def _get_owned_environment(user_id: int, env_id: int) -> Optional[Environment]:
    """按 id 取环境并经 Project.owner_id 做属主过滤：不存在/越权一律 None → 404（IDOR 修复）"""
    return (
        db.session.scalar(select(Environment).join(Project, Environment.project_id == Project.id).filter(Environment.id == env_id, Project.owner_id == user_id)))


def _invalidate_env_cache(user_id: int) -> None:
    """环境变更后失效该用户的环境列表缓存（与 v1 相同的键前缀）"""
    cache = get_cache_service()
    if cache:
        cache.invalidate_pattern(f"envs:user:{user_id}")


def _normalize_variables(raw: Any):
    """校验/规整 variables 字段，返回 (variables, error_message)；与 v1 校验规则一致"""
    if isinstance(raw, dict):
        if len(raw) > 100:
            return None, f"环境变量不能超过100个，当前有{len(raw)}个"
        return raw, None
    if isinstance(raw, list):
        return None, 'variables 必须是对象类型（如 {"key": "value"}），不能是数组'
    return {}, None


# ==================== 全局环境入口 ====================

@router.get("/api/v1/environments")
@release_session
def get_all_environments(request: Request, user: User = Depends(_current_user)):
    """获取用户所有环境列表"""
    user_id = user.id
    project_id = query_int(request, "project_id", 0) or None

    # 检查缓存（仅无 project_id 过滤时）
    cache = get_cache_service()
    if cache and not project_id:
        cached = cache.get(environments_key(user_id))
        if cached is not None:
            return _success(data=cached)

    # 获取用户所有项目（组织隔离）
    user_projects = db.session.scalars(
        filter_by_owner_or_org(select(Project), Project, user_id)
    ).all()
    project_ids = [p.id for p in user_projects]

    if not project_ids:
        return _success(data=[])

    query = select(Environment).filter(Environment.project_id.in_(project_ids))

    if project_id:
        query = query.filter_by(project_id=project_id)

    environments = db.session.scalars(query).all()
    result = [e.to_dict() for e in environments]

    # 写入缓存
    if cache and not project_id:
        cache.set(environments_key(user_id), result, ttl=ENVIRONMENTS_TTL)

    return _success(data=result)


@router.post("/api/v1/environments")
@release_session
def create_global_environment(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """创建环境（从全局入口）"""
    user_id = user.id
    data = data or {}

    if not data:
        return _error(400, "请求数据不能为空")

    name = str(data.get("name") or "").strip()
    base_url = str(data.get("base_url") or "").strip()

    if not name:
        return _error(400, "环境名称不能为空")

    # 获取project_id，如果没有提供则使用用户的第一个项目
    project_id = data.get("project_id")
    if not project_id:
        project = db.session.scalar(select(Project).filter_by(owner_id=user_id))
        if not project:
            # 自动创建默认项目
            project = Project(name="默认项目", owner_id=user_id, settings={})
            db.session.add(project)
            db.session.commit()
        project_id = project.id
    else:
        # 验证项目权限（组织隔离）
        query = filter_by_owner_or_org(select(Project), Project, user_id)
        project = db.session.scalar(query.filter_by(id=project_id))
        if not project:
            return _error(404, "项目不存在")

    # 检查同名环境
    existing = db.session.scalar(select(Environment).filter_by(project_id=project_id, name=name))
    if existing:
        return _error(400, "环境名称已存在")

    # 验证 variables 字段
    variables, err = _normalize_variables(data.get("variables", {}))
    if err:
        return _error(400, err)

    env = Environment(
        project_id=project_id,
        name=name,
        base_url=base_url,
        variables=variables,
        headers=data.get("headers", {}),
        is_default=data.get("is_default", False),
    )

    # 如果设为默认，取消其他环境的默认状态
    if env.is_default:
        db.session.execute(
            update(Environment)
            .filter_by(project_id=project_id, is_default=True)
            .values(is_default=False)
        )

    db.session.add(env)
    db.session.commit()

    # 失效环境列表缓存
    _invalidate_env_cache(user_id)

    return _success(data=env.to_dict(), message="创建成功", code=200)


# ==================== 项目环境入口 ====================

@router.get("/api/v1/projects/{project_id}/environments")
@release_session
def get_environments(project_id: int, user: User = Depends(_current_user)):
    """获取项目的环境列表"""
    user_id = user.id

    # 验证项目权限
    project = db.session.scalar(select(Project).filter_by(id=project_id, owner_id=user_id))
    if not project:
        return _error(404, "项目不存在")

    environments = db.session.scalars(select(Environment).filter_by(project_id=project_id)).all()

    return _success(data=[e.to_dict() for e in environments])


@router.post("/api/v1/projects/{project_id}/environments")
@release_session
def create_environment(
    project_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """创建环境（等价 v1 @validate_json('name', 'base_url')）"""
    user_id = user.id

    # 验证项目权限
    project = db.session.scalar(select(Project).filter_by(id=project_id, owner_id=user_id))
    if not project:
        return _error(404, "项目不存在")

    data = data or {}
    if not data:
        return _error(400, "请求体不能为空")
    missing = [f for f in ("name", "base_url") if f not in data]
    if missing:
        return _error(400, f"缺少必需字段: {', '.join(missing)}")

    name = str(data["name"]).strip()
    base_url = str(data["base_url"]).strip()

    # 检查同名环境
    existing = db.session.scalar(select(Environment).filter_by(project_id=project_id, name=name))
    if existing:
        return _error(400, "环境名称已存在")

    # 验证 variables 字段
    variables, err = _normalize_variables(data.get("variables", {}))
    if err:
        return _error(400, err)

    env = Environment(
        project_id=project_id,
        name=name,
        base_url=base_url,
        variables=variables,
        headers=data.get("headers", {}),
        is_default=data.get("is_default", False),
    )

    # 如果设为默认，取消其他环境的默认状态
    if env.is_default:
        db.session.execute(
            update(Environment)
            .filter_by(project_id=project_id, is_default=True)
            .values(is_default=False)
        )

    db.session.add(env)
    db.session.commit()

    # 失效环境列表缓存
    _invalidate_env_cache(user_id)

    return _success(data=env.to_dict(), message="创建成功", code=200)


# ==================== 环境详情 / 更新 / 删除 ====================

@router.get("/api/v1/environments/{env_id}")
@release_session
def get_environment(env_id: int, user: User = Depends(_current_user)):
    """获取环境详情（IDOR 修复：经 Project.owner_id 属主过滤，越权 404）"""
    env = _get_owned_environment(user.id, env_id)
    if not env:
        return _error(404, "环境不存在")

    return _success(data=env.to_dict())


@router.put("/api/v1/environments/{env_id}")
@release_session
def update_environment(
    env_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """更新环境（IDOR 修复：越权 404）"""
    user_id = user.id

    env = _get_owned_environment(user_id, env_id)
    if not env:
        return _error(404, "环境不存在")

    data = data or {}

    if "name" in data:
        name = str(data["name"]).strip()
        existing = db.session.scalar(select(Environment).filter(
            Environment.project_id == env.project_id,
            Environment.name == name,
            Environment.id != env_id,
        ))
        if existing:
            return _error(400, "环境名称已存在")
        env.name = name

    if "base_url" in data:
        env.base_url = str(data["base_url"]).strip()

    if "variables" in data:
        variables = data["variables"]
        # 验证 variables 类型（与 v1 update 语义一致：非 dict/list → 400）
        if isinstance(variables, dict):
            # 限制变量数量
            if len(variables) > 100:
                return _error(400, f"环境变量不能超过100个，当前有{len(variables)}个")
            env.variables = variables
        elif isinstance(variables, list):
            return _error(400, 'variables 必须是对象类型（如 {"key": "value"}），不能是数组')
        else:
            return _error(400, "variables 格式不正确，必须是有效的 JSON 对象")

    if "headers" in data:
        env.headers = data["headers"]

    if "is_default" in data and data["is_default"]:
        db.session.execute(
            update(Environment).filter(
                Environment.project_id == env.project_id,
                Environment.id != env_id,
                Environment.is_default == True,  # noqa: E712
            ).values(is_default=False)
        )
        env.is_default = True

    db.session.commit()

    # 失效环境列表缓存
    _invalidate_env_cache(user_id)

    return _success(data=env.to_dict(), message="更新成功")


@router.delete("/api/v1/environments/{env_id}")
@release_session
def delete_environment(env_id: int, user: User = Depends(_current_user)):
    """删除环境（IDOR 修复：越权 404）"""
    user_id = user.id

    env = _get_owned_environment(user_id, env_id)
    if not env:
        return _error(404, "环境不存在")

    db.session.delete(env)
    db.session.commit()

    # 失效环境列表缓存
    _invalidate_env_cache(user_id)

    return _success(message="删除成功")


@router.post("/api/v1/environments/{env_id}/default")
@release_session
def set_default_environment(env_id: int, user: User = Depends(_current_user)):
    """设置默认环境（IDOR 修复：越权 404）"""
    env = _get_owned_environment(user.id, env_id)
    if not env:
        return _error(404, "环境不存在")

    # 取消其他环境的默认状态
    db.session.execute(
        update(Environment).filter(
            Environment.project_id == env.project_id,
            Environment.is_default == True,  # noqa: E712
        ).values(is_default=False)
    )

    env.is_default = True
    db.session.commit()

    return _success(message="设置成功")


# ==================== 导出 / 导入 ====================

@router.get("/api/v1/environments/{env_id}/export")
@release_session
def export_environment(env_id: int, user: User = Depends(_current_user)):
    """导出环境变量为 JSON（IDOR 修复：越权 404；v1 缺 datetime import 已补）"""
    env = _get_owned_environment(user.id, env_id)
    if not env:
        return _error(404, "环境不存在")

    export_data = {
        "version": "1.0",
        "export_time": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        "environment": {
            "name": env.name,
            "base_url": env.base_url,
            "variables": env.variables or {},
            "headers": env.headers or {},
            # v1 引用 env.description（模型无此列 → AttributeError），
            # 平迁以 getattr 输出该字段，保持响应结构
            "description": getattr(env, "description", None),
        },
    }

    return _success(data=export_data)


@router.post("/api/v1/projects/{project_id}/environments/import")
@release_session
def import_environment(
    project_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """
    导入环境变量（IDOR 修复：v1 越权 403 → 平迁 404）

    请求体:
        data: 导出的 JSON 数据
        mode: 'override'（覆盖）或 'merge'（合并）

    支持两种格式：
    1. 导出的 JSON 格式
    2. .env 文件格式（KEY=VALUE）

    v1 修复：v1 创建/查询 Environment 传 user_id（模型无此列，必然 400），
    平迁去掉 user_id，归属由 project_id + 属主校验保证。
    """
    user_id = user.id

    # 验证项目权限（越权/不存在 → 404）
    project = db.session.scalar(select(Project).filter_by(id=project_id, owner_id=user_id))
    if not project:
        return _error(404, "项目不存在")

    data = data or {}
    import_data = data.get("data")
    mode = data.get("mode", "merge")

    if not import_data:
        return _error(400, "缺少导入数据")

    try:
        # 解析导入数据
        if isinstance(import_data, str):
            # .env 格式
            variables = {}
            for line in import_data.strip().split("\n"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    variables[key.strip()] = value.strip().strip("\"'")

            env_name = f'导入的环境 {datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y%m%d%H%M%S")}'
            env = Environment(
                name=env_name,
                project_id=project_id,
                # 模型 base_url 为 NOT NULL，.env 格式无该信息，落空串
                base_url="",
                variables=variables,
            )
            db.session.add(env)
        elif isinstance(import_data, dict):
            # JSON 格式
            env_info = import_data.get("environment", import_data)
            env_name = env_info.get(
                "name",
                f'导入的环境 {datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y%m%d%H%M%S")}',
            )

            # 查找同名环境（按项目归属；v1 额外过滤不存在的 user_id 列已移除）
            existing = db.session.scalar(select(Environment).filter_by(
                project_id=project_id,
                name=env_name,
            ))

            if existing and mode == "override":
                existing.variables = env_info.get("variables", {})
                existing.headers = env_info.get("headers", {})
                existing.base_url = env_info.get("base_url", existing.base_url)
                existing.description = env_info.get("description", getattr(existing, "description", None))
                env = existing
            elif existing and mode == "merge":
                # v1 修复：v1 直接 in-place update 同一个 dict 再赋回（同对象赋值），
                # SQLAlchemy 变更检测判定无变化 → 不发 UPDATE（静默丢数据）。
                # 平迁改为 copy-on-write，赋新 dict 让变更生效。
                existing_vars = dict(existing.variables or {})
                existing_vars.update(env_info.get("variables", {}))
                existing.variables = existing_vars
                env = existing
            else:
                env = Environment(
                    name=env_name,
                    project_id=project_id,
                    base_url=env_info.get("base_url", ""),
                    variables=env_info.get("variables", {}),
                    headers=env_info.get("headers", {}),
                )
                db.session.add(env)
        else:
            return _error(400, "不支持的数据格式")

        db.session.commit()

        return _success(data=env.to_dict(), message=f"导入成功（{mode}模式）")
    except Exception as exc:
        db.session.rollback()
        return _error(400, f"导入失败: {exc}")


@router.get("/api/v1/environments/{env_id}/export-docker")
@release_session
def export_environment_docker(env_id: int, user: User = Depends(_current_user)):
    """导出环境配置为 Docker Compose 格式（IDOR 修复：越权 404）"""
    env = _get_owned_environment(user.id, env_id)
    if not env:
        return _error(404, "环境不存在")

    # 构建 .env 文件内容
    env_lines = [
        "# FullScopeTest 环境配置",
        f"# 环境名称: {env.name}",
        f"# 导出时间: {datetime.now(timezone.utc).replace(tzinfo=None).isoformat()}",
        "",
        f"BASE_URL={env.base_url or ''}",
    ]
    for key, value in (env.variables or {}).items():
        env_lines.append(f"{key}={value}")

    # 构建 docker-compose snippet
    compose_lines = [
        "# FullScopeTest 环境配置片段",
        "# 将以下内容添加到 docker-compose.yml 的 service 配置中",
        f"# 环境: {env.name}",
        "",
        "environment:",
    ]
    for key, value in (env.variables or {}).items():
        compose_lines.append(f"  - {key}=${{{key}}}")

    return _success(
        data={
            "env_file": "\n".join(env_lines),
            "compose_snippet": "\n".join(compose_lines),
            "environment_name": env.name,
        }
    )
