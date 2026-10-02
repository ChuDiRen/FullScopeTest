"""
全局 AI Copilot —— 底层已替换为 deepagents 智能体引擎（langchain-ai/deepagents / langgraph）

对外契约不变：
- process_copilot_chat(messages, user_id, config) → {"role": "assistant", "content": ...}
- 路由 POST /api/v1/copilot/chat、前端 GlobalCopilot 面板零改动

引擎变化（相对旧两段式 Function Calling）：
- 多步智能体循环：模型可连续调用工具、观察结果、再决策，直到完成任务
- 内置 write_todos 任务规划与虚拟文件系统（内存态）
- 平台工具（创建压测/查失败 Web 测试等）定义在 services/ai/agent_service._build_tools，
  全部按 user_id 收敛到当前用户自有数据
- 未配置 key / 依赖缺失时文档化降级（degraded=True），不抛 500
"""

import json
from typing import Dict, Any, List
from ..extensions import db
from ..core.logging import get_logger

logger = get_logger(__name__)


# ==================== 自然语言创建测试用例 ====================

NL_TEST_SYSTEM_PROMPT = """你是一个 API 测试用例生成专家。根据用户的自然语言描述，生成完整的 API 测试用例。

返回格式（严格 JSON）：
{
  "name": "用例名称",
  "method": "HTTP 方法（GET/POST/PUT/DELETE/PATCH）",
  "url": "请求 URL",
  "headers": {"Content-Type": "application/json"},
  "body": {},
  "body_type": "json",
  "assertions": [
    {"type": "status_code", "expected": 200},
    {"type": "json_path", "path": "$.data.token", "condition": "exists"}
  ],
  "description": "用例描述"
}

规则：
- URL 使用相对路径（如 /api/v1/auth/login）
- 断言至少包含状态码断言
- Headers 根据方法自动推断
- 如果用户描述了多个场景，返回用例数组"""


def create_test_from_nl(
    user_input: str,
    user_id: int,
    project_id: int,
    collection_id: int = None,
    config: Dict[str, Any] = None,
) -> Dict[str, Any]:
    """
    通过自然语言描述创建测试用例

    Args:
        user_input: 用户的自然语言描述
        user_id: 用户 ID
        project_id: 项目 ID
        collection_id: 用例集 ID（可选）
        config: AI 配置

    Returns:
        Dict: 创建结果 {cases: [...], message: "..."}
    """
    from ..services.ai.base import AIServiceBase

    svc = AIServiceBase(config=config)
    messages = [{"role": "user", "content": user_input}]

    response = svc.simple_chat(
        messages=messages,
        feature="nl_test_creation",
        user_id=user_id,
        system_prompt=NL_TEST_SYSTEM_PROMPT,
        temperature=0.2,
    )

    content = svc.get_content(response)
    cases_data = _parse_nl_response(content)

    # 保存到数据库
    created_cases = []
    for case_data in cases_data:
        try:
            from ..models.api_test_case import ApiTestCase
            case = ApiTestCase(
                name=case_data.get("name", "NL 生成用例"),
                method=case_data.get("method", "GET"),
                url=case_data.get("url", "/"),
                headers=case_data.get("headers"),
                body=case_data.get("body"),
                body_type=case_data.get("body_type", "json"),
                assertions=case_data.get("assertions"),
                description=case_data.get("description", "AI 生成"),
                project_id=project_id,
                collection_id=collection_id,
                user_id=user_id,
            )
            db.session.add(case)
            db.session.flush()
            created_cases.append(case.to_dict())
        except Exception as exc:
            logger.warning("NL 用例创建失败", error=str(exc))

    db.session.commit()

    return {
        "cases": created_cases,
        "message": f"已根据描述创建 {len(created_cases)} 个测试用例",
    }


def _parse_nl_response(content: str) -> List[Dict[str, Any]]:
    """解析 AI 返回的用例 JSON"""
    try:
        if "```json" in content:
            json_str = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            json_str = content.split("```")[1].split("```")[0].strip()
        else:
            json_str = content.strip()
        result = json.loads(json_str)
        if isinstance(result, list):
            return result
        return [result]
    except (json.JSONDecodeError, IndexError):
        logger.warning("NL 响应解析失败", content_preview=content[:200])
        return []


def process_copilot_chat(
    messages: List[Dict[str, str]],
    user_id: int,
    config: Dict[str, Any]
) -> Dict[str, Any]:
    """
    处理全局 Copilot 的对话逻辑 —— 底层委托 deepagents 智能体引擎。

    返回契约与旧实现一致（{"role": "assistant", "content": ...}），
    额外附带 steps/todos/degraded 字段，前端当前只读 content，向后兼容。
    """
    from ..services.ai.agent_service import run_agent_chat

    result = run_agent_chat(messages, user_id, config)
    return {
        "role": "assistant",
        "content": result["reply"],
        "steps": result.get("steps", []),
        "todos": result.get("todos", []),
    }
