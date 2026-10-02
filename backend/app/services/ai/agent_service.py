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

from ...extensions import db
from ...models.ai_invocation_log import AIInvocationLog
from ...core.logging import get_logger

logger = get_logger(__name__)

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

AGENT_SYSTEM_PROMPT = """你是"大熊AI测试平台"的智能体（Agent），帮助用户完成测试相关任务。

你可以：
- 创建和管理性能测试场景（Locust 压测）
- 为已创建的压测场景生成定制化 Locust 业务脚本
- 查询最近失败的 Web UI 测试
- 查询用户的 API 测试用例
- 对多步任务先用 write_todos 列出计划，再逐步执行并更新状态

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

    return [create_performance_test, generate_performance_script, query_failed_web_tests, query_recent_api_cases, list_performance_scenarios]


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
