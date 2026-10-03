"""
deepagents 智能体服务 —— 平台 AI 能力的 Agent 化实现（默认 DeepSeek）

与 copilot 的两段式 Function Calling 不同，这里使用 langchain-ai/deepagents
（基于 langgraph）构建真正的智能体循环：

- 多步推理：模型可以连续调用工具、观察结果、再决定下一步，直到完成任务
- 任务规划：内置 write_todos 工具，多步任务先列计划再执行
- 虚拟文件系统：内置 ls/read_file/write_file/edit_file（内存态，不落盘）
- 每轮对话结束写入 AIInvocationLog（feature='agent_chat'）

模型走 OpenAI 兼容协议，默认 DeepSeek（https://api.deepseek.com/v1 + deepseek-chat）。

降级策略：**没有降级**。未配置 key、依赖缺失或执行失败一律写失败日志后抛异常，
由路由层返回明确错误，绝不返回伪造的兜底回复。

工具全部按 user_id 收敛到当前用户自有数据（越权铁律），只读工具加属主过滤。
"""

import json
import time
from typing import Any, Dict, List, Optional

import requests

from ...extensions import db
from ...models.ai_invocation_log import AIInvocationLog
from ...core.logging import get_logger

logger = get_logger(__name__)

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

AGENT_SYSTEM_PROMPT = """你是"大熊AI测试平台"的智能体（Agent），帮助用户完成测试相关任务。

你可以：
- 检索并调用平台全部业务接口（300+ 条：API 测试/Web UI/APP/性能压测/测试计划/报告/Mock/环境等）
- 抓取 API 文档 URL 并解析出端点清单（自动发现 Redoc/Swagger UI 背后的 OpenAPI 规范）
- 对目标接口发真实请求做探活验证
- 创建和管理性能测试场景（Locust 压测）
- 为已创建的压测场景生成定制化 Locust 业务脚本
- 查询最近失败的 Web UI 测试
- 查询用户的 API 测试用例
- 用 load_skill 随时查阅平台能力手册（工具规范/业务域地图/工作流）
- 对多步任务先用 write_todos 列出计划，再逐步执行并更新状态

平台接口调用规范：
- 要调用平台接口时：先 search_platform_apis 查到准确路径模板，再 call_platform_api 执行
- call_platform_api 以当前用户身份执行，属主过滤由路由层强制；查不到别人的资源属正常，不要重试
- 平台信封 {"code": 200, ...}，创建类也返回 200；400 参数错误、404 多为不属当前用户
- DELETE 必须来自用户明确意图；批量删除先列出将删的 id 让用户确认
- 首次接触某业务域任务时可用 load_skill 查看对应工作流（如"典型工作流"章节）

API 文档分析流程（用户给出 API 文档/接口文档 URL，或要求"分析这个 API 生成压测"时）：
1. 先 fetch_api_docs 抓取并解析端点清单（接受文档页 URL 或 openapi.json 直链）
2. 用 probe_api_endpoint 对关键接口探活：GET 优先；POST/PUT 用文档示例值；
   探针发现的 401/404/5xx 必须在压测方案中注明或规避，不要假设接口可用
3. 基于真实端点清单设计业务压测场景：按用户旅程分组（浏览/下单链路等），
   读接口高权重、写接口低权重，明确每个场景压哪些接口链路
4. 每个场景先 create_performance_test 落库，再 generate_performance_script
   生成多接口业务脚本（requirements 里带上场景的接口链路与权重）

压测场景创建准则：
- 用户提出压测需求时：先 create_performance_test 落库场景，再调用 generate_performance_script
  生成定制化业务脚本（除非用户明确说"只要默认模板"）
- 生成脚本时把用户的完整需求（接口、断言、参数化、请求链等）传入 requirements
- 用户的意图明确时才执行创建类操作；信息不足时先询问，不要编造参数
- 回答用中文，结论先行，简洁专业
- 工具返回的数据是唯一事实来源，不要虚构测试结果"""


def _resolve_config(config: Dict[str, Any]) -> Dict[str, str]:
    """配置解析：显式 config > 环境变量 > 默认值（与 AIServiceBase 一致）"""
    import os

    return {
        "base_url": str(
            config.get("AI_ASSISTANT_BASE_URL")
            or os.environ.get("AI_ASSISTANT_BASE_URL")
            or DEFAULT_BASE_URL
        ).rstrip("/"),
        "api_key": str(
            config.get("AI_ASSISTANT_API_KEY")
            or os.environ.get("AI_ASSISTANT_API_KEY")
            or ""
        ).strip(),
        "model": str(
            config.get("AI_ASSISTANT_MODEL")
            or os.environ.get("AI_ASSISTANT_MODEL")
            or DEFAULT_MODEL
        ),
        "timeout": int(
            config.get("AI_ASSISTANT_TIMEOUT")
            or os.environ.get("AI_ASSISTANT_TIMEOUT")
            or 60
        ),
    }


def _build_tools(user_id: int, config: Dict[str, Any] = None) -> List[Any]:
    """构建当前用户作用域内的智能体工具（闭包注入 user_id，属主过滤铁律）"""
    from langchain_core.tools import tool

    config = config or {}

    @tool
    def create_performance_test(
        name: str,
        target_url: str = "http://example.com",
        endpoint_path: str = "/",
        concurrent_users: int = 10,
        duration_seconds: int = 60,
    ) -> str:
        """创建一个新的性能测试场景（Locust 压测）。用户明确要求创建压测/性能测试时使用。

        Args:
            name: 场景名称（简洁描述压测目标）
            target_url: 目标服务基地址（如 https://fakestoreapi.com）
            endpoint_path: 要压测的接口路径（如 /products），默认 /
            concurrent_users: 并发用户数（新手建议 5-20）
            duration_seconds: 持续时间（秒）
        """
        from urllib.parse import urlparse

        from ...models.perf_test_scenario import PerfTestScenario

        path = endpoint_path if endpoint_path.startswith("/") else "/" + endpoint_path
        # target_url 若带路径，路径并入脚本、基地址单独存
        parsed = urlparse(target_url)
        if parsed.path and parsed.path != "/":
            path = parsed.path
            target_url = f"{parsed.scheme}://{parsed.netloc}"
        script_content = (
            "from locust import HttpUser, task, between\n\n"
            "class QuickstartUser(HttpUser):\n"
            "    wait_time = between(1, 5)\n\n"
            "    @task\n"
            "    def test_target(self):\n"
            f'        self.client.get("{path}")\n'
        )
        scenario = PerfTestScenario(
            name=name,
            description=f"Agent created: {concurrent_users} VUs for {duration_seconds}s, path {path}",
            target_url=target_url,
            user_count=concurrent_users,
            duration=duration_seconds,
            script_content=script_content,
            status="pending",
            user_id=user_id,
        )
        db.session.add(scenario)
        db.session.commit()
        return json.dumps(
            {
                "status": "success",
                "scenario_id": scenario.id,
                "message": f"已创建性能测试场景 '{name}'（压测 {path}，并发 {concurrent_users}，时长 {duration_seconds} 秒）",
            },
            ensure_ascii=False,
        )

    @tool
    def generate_performance_script(scenario_id: int, requirements: str = "") -> str:
        """为已创建的压测场景生成定制化 Locust 业务脚本并回写场景。

        在 create_performance_test 之后调用；requirements 传入用户的完整需求
        （要压的接口、请求方法、断言、参数化、多接口链路等）。

        Args:
            scenario_id: create_performance_test 返回的场景 ID
            requirements: 业务需求描述（接口路径、请求体、断言、思考时间等）
        """
        from sqlalchemy import select

        from ...models.perf_test_scenario import PerfTestScenario
        from ...utils.ai_script_generator import generate_test_script
        from ...utils.sandbox import check_script_safety

        scenario = db.session.scalar(
            select(PerfTestScenario).filter_by(id=scenario_id, user_id=user_id)
        )
        if not scenario:
            return json.dumps(
                {"status": "error", "message": f"场景 {scenario_id} 不存在"},
                ensure_ascii=False,
            )

        prompt = (
            f"目标地址: {scenario.target_url}（Locust HttpUser 的 host，脚本内请求路径不要重复写完整域名）\n"
            f"并发用户数: {scenario.user_count}，持续时间: {scenario.duration} 秒\n"
            f"业务需求: {requirements or '对默认接口做 GET 压测'}\n"
            "生成完整的 Locust 压测脚本（HttpUser + @task）。"
        )
        script = generate_test_script(
            prompt, "perf", _resolve_config(config), user_id=user_id
        )
        safe, reason = check_script_safety(script, allow_network_libs=True)
        if not safe:
            return json.dumps(
                {"status": "error", "message": f"生成的脚本未通过安全检查: {reason}"},
                ensure_ascii=False,
            )

        scenario.script_content = script
        db.session.commit()
        return json.dumps(
            {
                "status": "success",
                "scenario_id": scenario.id,
                "message": f"已为场景 '{scenario.name}' 生成定制化压测脚本并保存",
                "script_preview": script[:400],
            },
            ensure_ascii=False,
        )

    @tool
    def fetch_api_docs(url: str) -> str:
        """抓取 API 文档 URL 并解析为端点清单。

        支持文档页地址（自动发现 Redoc/Swagger UI 背后的 OpenAPI/Swagger 规范，
        如 https://fakestoreapi.com/docs）或 spec 直链（openapi.json/swagger.yaml）。
        返回接口清单：方法、路径、参数、请求体 schema、描述。分析接口、设计压测场景前必须先用本工具摸清接口面。

        Args:
            url: API 文档页或 OpenAPI spec 的 URL
        """
        from .api_doc_importer import api_doc_importer

        try:
            spec = api_doc_importer.discover_spec(url.strip())
            inventory = api_doc_importer.parse_inventory(spec["raw"])
            return json.dumps(
                {
                    "status": "success",
                    "spec_url": spec["spec_url"],
                    "discovered_via": spec["discovered_via"],
                    **inventory,
                    "endpoints_truncated": inventory["endpoints_count"] > len(inventory["endpoints"]),
                },
                ensure_ascii=False,
            )
        except (ValueError, requests.exceptions.RequestException) as exc:
            return json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False)

    @tool
    def probe_api_endpoint(
        base_url: str,
        method: str = "GET",
        path: str = "/",
        path_values: str = "",
        query_params: str = "",
        headers_json: str = "",
        body_json: str = "",
    ) -> str:
        """对目标接口发一次真实请求，探活验证可用性与响应结构。

        压测前用它验证接口真实可用（fetch_api_docs 的 spec 可能滞后于服务实际行为）。
        path 中的 {param} 占位符用 path_values 的 JSON 填充（如 {"id": 1}）；
        query_params/body_json/headers_json 传 JSON 字符串（可为空串）。
        返回状态码、耗时、响应体预览。POST/PUT 探活用文档示例值，避免写脏数据。

        Args:
            base_url: 目标基地址（如 https://fakestoreapi.com）
            method: HTTP 方法（GET/POST/PUT/DELETE/PATCH）
            path: 接口路径，可含 {param} 占位符（如 /products/{id}）
            path_values: 路径占位符取值的 JSON 字符串（如 '{"id": 1}'，可为空）
            query_params: 查询参数 JSON 字符串（如 '{"limit": 5}'，可为空）
            headers_json: 额外请求头 JSON 字符串（可为空）
            body_json: 请求体 JSON 字符串（POST/PUT 时使用，可为空）
        """
        from .api_doc_importer import api_doc_importer

        def _loads(raw: str, field: str, default):
            if not raw or not str(raw).strip():
                return default
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{field} 不是合法 JSON: {exc}") from exc

        try:
            result = api_doc_importer.probe_endpoint(
                base_url.strip(),
                method,
                path,
                path_values=_loads(path_values, "path_values", {}),
                query_params=_loads(query_params, "query_params", {}),
                headers=_loads(headers_json, "headers_json", {}),
                body=_loads(body_json, "body_json", None),
            )
            # 探针结果的 status 是 HTTP 状态码，信封改用 http_status 避免覆盖
            return json.dumps(
                {
                    "status": "success",
                    "http_status": result["status"],
                    "latency_ms": result["latency_ms"],
                    "content_type": result["content_type"],
                    "body_preview": result["body_preview"],
                },
                ensure_ascii=False,
            )
        except (ValueError, requests.exceptions.RequestException) as exc:
            return json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False)

    @tool
    def search_platform_apis(keyword: str, limit: int = 15) -> str:
        """按关键字检索平台接口目录，拿到可调用的接口路径模板与参数定义。

        调用平台任何接口前先用本工具查准路径（call_platform_api 只接受目录里的模板）。
        keyword 匹配路径/摘要/标签，支持中文与英文，如"性能"、"scenario"、"用例"。

        Args:
            keyword: 检索关键字（空串返回目录前 15 条）
            limit: 最多返回条数（默认 15，上限 30）
        """
        from .platform_api_registry import get_catalog, search_apis

        try:
            hits = search_apis(keyword, limit)
            return json.dumps(
                {
                    "status": "success",
                    "total_in_catalog": len(get_catalog()),
                    "matches": hits,
                },
                ensure_ascii=False,
            )
        except Exception as exc:
            return json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False)

    @tool
    def call_platform_api(
        method: str,
        path: str,
        path_values: str = "",
        query_json: str = "",
        body_json: str = "",
    ) -> str:
        """以当前用户身份调用平台任意接口（完整鉴权与属主过滤，等同用户亲自操作）。

        path 必须是 search_platform_apis 返回的路径模板原样（含 {param} 占位符）。
        返回 {status: HTTP状态码, body: 响应文本}；平台成功统一 200，404 多为资源不属当前用户。

        Args:
            method: HTTP 方法（GET/POST/PUT/DELETE/PATCH）
            path: 接口路径模板，如 /api/v1/perf-test/scenarios/{scenario_id}
            path_values: 路径占位符取值 JSON 字符串（如 '{"scenario_id": 12}'，可为空）
            query_json: 查询参数 JSON 字符串（可为空）
            body_json: 请求体 JSON 字符串（POST/PUT/PATCH 时使用，可为空）
        """
        from .platform_api_registry import dispatch

        def _loads(raw: str, field: str, default):
            if not raw or not str(raw).strip():
                return default
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{field} 不是合法 JSON: {exc}") from exc

        try:
            result = dispatch(
                user_id,
                method,
                path,
                path_values=_loads(path_values, "path_values", {}),
                query=_loads(query_json, "query_json", {}),
                body=_loads(body_json, "body_json", None),
            )
            return json.dumps({"status": "success", "http_status": result["status"], "body": result["body"]}, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False)

    @tool
    def load_skill(section: str = "") -> str:
        """查阅平台能力手册（SKILL.md）：工具规范、业务域地图、典型工作流。

        接触陌生业务域任务、或忘记调用规范时使用。

        Args:
            section: 章节名关键字（如"工作流"、"调用规范"）；空串返回全文
        """
        from .platform_api_registry import load_skill_doc

        return load_skill_doc(section)

    @tool
    def query_failed_web_tests(limit: int = 5) -> str:
        """查询当前用户最近的失败 Web UI 测试脚本。用户询问失败测试/测试结果时使用。"""
        from sqlalchemy import select
        from ...models.web_test_script import WebTestScript

        rows = db.session.scalars(
            select(WebTestScript)
            .filter_by(status="failed", user_id=user_id)
            .order_by(WebTestScript.updated_at.desc())
            .limit(max(1, min(int(limit), 20)))
        ).all()
        if not rows:
            return json.dumps({"status": "success", "data": [], "message": "最近没有失败的 Web 测试"}, ensure_ascii=False)
        return json.dumps(
            {"status": "success", "data": [{"id": r.id, "name": r.name, "time": str(r.updated_at)} for r in rows]},
            ensure_ascii=False,
        )

    @tool
    def query_recent_api_cases(limit: int = 5) -> str:
        """查询当前用户最近的 API 测试用例（只读）。用户询问已有用例、想基于用例继续工作时使用。"""
        from sqlalchemy import select
        from ...models.api_test_case import ApiTestCase

        rows = db.session.scalars(
            select(ApiTestCase)
            .filter_by(user_id=user_id)
            .order_by(ApiTestCase.id.desc())
            .limit(max(1, min(int(limit), 20)))
        ).all()
        return json.dumps(
            {
                "status": "success",
                "data": [{"id": r.id, "name": r.name, "method": r.method, "url": r.url} for r in rows],
            },
            ensure_ascii=False,
        )

    @tool
    def list_performance_scenarios(limit: int = 5) -> str:
        """列出当前用户已有的性能测试场景（只读），含状态与并发/时长配置。"""
        from sqlalchemy import select
        from ...models.perf_test_scenario import PerfTestScenario

        rows = db.session.scalars(
            select(PerfTestScenario)
            .filter_by(user_id=user_id)
            .order_by(PerfTestScenario.id.desc())
            .limit(max(1, min(int(limit), 20)))
        ).all()
        return json.dumps(
            {
                "status": "success",
                "data": [
                    {"id": r.id, "name": r.name, "target_url": r.target_url, "status": r.status,
                     "vus": r.user_count, "duration": r.duration}
                    for r in rows
                ],
            },
            ensure_ascii=False,
        )

    return [
        search_platform_apis,
        call_platform_api,
        load_skill,
        fetch_api_docs,
        probe_api_endpoint,
        create_performance_test,
        generate_performance_script,
        query_failed_web_tests,
        query_recent_api_cases,
        list_performance_scenarios,
    ]


def _to_langchain_messages(messages: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """前端历史消息 → langgraph 消息（assistant 角色名映射为 ai）"""
    out = []
    for msg in messages or []:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if not content:
            continue
        if role == "assistant":
            role = "ai"
        elif role not in ("user", "system"):
            role = "user"
        out.append({"role": role, "content": content})
    return out


def _extract_steps(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从 langgraph 终态提取可展示的执行轨迹（工具调用 + 工具结果）"""
    steps: List[Dict[str, Any]] = []
    for msg in result.get("messages", []):
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            for tc in tool_calls:
                steps.append(
                    {
                        "type": "tool",
                        "name": tc.get("name", "unknown"),
                        "args": tc.get("args", {}),
                        "result": None,
                    }
                )
        if type(msg).__name__ == "ToolMessage":
            content = msg.content
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, default=str)
            if steps and steps[-1]["type"] == "tool" and steps[-1]["result"] is None:
                steps[-1]["result"] = content[:2000]
    return steps


def _record_log(
    *,
    user_id: Optional[int],
    prompt: str,
    response: Optional[str],
    success: bool,
    latency_ms: int,
    model_name: str,
    total_tokens: int = 0,
    error_message: Optional[str] = None,
    error_type: Optional[str] = None,
    extra_metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """写入 AIInvocationLog（失败不阻断主流程）"""
    try:
        log = AIInvocationLog(
            user_id=user_id,
            feature="agent_chat",
            prompt_version_id=None,
            prompt=prompt[:10000],
            model_name=model_name,
            temperature=0.3,
            response=response[:5000] if response else None,
            success=success,
            error_message=error_message[:2000] if error_message else None,
            error_type=error_type,
            latency_ms=latency_ms,
            total_tokens=total_tokens,
            metadata_json=extra_metadata,
        )
        db.session.add(log)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.error("Failed to record agent invocation log", error=str(exc))


def run_agent_chat(
    messages: List[Dict[str, str]],
    user_id: int,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """
    执行 deepagents 智能体对话。无降级：任何失败写日志后抛异常。

    Returns:
        {"reply": 最终回复, "steps": [{type,name,args,result}], "todos": 计划列表,
         "model": 模型名}
    """
    cfg = _resolve_config(config)
    prompt_text = "\n".join(f"[{m.get('role')}] {m.get('content', '')}" for m in messages or [])[:10000]

    if not cfg["api_key"]:
        error_msg = "AI_ASSISTANT_API_KEY is not configured"
        _record_log(
            user_id=user_id, prompt=prompt_text, response=None, success=False,
            latency_ms=0, model_name=cfg["model"],
            error_message=error_msg, error_type="auth_error",
        )
        raise RuntimeError(error_msg)

    try:
        from deepagents import create_deep_agent
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        _record_log(
            user_id=user_id, prompt=prompt_text, response=None, success=False,
            latency_ms=0, model_name=cfg["model"],
            error_message=str(exc), error_type="dependency_missing",
        )
        raise RuntimeError(f"智能体依赖缺失：pip install deepagents langchain-openai（{exc}）") from exc

    llm = ChatOpenAI(
        model=cfg["model"],
        api_key=cfg["api_key"],
        base_url=cfg["base_url"],
        temperature=0.3,
        timeout=cfg["timeout"],
    )
    agent = create_deep_agent(model=llm, tools=_build_tools(user_id, config), system_prompt=AGENT_SYSTEM_PROMPT)

    start_time = time.monotonic()
    try:
        result = agent.invoke(
            {"messages": _to_langchain_messages(messages)},
            config={"recursion_limit": 40},
        )
    except Exception as exc:
        latency_ms = int((time.monotonic() - start_time) * 1000)
        logger.error("Agent invocation failed", error=str(exc))
        _record_log(
            user_id=user_id, prompt=prompt_text, response=None, success=False,
            latency_ms=latency_ms, model_name=cfg["model"],
            error_message=str(exc), error_type="agent_error",
        )
        raise

    latency_ms = int((time.monotonic() - start_time) * 1000)
    result_messages = result.get("messages", [])
    reply = ""
    if result_messages:
        reply = getattr(result_messages[-1], "content", "") or ""
        if not isinstance(reply, str):
            reply = json.dumps(reply, ensure_ascii=False, default=str)

    usage = getattr(result_messages[-1] if result_messages else None, "usage_metadata", None) or {}
    todos = [
        {"content": t.get("content") or t.get("task", ""), "status": t.get("status", "pending")}
        for t in (result.get("todos") or [])
        if isinstance(t, dict)
    ]
    steps = _extract_steps(result)

    _record_log(
        user_id=user_id, prompt=prompt_text, response=reply, success=True,
        latency_ms=latency_ms, model_name=cfg["model"],
        total_tokens=int(usage.get("total_tokens", 0) or 0),
        extra_metadata={"steps": len(steps)},
    )

    return {
        "reply": reply,
        "steps": steps,
        "todos": todos,
        "model": cfg["model"],
    }
