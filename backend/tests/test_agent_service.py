"""
deepagents 智能体服务单测（copilot 底层引擎）

process_copilot_chat 现已委托 run_agent_chat（deepagents/langgraph）。
本文件只测服务层纯逻辑：零真实外呼、零 API key 依赖。
路由层的 mock 由 tests/api_v2/test_v1_ai_modules.py::test_copilot_chat_mock 覆盖
（monkeypatch 路由模块属性 process_copilot_chat，接口契约不变）。
"""
import uuid

import pytest


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    import app.services.rate_limit_service as rate_limit_service
    import app.services.token_blacklist as token_blacklist

    monkeypatch.setattr(token_blacklist, "_get_redis", lambda: None)
    monkeypatch.setattr(rate_limit_service, "_get_redis", lambda: None, raising=False)
    yield


def _uname(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


@pytest.fixture
def make_user(app):
    """tests/ 根目录没有 api_v2 的 make_user，本地造一个（与 test_login_lockout 同风格）"""
    from app.extensions import db
    from app.models.user import User

    def _make(prefix: str) -> int:
        user = User(
            username=_uname(prefix),
            email=f"{_uname(prefix)}@test.com",
            password_hash="h",
        )
        db.session.add(user)
        db.session.commit()
        return user.id

    return _make


def test_run_agent_chat_raises_without_api_key(app, make_user, monkeypatch):
    """无降级：未配置 API key 直接抛 RuntimeError（先写失败日志）"""
    from app.services.ai import agent_service

    uid = make_user(_uname("agent"))
    monkeypatch.delenv("AI_ASSISTANT_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="AI_ASSISTANT_API_KEY"):
        agent_service.run_agent_chat(
            [{"role": "user", "content": "创建压测"}], uid, {"AI_ASSISTANT_API_KEY": ""}
        )


def test_process_copilot_chat_propagates_error(app, make_user, monkeypatch):
    """process_copilot_chat 不吞异常：错误向上传播，路由层转 500"""
    from app.utils.ai_copilot import process_copilot_chat

    uid = make_user(_uname("agent"))
    monkeypatch.delenv("AI_ASSISTANT_API_KEY", raising=False)

    with pytest.raises(RuntimeError, match="AI_ASSISTANT_API_KEY"):
        process_copilot_chat(
            [{"role": "user", "content": "hi"}], uid, {"AI_ASSISTANT_API_KEY": ""}
        )


def test_to_langchain_messages_role_mapping():
    from app.services.ai.agent_service import _to_langchain_messages

    out = _to_langchain_messages(
        [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "system", "content": "c"},
            {"role": "weird", "content": "d"},
            {"role": "assistant", "content": ""},
        ]
    )
    assert [m["role"] for m in out] == ["user", "ai", "system", "user"]
    assert len(out) == 4  # 空 content 被丢弃


def test_extract_steps_pairs_tool_calls_with_results():
    from app.services.ai.agent_service import _extract_steps

    class AIMsg:
        def __init__(self, tool_calls):
            self.tool_calls = tool_calls

    class ToolMessage:
        content = '{"status": "success"}'

    result = {
        "messages": [
            AIMsg([{"id": "t1", "name": "query_failed_web_tests", "args": {"limit": 5}}]),
            ToolMessage(),
            AIMsg([]),
        ]
    }
    steps = _extract_steps(result)
    assert len(steps) == 1
    assert steps[0]["name"] == "query_failed_web_tests"
    assert steps[0]["args"] == {"limit": 5}
    assert steps[0]["result"] == '{"status": "success"}'
