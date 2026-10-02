"""
性能测试模块 - v1 平迁路由（自 app/api/perf_test.py 平迁，路径不变）

- 挂载前缀：/api/v1/perf-test（与 Flask 蓝图 url_prefix='/api/v1' + '/perf-test/...' 一致）
- @jwt_required → Depends(get_current_user)
- 资源查询统一加 user.id 属主过滤（IDOR 修复）：越权访问一律 404
  * v1 的 GET /compare 仅做组织过滤（无组织上下文时不过滤），平迁后改为
    join 场景按 user_id 过滤
  * v1 的 POST /baselines 会把他人场景的基线置为非活跃、POST /baselines/compare
    可读取他人基线，平迁后均校验场景属主（越权 404）并将基线查询限定到本人
- target_url 校验：保留源文件 API 层的 is_safe_url SSRF 校验；
  任务执行期的复验逻辑位于 tasks 层（本模块不改其行为）
- Celery 派发保持原调用：run_perf_test_task.apply_async(args=[...],
  task_id='perf_test_{scenario_id}_{user_id}')；停止/删除时
  celery.control.revoke(task_id, terminate=True)
- 全部同步 def；响应体结构与 v1 完全一致
  （success: {code, message, data, timestamp}；
    error: {code, message, errors, request_id, timestamp}）
"""
from sqlalchemy import select, update
from app.database import paginate

import json
from datetime import datetime, timezone
from typing import Any, Dict, Tuple
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from app.core.runtime import get_config

from app.api.v2.deps import get_current_user, json_body, query_int, query_str
from app.core.logging import get_logger
from app.extensions import db, celery
from app.models.perf_baseline import PerfBaseline
from app.models.perf_test_result import PerformanceMetricSample, PerformanceTestResult
from app.models.perf_test_scenario import PerfTestScenario
from app.models.user import User
from app.services.perf_test_service import PerfTestService
from app.tasks import run_perf_test_task
from app.utils.ai_script_generator import generate_test_script
from app.utils.exceptions import NotFoundError
from app.utils.url_safety import is_safe_url
from app.utils.validators import is_valid_http_method, is_valid_url, validate_required

logger = get_logger(__name__)

# 初始化 Service
perf_test_service = PerfTestService()

router = APIRouter(prefix="/api/v1/perf-test", tags=["perf-test"])


# ==================== v1 响应结构（与 app/utils/response.py 字段一致） ====================

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat() + "Z"


def _request_id() -> str:
    try:
        from app.core.runtime import ctx

        return ctx.get_request_id() or ""
    except Exception:
        return ""


def success_response(data=None, message="success", code=200):
    """等价 v1 success_response：{code, message, data, timestamp}"""
    return JSONResponse(
        status_code=code,
        content={"code": code, "message": message, "data": data, "timestamp": _now_iso()},
    )


def error_response(code, message, errors=None):
    """等价 v1 error_response：{code, message, errors, request_id, timestamp}"""
    return JSONResponse(
        status_code=code,
        content={
            "code": code,
            "message": message,
            "errors": errors,
            "request_id": _request_id(),
            "timestamp": _now_iso(),
        },
    )


def paginate_response(items, total, page, per_page, message="success"):
    """等价 v1 paginate_response：data.items + data.pagination"""
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
            "timestamp": _now_iso(),
        },
    )


# ==================== URL 解析与校验工具（自源文件平迁） ====================

def _parse_target_url(url: str) -> Tuple[str, str]:
    """
    解析目标 URL，提取 base_host 和 endpoint_path

    Returns:
        (base_host, endpoint_path):
            - base_host: https://api.example.com（用于 Locust --host）
            - endpoint_path: /v1/users（用于脚本中的路径）
    """
    try:
        parsed = urlparse(url)
        base_host = f"{parsed.scheme}://{parsed.netloc}"
        endpoint_path = parsed.path or "/"
        return base_host, endpoint_path
    except Exception:
        return url, "/"


def _get_perf_limits() -> dict:
    limits = get_config().get("PERF_TEST_LIMITS", {})
    return {
        "min_users": limits.get("min_users", 1),
        "max_users": limits.get("max_users", 2000),
        "min_spawn_rate": limits.get("min_spawn_rate", 1),
        "max_spawn_rate": limits.get("max_spawn_rate", 50),
        "min_duration": limits.get("min_duration", 10),
        "max_duration": limits.get("max_duration", 3600),
    }


def _parse_int(value, field_name: str):
    try:
        return int(value), None
    except (TypeError, ValueError):
        return None, f"{field_name} must be an integer"


def _parse_bool(value, field_name: str):
    if isinstance(value, bool):
        return value, None
    return None, f"{field_name} must be a boolean"


def _validate_perf_numbers(user_count, spawn_rate, duration):
    limits = _get_perf_limits()

    user_count, error = _parse_int(user_count, "user_count")
    if error:
        return None, error
    spawn_rate, error = _parse_int(spawn_rate, "spawn_rate")
    if error:
        return None, error
    duration, error = _parse_int(duration, "duration")
    if error:
        return None, error

    if not limits["min_users"] <= user_count <= limits["max_users"]:
        return None, f'user_count must be between {limits["min_users"]} and {limits["max_users"]}'
    if not limits["min_spawn_rate"] <= spawn_rate <= limits["max_spawn_rate"]:
        return None, f'spawn_rate must be between {limits["min_spawn_rate"]} and {limits["max_spawn_rate"]}'
    if not limits["min_duration"] <= duration <= limits["max_duration"]:
        return None, f'duration must be between {limits["min_duration"]} and {limits["max_duration"]} seconds'

    return (user_count, spawn_rate, duration), None


def _validate_step_load_config(step_load_enabled, step_users, step_duration):
    limits = _get_perf_limits()

    step_load_enabled, error = _parse_bool(step_load_enabled, "step_load_enabled")
    if error:
        return None, error

    parsed_step_users = None
    if step_users is not None:
        parsed_step_users, error = _parse_int(step_users, "step_users")
        if error:
            return None, error
        if not 1 <= parsed_step_users <= limits["max_users"]:
            return None, f'step_users must be between 1 and {limits["max_users"]}'

    parsed_step_duration = None
    if step_duration is not None:
        parsed_step_duration, error = _parse_int(step_duration, "step_duration")
        if error:
            return None, error
        if not 1 <= parsed_step_duration <= limits["max_duration"]:
            return None, f'step_duration must be between 1 and {limits["max_duration"]} seconds'

    if step_load_enabled and parsed_step_users is None:
        return None, "step_users is required when step_load_enabled is true"
    if step_load_enabled and parsed_step_duration is None:
        return None, "step_duration is required when step_load_enabled is true"

    if not step_load_enabled:
        parsed_step_users = parsed_step_users if parsed_step_users is not None else 10
        parsed_step_duration = parsed_step_duration if parsed_step_duration is not None else 30

    return (step_load_enabled, parsed_step_users, parsed_step_duration), None


def _generate_locust_script(method: str, endpoint_path: str,
                            headers: dict = None, body: dict = None) -> str:
    """根据请求方法生成 Locust 脚本（与源文件一致）"""
    method = method.upper()
    endpoint_path = endpoint_path or "/"

    headers_code = ""
    if headers:
        headers_items = [f'            "{k}": "{v}"' for k, v in headers.items()]
        headers_joined = ",\n".join(headers_items)
        headers_code = f"""

        # 请求头
        headers = {{
{headers_joined}
        }}
"""

    if method == "GET":
        request_code = f'self.client.get("{endpoint_path}"'
        if headers:
            request_code += ", headers=headers"
        request_code += ")"
    elif method == "POST":
        body_str = json.dumps(body, ensure_ascii=False) if body else "{}"
        request_code = f'self.client.post("{endpoint_path}", json={body_str}'
        if headers:
            request_code += ", headers=headers"
        request_code += ")"
    elif method == "PUT":
        body_str = json.dumps(body, ensure_ascii=False) if body else "{}"
        request_code = f'self.client.put("{endpoint_path}", json={body_str}'
        if headers:
            request_code += ", headers=headers"
        request_code += ")"
    elif method == "DELETE":
        request_code = f'self.client.delete("{endpoint_path}"'
        if headers:
            request_code += ", headers=headers"
        request_code += ")"
    else:
        request_code = f'self.client.get("{endpoint_path}")'

    script = f'''"""
Locust 性能测试脚本（自动生成）
"""
from locust import HttpUser, task, between
from sqlalchemy import select, update

class TestUser(HttpUser):
    wait_time = between(1, 2)
{headers_code}
    @task
    def test_endpoint(self):
        """测试接口"""
        {request_code}
'''
    return script


# ==================== 健康检查 ====================

@router.get("/health")
def perf_test_health():
    """性能测试模块健康检查"""
    return success_response(message="性能测试模块正常")


# ==================== AI 脚本生成 ====================

@router.post("/ai/generate")
def generate_perf_script(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(get_current_user),
):
    """AI 生成性能测试脚本"""
    prompt = (data.get("prompt") or "").strip()

    if not prompt:
        return error_response(400, "prompt is required")

    try:
        from app.api.routes.api_test import _build_ai_runtime_config

        runtime_config = _build_ai_runtime_config(data)

        script_content = generate_test_script(prompt, "perf", runtime_config, user_id=user.id)
        return success_response(data={"script_content": script_content}, message="AI 脚本生成成功")
    except Exception as exc:
        return error_response(500, f"AI 脚本生成失败: {str(exc)}")


# ==================== 场景管理 ====================

@router.get("/scenarios")
def get_scenarios(request: Request, user: User = Depends(get_current_user)):
    """获取性能测试场景列表"""
    project_id = query_int(request, "project_id", 0)
    try:
        data = perf_test_service.get_scenarios(user.id, project_id or None)
        return success_response(data=data)
    except Exception as exc:
        logger.error("get perf scenarios failed", error=str(exc))
        return error_response(500, f"获取场景失败: {str(exc)}")


@router.post("/scenarios")
def create_scenario(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(get_current_user),
):
    """Create a performance test scenario."""
    error = validate_required(data, ["name"])
    if error:
        return error_response(400, error)

    target_url = data.get("target_url", "http://localhost:8080")
    if not is_valid_url(target_url):
        return error_response(400, "target_url must be a valid http/https URL")

    # SSRF 防护：校验目标地址
    safe, reason = is_safe_url(target_url)
    if not safe:
        return error_response(400, reason)

    method = data.get("method", "GET").upper()
    if not is_valid_http_method(method):
        return error_response(400, "method must be a valid HTTP method")

    headers = data.get("headers")
    if headers is not None and not isinstance(headers, dict):
        return error_response(400, "headers must be an object")

    body = data.get("body")
    user_count = data.get("user_count", 10)
    spawn_rate = data.get("spawn_rate", 1)
    duration = data.get("duration", 60)
    step_load_enabled = data.get("step_load_enabled", False)
    step_users = data.get("step_users")
    step_duration = data.get("step_duration")

    numbers, error = _validate_perf_numbers(user_count, spawn_rate, duration)
    if error:
        return error_response(400, error)
    user_count, spawn_rate, duration = numbers

    step_config, error = _validate_step_load_config(step_load_enabled, step_users, step_duration)
    if error:
        return error_response(400, error)
    step_load_enabled, step_users, step_duration = step_config

    # Generate script when no custom script is provided.
    script_content = data.get("script_content")
    if not script_content:
        _, endpoint_path = _parse_target_url(target_url)
        script_content = _generate_locust_script(method, endpoint_path, headers, body)

    scenario = PerfTestScenario(
        name=data["name"],
        description=data.get("description", ""),
        target_url=target_url,
        method=method,
        headers=headers,
        body=body,
        user_count=user_count,
        spawn_rate=spawn_rate,
        duration=duration,
        step_load_enabled=step_load_enabled,
        step_users=step_users,
        step_duration=step_duration,
        project_id=data.get("project_id"),
        user_id=user.id,
        script_content=script_content,
    )

    db.session.add(scenario)
    db.session.commit()

    return success_response(data=scenario.to_dict(), message="Created")


@router.get("/scenarios/{scenario_id}")
def get_scenario(scenario_id: int, user: User = Depends(get_current_user)):
    """获取场景详情"""
    try:
        result = perf_test_service.get_scenario(scenario_id, user.id)
        return success_response(data=result)
    except NotFoundError as exc:
        return error_response(404, str(exc))
    except Exception as exc:
        logger.error("get perf scenario failed", error=str(exc))
        return error_response(500, f"获取场景失败: {str(exc)}")


@router.put("/scenarios/{scenario_id}")
def update_scenario(
    scenario_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(get_current_user),
):
    """Update a performance test scenario."""
    scenario = db.session.scalar(select(PerfTestScenario).filter_by(id=scenario_id, user_id=user.id))

    if not scenario:
        return error_response(404, "Scenario not found")

    if "step_load_enabled" in data:
        requested_step_enabled, error = _parse_bool(data["step_load_enabled"], "step_load_enabled")
        if error:
            return error_response(400, error)
        if requested_step_enabled and ("step_users" not in data or "step_duration" not in data):
            return error_response(400, "step_users and step_duration are required when step_load_enabled is true")

    if "target_url" in data:
        if not is_valid_url(data["target_url"]):
            return error_response(400, "target_url must be a valid http/https URL")
        # SSRF 防护：校验目标地址
        safe, reason = is_safe_url(data["target_url"])
        if not safe:
            return error_response(400, reason)
        scenario.target_url = data["target_url"]

    if "method" in data:
        method = str(data["method"]).upper()
        if not is_valid_http_method(method):
            return error_response(400, "method must be a valid HTTP method")
        scenario.method = method

    if "headers" in data:
        headers = data["headers"]
        if headers is not None and not isinstance(headers, dict):
            return error_response(400, "headers must be an object")
        scenario.headers = headers

    if "body" in data:
        scenario.body = data["body"]

    if "name" in data:
        scenario.name = data["name"]
    if "description" in data:
        scenario.description = data["description"]
    if "script_content" in data:
        scenario.script_content = data["script_content"]

    limits = _get_perf_limits()
    if "user_count" in data:
        user_count, error = _parse_int(data["user_count"], "user_count")
        if error:
            return error_response(400, error)
        if not limits["min_users"] <= user_count <= limits["max_users"]:
            return error_response(400, f'user_count must be between {limits["min_users"]} and {limits["max_users"]}')
        scenario.user_count = user_count

    if "spawn_rate" in data:
        spawn_rate, error = _parse_int(data["spawn_rate"], "spawn_rate")
        if error:
            return error_response(400, error)
        if not limits["min_spawn_rate"] <= spawn_rate <= limits["max_spawn_rate"]:
            return error_response(400, f'spawn_rate must be between {limits["min_spawn_rate"]} and {limits["max_spawn_rate"]}')
        scenario.spawn_rate = spawn_rate

    if "duration" in data:
        duration, error = _parse_int(data["duration"], "duration")
        if error:
            return error_response(400, error)
        if not limits["min_duration"] <= duration <= limits["max_duration"]:
            return error_response(400, f'duration must be between {limits["min_duration"]} and {limits["max_duration"]} seconds')
        scenario.duration = duration

    next_step_load_enabled_raw = data["step_load_enabled"] if "step_load_enabled" in data else scenario.step_load_enabled
    next_step_users_raw = data["step_users"] if "step_users" in data else scenario.step_users
    next_step_duration_raw = data["step_duration"] if "step_duration" in data else scenario.step_duration

    step_config, error = _validate_step_load_config(
        next_step_load_enabled_raw,
        next_step_users_raw,
        next_step_duration_raw,
    )
    if error:
        return error_response(400, error)
    next_step_load_enabled, next_step_users, next_step_duration = step_config

    scenario.step_load_enabled = next_step_load_enabled
    scenario.step_users = next_step_users
    scenario.step_duration = next_step_duration

    db.session.commit()

    return success_response(data=scenario.to_dict(), message="Updated")


@router.delete("/scenarios/{scenario_id}")
def delete_scenario(scenario_id: int, user: User = Depends(get_current_user)):
    """删除性能测试场景"""
    try:
        # 如果正在运行，先停止
        scenario = db.session.scalar(select(PerfTestScenario).filter_by(id=scenario_id, user_id=user.id))
        if scenario and scenario.status == "running":
            task_id = f"perf_test_{scenario_id}_{user.id}"
            try:
                celery.control.revoke(task_id, terminate=True)
            except Exception as exc:
                logger.warning("Failed to revoke celery task", task_id=task_id, error=str(exc))

        perf_test_service.delete_scenario(scenario_id, user.id)
        return success_response(message="删除成功")
    except NotFoundError as exc:
        return error_response(404, str(exc))
    except Exception as exc:
        logger.error("delete perf scenario failed", error=str(exc))
        return error_response(500, f"删除场景失败: {str(exc)}")


# ==================== 执行测试 ====================

@router.post("/scenarios/{scenario_id}/run")
def run_scenario(
    scenario_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(get_current_user),
):
    """Run a performance test scenario (async)."""
    scenario = db.session.scalar(select(PerfTestScenario).filter_by(id=scenario_id, user_id=user.id))

    if not scenario:
        return error_response(404, "Scenario not found")

    if scenario.status == "running":
        return error_response(400, "Scenario is already running")

    try:
        if "step_load_enabled" in data:
            requested_step_enabled, error = _parse_bool(data["step_load_enabled"], "step_load_enabled")
            if error:
                return error_response(400, error)
            if requested_step_enabled and ("step_users" not in data or "step_duration" not in data):
                return error_response(400, "step_users and step_duration are required when step_load_enabled is true")

        user_count = data.get("user_count", scenario.user_count)
        spawn_rate = data.get("spawn_rate", scenario.spawn_rate)
        run_time = data.get("duration", scenario.duration)
        step_load_enabled_raw = data.get("step_load_enabled", scenario.step_load_enabled)
        step_users_raw = data.get("step_users", scenario.step_users)
        step_duration_raw = data.get("step_duration", scenario.step_duration)

        numbers, error = _validate_perf_numbers(user_count, spawn_rate, run_time)
        if error:
            return error_response(400, error)
        user_count, spawn_rate, run_time = numbers

        step_config, error = _validate_step_load_config(
            step_load_enabled_raw,
            step_users_raw,
            step_duration_raw,
        )
        if error:
            return error_response(400, error)
        step_load_enabled, step_users, step_duration = step_config

        scenario.status = "running"
        scenario.last_run_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.session.commit()

        task = run_perf_test_task.apply_async(
            args=[scenario_id, user_count, spawn_rate, run_time, step_load_enabled, step_users, step_duration],
            task_id=f"perf_test_{scenario_id}_{user.id}",
        )

        return success_response(data={
            "message": "Scenario submitted",
            "task_id": task.id,
            "scenario_id": scenario_id,
            "config": {
                "users": user_count,
                "spawn_rate": spawn_rate,
                "run_time": run_time,
                "step_load_enabled": step_load_enabled,
                "step_users": step_users,
                "step_duration": step_duration,
            },
        })

    except Exception as e:
        try:
            scenario = db.session.scalar(select(PerfTestScenario).filter_by(id=scenario_id, user_id=user.id))
            if scenario:
                scenario.status = "failed"
                scenario.last_result = {
                    "success": False,
                    "error": f"Failed to submit: {str(e)}",
                    "timestamp": datetime.now(timezone.utc).replace(tzinfo=None).isoformat() + "Z",
                }
                db.session.commit()
        except Exception:
            db.session.rollback()
        return error_response(500, f"Failed to submit: {str(e)}")


@router.post("/scenarios/{scenario_id}/stop")
def stop_scenario(scenario_id: int, user: User = Depends(get_current_user)):
    """停止运行中的性能测试"""
    scenario = db.session.scalar(select(PerfTestScenario).filter_by(id=scenario_id, user_id=user.id))

    if not scenario:
        return error_response(404, "场景不存在")

    if scenario.status != "running":
        return error_response(400, "测试未在运行")

    try:
        # 尝试撤销 Celery 任务
        task_id = f"perf_test_{scenario_id}_{user.id}"
        celery.control.revoke(task_id, terminate=True)

        # 更新状态
        scenario.status = "stopped"
        db.session.commit()

        return success_response(message="已停止")
    except Exception as e:
        return error_response(500, f"停止失败: {str(e)}")


@router.get("/scenarios/{scenario_id}/status")
def get_scenario_status(scenario_id: int, user: User = Depends(get_current_user)):
    """获取场景执行状态"""
    scenario = db.session.scalar(select(PerfTestScenario).filter_by(id=scenario_id, user_id=user.id))

    if not scenario:
        return error_response(404, "场景不存在")

    # 从 last_result 中获取实时数据，如果没有则使用数据库中的值
    return success_response(data={
        "status": scenario.status,
        "last_run_at": scenario.last_run_at.isoformat() + "Z" if scenario.last_run_at else None,
        "last_result": scenario.last_result,
        "avg_response_time": scenario.avg_response_time,
        "max_response_time": scenario.max_response_time,
        "min_response_time": scenario.min_response_time,
        "throughput": scenario.throughput,
        "error_rate": scenario.error_rate,
    })


# ==================== 性能测试历史对比 ====================

@router.get("/compare")
def compare_performance_runs(request: Request, user: User = Depends(get_current_user)):
    """
    性能测试历史对比 API

    查询参数:
        run_ids: 逗号分隔的测试结果 ID 列表（至少 2 个，最多 10 个）
    返回: 多次测试运行的关键指标对比，包含性能劣化百分比（相对于基准运行）

    IDOR 修复：仅允许对比本人场景下的测试结果（v1 仅做组织过滤，
    无组织上下文时任何用户可对比他人结果）。
    """
    run_ids_str = query_str(request, "run_ids", "").strip()
    if not run_ids_str:
        return error_response(400, "run_ids 参数不能为空")

    try:
        run_ids = [int(rid.strip()) for rid in run_ids_str.split(",") if rid.strip()]
    except ValueError:
        return error_response(400, "run_ids 必须是逗号分隔的整数列表")

    if len(run_ids) < 2:
        return error_response(400, "至少需要 2 个测试运行 ID 进行对比")
    if len(run_ids) > 10:
        return error_response(400, "最多支持 10 个测试运行 ID")

    # 查询所有匹配的结果（属主过滤：join 场景限制 user_id）
    results = (
        db.session.scalars(select(PerformanceTestResult).join(PerfTestScenario).filter(PerfTestScenario.user_id == user.id).filter(PerformanceTestResult.id.in_(run_ids))).all())

    if len(results) == 0:
        return error_response(404, "未找到匹配的性能测试结果")

    if len(results) != len(run_ids):
        found_ids = {r.id for r in results}
        missing = [rid for rid in run_ids if rid not in found_ids]
        return error_response(404, f'未找到测试结果 ID: {", ".join(str(m) for m in missing)}')

    # 按创建时间排序，第一个作为基准
    results_sorted = sorted(results, key=lambda r: r.created_at or datetime.min)
    baseline = results_sorted[0]

    def _calc_degradation(current_val, base_val):
        """计算性能劣化百分比"""
        if base_val is None or current_val is None or base_val == 0:
            return None
        return round(((current_val - base_val) / base_val) * 100, 2)

    comparison_runs = []
    for r in results_sorted:
        comparison_runs.append({
            "id": r.id,
            "scenario_id": r.scenario_id,
            "user_count": r.user_count,
            "spawn_rate": r.spawn_rate,
            "duration": r.duration,
            "status": r.status,
            "started_at": r.started_at.isoformat() + "Z" if r.started_at else None,
            "finished_at": r.finished_at.isoformat() + "Z" if r.finished_at else None,
            "metrics": {
                "total_requests": r.total_requests,
                "total_failures": r.total_failures,
                "error_rate": r.error_rate,
                "rps": r.rps,
                "avg_response_time": r.avg_response_time,
                "min_response_time": r.min_response_time,
                "max_response_time": r.max_response_time,
                "p50_response_time": r.p50_response_time,
                "p75_response_time": r.p75_response_time,
                "p95_response_time": r.p95_response_time,
                "p99_response_time": r.p99_response_time,
            },
            "degradation": {
                "rps": _calc_degradation(r.rps, baseline.rps),
                "avg_response_time": _calc_degradation(r.avg_response_time, baseline.avg_response_time),
                "p95_response_time": _calc_degradation(r.p95_response_time, baseline.p95_response_time),
                "p99_response_time": _calc_degradation(r.p99_response_time, baseline.p99_response_time),
                "error_rate": _calc_degradation(r.error_rate, baseline.error_rate),
            } if r.id != baseline.id else None,
        })

    return success_response(data={
        "runs": comparison_runs,
        "baseline_id": baseline.id,
        "comparison_count": len(comparison_runs),
    })


# ==================== 历史测试结果 ====================

@router.get("/results")
def get_performance_results(request: Request, user: User = Depends(get_current_user)):
    """
    获取性能测试历史结果列表（分页）

    查询参数:
        project_id: 按项目过滤
        scenario_id: 按场景过滤
        status: 按状态过滤（passed/completed/failed/stopped）
        start_date: 开始日期（YYYY-MM-DD）
        end_date: 结束日期（YYYY-MM-DD）
        page: 页码（默认 1）
        per_page: 每页数量（默认 20）
    """
    user_id = user.id
    page = query_int(request, "page", 1)
    per_page = query_int(request, "per_page", 20)
    project_id = query_int(request, "project_id", 0)
    scenario_id = query_int(request, "scenario_id", 0)
    status = query_str(request, "status", "").strip()
    start_date = query_str(request, "start_date", "").strip()
    end_date = query_str(request, "end_date", "").strip()

    query = select(PerformanceTestResult).join(PerfTestScenario).filter(
        PerfTestScenario.user_id == user_id
    )

    if project_id:
        query = query.filter(PerformanceTestResult.project_id == project_id)
    if scenario_id:
        query = query.filter(PerformanceTestResult.scenario_id == scenario_id)
    if status:
        # 前端传递 passed/failed，后端存储 completed/failed
        status_map = {"passed": "completed", "failed": "failed"}
        db_status = status_map.get(status, status)
        query = query.filter(PerformanceTestResult.status == db_status)
    if start_date:
        try:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            query = query.filter(PerformanceTestResult.created_at >= start_dt)
        except ValueError:
            pass
    if end_date:
        try:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            query = query.filter(PerformanceTestResult.created_at <= end_dt)
        except ValueError:
            pass

    pagination = paginate(
        query.order_by(PerformanceTestResult.created_at.desc()),
        page=page, per_page=per_page,
    )

    return paginate_response(
        items=[r.to_dict() for r in pagination.items],
        total=pagination.total,
        page=page,
        per_page=per_page,
    )


@router.get("/results/{result_id}/metrics")
def get_performance_result_metrics(result_id: int, request: Request, user: User = Depends(get_current_user)):
    """
    获取某个测试结果的时间序列指标采样数据

    查询参数:
        limit: 最多返回的采样点数（默认全部）
    """
    result = db.session.scalar(select(PerformanceTestResult).join(PerfTestScenario).filter(
        PerformanceTestResult.id == result_id,
        PerfTestScenario.user_id == user.id,
    ))

    if not result:
        return error_response(404, "测试结果不存在")

    limit = query_int(request, "limit", 0)

    query = select(PerformanceMetricSample).filter_by(
        test_result_id=result_id
    ).order_by(PerformanceMetricSample.elapsed_seconds.asc())

    if limit:
        query = query.limit(limit)

    samples = db.session.scalars(query).all()

    return success_response(data={
        "result": result.to_dict(),
        "metrics": [s.to_dict() for s in samples],
        "total_samples": len(samples),
    })


@router.get("/running")
def get_running_tests(user: User = Depends(get_current_user)):
    """获取当前用户运行中的测试列表"""
    # 查询数据库中状态为 running 的场景
    running_scenarios = db.session.scalars(select(PerfTestScenario).filter_by(
        user_id=user.id,
        status="running",
    )).all()

    user_tests = [{
        "id": s.id,  # 前端需要 id 字段
        "scenario_id": s.id,
        "name": s.name,
        "user_count": s.user_count,
        "duration": s.duration,
        "elapsed": 0,  # 计算已运行时间
        "status": s.status,
        "avg_response_time": s.avg_response_time or 0,
        "throughput": s.throughput or 0,
        "error_rate": s.error_rate or 0,
        "started_at": s.last_run_at.isoformat() + "Z" if s.last_run_at else None,
    } for s in running_scenarios]

    return success_response(data=user_tests)


# ==================== 性能测试基线 ====================

@router.get("/baselines")
def get_perf_baselines(request: Request, user: User = Depends(get_current_user)):
    """获取性能测试基线列表"""
    scenario_id = query_int(request, "scenario_id", 0)

    query = select(PerfBaseline).filter_by(user_id=user.id)
    if scenario_id:
        query = query.filter_by(scenario_id=scenario_id)

    baselines = db.session.scalars(query.order_by(PerfBaseline.created_at.desc())).all()
    return success_response(data=[b.to_dict() for b in baselines])


@router.post("/baselines")
def create_baseline(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(get_current_user),
):
    """创建性能测试基线"""
    scenario_id = data.get("scenario_id")
    if not scenario_id:
        return error_response(400, "缺少 scenario_id")

    # IDOR 修复：校验场景属主（v1 未校验，可为他人的场景创建基线）
    scenario = db.session.scalar(select(PerfTestScenario).filter_by(id=scenario_id, user_id=user.id))
    if not scenario:
        return error_response(404, "场景不存在")

    # 将同场景的其他基线设为非活跃（仅本人基线，IDOR 修复）
    db.session.execute(
        update(PerfBaseline)
        .filter_by(scenario_id=scenario_id, user_id=user.id, is_active=True)
        .values(is_active=False)
    )

    baseline = PerfBaseline(
        scenario_id=scenario_id,
        user_id=user.id,
        name=data.get("name", f'基线 {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")}'),
        metrics=data.get("metrics", {}),
        run_id=data.get("run_id"),
        is_active=True,
    )
    db.session.add(baseline)
    db.session.commit()

    return success_response(data=baseline.to_dict(), message="基线已创建", code=200)


@router.post("/baselines/compare")
def compare_with_baseline(
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(get_current_user),
):
    """将当前运行结果与基线对比，检测退化"""
    scenario_id = data.get("scenario_id")
    current_metrics = data.get("current_metrics", {})

    if not scenario_id:
        return error_response(400, "缺少 scenario_id")

    # IDOR 修复：仅允许对比本人场景（v1 未校验，可读取他人基线指标）
    scenario = db.session.scalar(select(PerfTestScenario).filter_by(id=scenario_id, user_id=user.id))
    if not scenario:
        return error_response(404, "场景不存在")

    baseline = db.session.scalar(select(PerfBaseline).filter_by(
        scenario_id=scenario_id, user_id=user.id, is_active=True
    ))

    if not baseline:
        return success_response(data={"has_baseline": False, "message": "无活跃基线"})

    base = baseline.metrics or {}
    comparison = {}
    degraded = []

    # 对比关键指标
    for key in ["p50", "p90", "p95", "p99", "avg", "error_rate"]:
        base_val = base.get(key, 0)
        curr_val = current_metrics.get(key, 0)
        if base_val > 0:
            change_pct = ((curr_val - base_val) / base_val) * 100
            comparison[key] = {
                "baseline": base_val,
                "current": curr_val,
                "change_pct": round(change_pct, 1),
                "degraded": change_pct > 20,
            }
            if change_pct > 20:
                degraded.append(key)

    return success_response(data={
        "has_baseline": True,
        "baseline": baseline.to_dict(),
        "comparison": comparison,
        "degraded_metrics": degraded,
        "is_degraded": len(degraded) > 0,
    })
