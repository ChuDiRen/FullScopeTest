"""
v1 Web 自动化测试路由平迁测试（/api/v1/web-test/*，源：app/api/web_test.py）

覆盖：
1. 全部受保护端点未登录 401
2. 创建脚本 → 列表 → 详情 → 更新 → 删除、用例集 CRUD 全链路 200（响应信封与 v1 一致）
3. 访问/更新/删除/执行 他人脚本与用例集、他人视觉快照 → 404（IDOR 属主过滤验证）
4. 执行类端点（脚本运行/用例集批量运行）Celery 任务全 mock，返回 task_id；
   AI 生成/诊断/探索、Playwright codegen 录制全部 mock，零真实外呼、零浏览器启动
5. 非法 URL（内网地址）被 SSRF 校验拒绝（explore / explore/stream / record/start /
   live view allocator 均在动作执行前拦截）
"""

import gc
import os
import shutil
import types
import uuid

import pytest

import app.api.routes.web_test as wt
from app.extensions import db
from sqlalchemy import update

BASE = "/api/v1/web-test"


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    """测试环境无 Redis：把 token 黑名单/限流服务的 _get_redis 打桩为 None。

    token_blacklist 与 rate_limit_service 对 Redis 不可用均有文档化的降级路径，
    打桩后签发/校验 JWT 全程零 socket，避免本机 6379（需认证/挂死）长阻塞。
    """
    import app.services.rate_limit_service as rate_limit_service
    import app.services.token_blacklist as token_blacklist

    monkeypatch.setattr(token_blacklist, "_get_redis", lambda: None)
    monkeypatch.setattr(rate_limit_service, "_get_redis", lambda: None, raising=False)
    yield


@pytest.fixture(autouse=True)
def _mem_session_store(monkeypatch):
    """录制/live view 会话元数据一律走内存 SessionStore（隔离 Redis，零 socket）"""
    from app.services.session_store import MemorySessionStore

    store = MemorySessionStore()
    monkeypatch.setattr(wt, "get_session_store", lambda: store)
    # 录制进程对象按用例隔离（路由只读模块级 dict）
    monkeypatch.setattr(wt, "_recording_process_objects", {})
    yield
    store.shutdown()


class _FakeCeleryTask:
    """run_web_test_task 的测试替身：记录 apply_async 调用，绝不触达 broker"""

    def __init__(self):
        self.calls = []

    def apply_async(self, args=None, task_id=None, **kwargs):
        self.calls.append({"args": args, "task_id": task_id})
        return types.SimpleNamespace(id=task_id or "fake-task-id")


class _FakeProcess:
    """subprocess.Popen 的测试替身（Playwright codegen 永不真正启动）"""

    def __init__(self, pid=4321, exit_code=None):
        self.pid = pid
        self._exit_code = exit_code
        self.terminated = False

    def poll(self):
        return self._exit_code

    @property
    def returncode(self):
        return self._exit_code

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0


def _username() -> str:
    return f"webtest_{uuid.uuid4().hex[:10]}"


def _no_browser_popen(monkeypatch):
    """录制路由的 Popen 替身：若被调用返回 FakeProcess；同时旁路 time.sleep"""

    def fake_popen(cmd, **kwargs):
        assert "codegen" in cmd, f"只允许 playwright codegen 命令: {cmd}"
        return _FakeProcess(pid=4321)

    monkeypatch.setattr(
        wt,
        "subprocess",
        types.SimpleNamespace(Popen=fake_popen, CREATE_NEW_CONSOLE=0x10, DEVNULL=-3),
    )
    import time as _time

    monkeypatch.setattr(wt, "time", types.SimpleNamespace(sleep=lambda s: None, time=_time.time))


# ---------------------------------------------------------------------------
# 数据工厂
# ---------------------------------------------------------------------------

def _create_collection(v2_client, headers, name="Web 用例集", **extra) -> int:
    payload = {"name": name, **extra}
    resp = v2_client.post(f"{BASE}/collections", json=payload, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["id"]


def _create_script(v2_client, headers, name=None, **extra) -> int:
    payload = {"name": name or f"脚本_{uuid.uuid4().hex[:6]}", **extra}
    resp = v2_client.post(f"{BASE}/scripts", json=payload, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["id"]


# ---------------------------------------------------------------------------
# 1. 公开端点 + 未登录 401
# ---------------------------------------------------------------------------

class TestWebTestAuth:
    @pytest.mark.parametrize(
        "method,url",
        [
            ("post", f"{BASE}/ai/generate"),
            ("post", f"{BASE}/ai/analyze-error"),
            ("post", f"{BASE}/ai/explore"),
            ("post", f"{BASE}/ai/explore/stream"),
            ("get", f"{BASE}/collections"),
            ("post", f"{BASE}/collections"),
            ("put", f"{BASE}/collections/1"),
            ("delete", f"{BASE}/collections/1"),
            ("post", f"{BASE}/collections/1/run"),
            ("get", f"{BASE}/scripts"),
            ("post", f"{BASE}/scripts"),
            ("get", f"{BASE}/scripts/1"),
            ("put", f"{BASE}/scripts/1"),
            ("delete", f"{BASE}/scripts/1"),
            ("get", f"{BASE}/scripts/1/snapshots/baseline/home.png"),
            ("post", f"{BASE}/scripts/1/run"),
            ("post", f"{BASE}/record/start"),
            ("post", f"{BASE}/record/stop"),
            ("get", f"{BASE}/record/status"),
        ],
    )
    def test_protected_endpoints_require_auth(self, v2_client, method, url):
        """v1 带 @jwt_required 的端点全部未登录 401"""
        kwargs = {} if method in ("get", "delete") else {"json": {}}
        resp = getattr(v2_client, method)(url, **kwargs)
        assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# 2. 脚本 / 用例集 CRUD 链路
# ---------------------------------------------------------------------------

class TestScriptLifecycle:
    def test_create_list_detail_update_delete(self, v2_client, make_user, auth_headers):
        user_id = make_user(_username())
        headers = auth_headers(user_id)

        # 创建脚本（默认 Playwright 模板）
        resp = v2_client.post(
            f"{BASE}/scripts",
            json={"name": "首页冒烟", "description": "smoke", "target_url": "https://example.com"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == 200
        assert body["message"] == "创建成功"
        script = body["data"]
        assert script["name"] == "首页冒烟"
        assert script["user_id"] == user_id
        assert script["browser"] == "chromium"
        assert "sync_playwright" in script["script_content"]
        assert script["status"] == "pending"

        # 列表
        resp = v2_client.get(f"{BASE}/scripts", headers=headers)
        assert resp.status_code == 200, resp.text
        items = resp.json()["data"]
        assert [s["id"] for s in items] == [script["id"]]

        # 详情
        resp = v2_client.get(f"{BASE}/scripts/{script['id']}", headers=headers)
        assert resp.status_code == 200, resp.text
        detail = resp.json()["data"]
        assert detail["id"] == script["id"]
        assert detail["target_url"] == "https://example.com"

        # 更新
        resp = v2_client.put(
            f"{BASE}/scripts/{script['id']}",
            json={"name": "首页冒烟-v2"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["name"] == "首页冒烟-v2"

        # 删除后详情 404
        resp = v2_client.delete(f"{BASE}/scripts/{script['id']}", headers=headers)
        assert resp.status_code == 200, resp.text
        resp = v2_client.get(f"{BASE}/scripts/{script['id']}", headers=headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == 404

    def test_create_missing_name_400(self, v2_client, make_user, auth_headers):
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        resp = v2_client.post(f"{BASE}/scripts", json={}, headers=headers)
        assert resp.status_code == 400
        assert resp.json()["code"] == 400

    def test_list_filter_by_collection(self, v2_client, make_user, auth_headers):
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        col_id = _create_collection(v2_client, headers)
        sid_in = _create_script(v2_client, headers, collection_id=col_id)
        _create_script(v2_client, headers)

        resp = v2_client.get(f"{BASE}/scripts", params={"collection_id": col_id}, headers=headers)
        assert resp.status_code == 200, resp.text
        items = resp.json()["data"]
        assert [s["id"] for s in items] == [sid_in]
        assert items[0]["collection_id"] == col_id


class TestCollectionLifecycle:
    def test_create_update_delete(self, v2_client, make_user, auth_headers):
        user_id = make_user(_username())
        headers = auth_headers(user_id)

        resp = v2_client.post(
            f"{BASE}/collections",
            json={"name": "回归集合", "description": "d1"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        col = resp.json()["data"]
        assert col["name"] == "回归集合"
        assert col["user_id"] == user_id

        resp = v2_client.get(f"{BASE}/collections", headers=headers)
        assert [c["id"] for c in resp.json()["data"]] == [col["id"]]

        resp = v2_client.put(f"{BASE}/collections/{col['id']}", json={"name": "回归集合-2"}, headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["name"] == "回归集合-2"

        resp = v2_client.delete(f"{BASE}/collections/{col['id']}", headers=headers)
        assert resp.status_code == 200, resp.text
        resp = v2_client.get(f"{BASE}/collections", headers=headers)
        assert resp.json()["data"] == []

    def test_create_missing_name_400(self, v2_client, make_user, auth_headers):
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        resp = v2_client.post(f"{BASE}/collections", json={"description": "no name"}, headers=headers)
        assert resp.status_code == 400
        assert resp.json()["message"] == "name is required"


# ---------------------------------------------------------------------------
# 3. IDOR：他人脚本 / 用例集 / 执行结果 404
# ---------------------------------------------------------------------------

class TestIdor:
    def test_other_users_script_404(self, v2_client, make_user, auth_headers):
        owner = make_user(_username())
        other = make_user(_username())
        owner_headers = auth_headers(owner)
        other_headers = auth_headers(other)
        sid = _create_script(v2_client, owner_headers)

        resp = v2_client.get(f"{BASE}/scripts/{sid}", headers=other_headers)
        assert resp.status_code == 404
        assert resp.json()["code"] == 404

        resp = v2_client.put(f"{BASE}/scripts/{sid}", json={"name": "hijack"}, headers=other_headers)
        assert resp.status_code == 404

        resp = v2_client.delete(f"{BASE}/scripts/{sid}", headers=other_headers)
        assert resp.status_code == 404

        # 原属主不受影响
        resp = v2_client.get(f"{BASE}/scripts/{sid}", headers=owner_headers)
        assert resp.status_code == 200
        assert resp.json()["data"]["name"] != "hijack"

    def test_other_users_script_run_404(self, v2_client, make_user, auth_headers, monkeypatch):
        fake_task = _FakeCeleryTask()
        monkeypatch.setattr(wt, "run_web_test_task", fake_task)

        owner = make_user(_username())
        other = make_user(_username())
        sid = _create_script(v2_client, auth_headers(owner))

        resp = v2_client.post(f"{BASE}/scripts/{sid}/run", headers=auth_headers(other))
        assert resp.status_code == 404
        # 越权请求绝不派发 Celery 任务
        assert fake_task.calls == []

    def test_other_users_snapshots_404(self, v2_client, make_user, auth_headers):
        """视觉基准/actual/diff 图片按脚本属主过滤（他人执行结果 404）"""
        owner = make_user(_username())
        other = make_user(_username())
        sid = _create_script(v2_client, auth_headers(owner))

        resp = v2_client.get(
            f"{BASE}/scripts/{sid}/snapshots/baseline/home.png", headers=auth_headers(other)
        )
        assert resp.status_code == 404
        assert resp.json()["message"] == "脚本不存在"

    def test_other_users_script_in_analyze_error_404(self, v2_client, make_user, auth_headers):
        owner = make_user(_username())
        sid = _create_script(v2_client, auth_headers(owner))

        resp = v2_client.post(
            f"{BASE}/ai/analyze-error",
            json={"script_id": sid, "error_log": "TimeoutError"},
            headers=auth_headers(make_user(_username())),
        )
        assert resp.status_code == 404
        assert resp.json()["message"] == "脚本不存在"

    def test_other_users_collection_404(self, v2_client, make_user, auth_headers, monkeypatch):
        monkeypatch.setattr(wt, "run_web_test_task", _FakeCeleryTask())

        owner = make_user(_username())
        other = make_user(_username())
        owner_headers = auth_headers(owner)
        other_headers = auth_headers(other)
        col_id = _create_collection(v2_client, owner_headers)

        resp = v2_client.put(f"{BASE}/collections/{col_id}", json={"name": "x"}, headers=other_headers)
        assert resp.status_code == 404

        resp = v2_client.delete(f"{BASE}/collections/{col_id}", headers=other_headers)
        assert resp.status_code == 404

        resp = v2_client.post(f"{BASE}/collections/{col_id}/run", headers=other_headers)
        assert resp.status_code == 404
        assert resp.json()["message"] == "用例集不存在"

        # 他人集合不能挂自己的脚本（创建脚本时集合属主过滤）
        resp = v2_client.post(
            f"{BASE}/scripts", json={"name": "越权挂载", "collection_id": col_id}, headers=other_headers
        )
        assert resp.status_code == 404
        assert resp.json()["message"] == "用例集不存在"


# ---------------------------------------------------------------------------
# 4. 执行类端点：Celery 全 mock 返回 task_id
# ---------------------------------------------------------------------------

class TestRunScript:
    def test_run_script_returns_task_id(self, v2_client, make_user, auth_headers, monkeypatch):
        fake_task = _FakeCeleryTask()
        monkeypatch.setattr(wt, "run_web_test_task", fake_task)

        user_id = make_user(_username())
        headers = auth_headers(user_id)
        sid = _create_script(v2_client, headers)

        resp = v2_client.post(f"{BASE}/scripts/{sid}/run", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == 200
        assert body["data"]["task_id"] == f"web_test_{sid}_{user_id}"
        assert body["data"]["script_id"] == sid
        assert body["data"]["message"] == "测试已提交，正在后台执行"

        # Celery 派发参数与 v1 一致
        assert fake_task.calls == [{"args": [sid, user_id], "task_id": f"web_test_{sid}_{user_id}"}]

        # 提交后脚本状态立即为 running
        resp = v2_client.get(f"{BASE}/scripts/{sid}", headers=headers)
        assert resp.json()["data"]["status"] == "running"
        assert resp.json()["data"]["last_status"] == "running"

        # 运行中重复提交 → 400
        resp = v2_client.post(f"{BASE}/scripts/{sid}/run", headers=headers)
        assert resp.status_code == 400
        assert resp.json()["message"] == "脚本正在运行中"

    def test_run_collection_submits_enabled_scripts(self, v2_client, app, make_user, auth_headers, monkeypatch):
        fake_task = _FakeCeleryTask()
        monkeypatch.setattr(wt, "run_web_test_task", fake_task)

        user_id = make_user(_username())
        headers = auth_headers(user_id)
        col_id = _create_collection(v2_client, headers)
        sid1 = _create_script(v2_client, headers, collection_id=col_id)
        sid2 = _create_script(v2_client, headers, collection_id=col_id)
        _create_script(v2_client, headers)  # 不属于该集合

        # 把 sid2 置为禁用（不应被批量提交）
        from app.models.web_test_script import WebTestScript
        disabled = db.session.get(WebTestScript, sid2)
        disabled.is_enabled = False
        db.session.commit()
        resp = v2_client.post(f"{BASE}/collections/{col_id}/run", headers=headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["collection_id"] == col_id
        assert data["submitted_count"] == 1
        assert data["submitted"] == [
            {"script_id": sid1, "task_id": f"web_test_{sid1}_{user_id}"}
        ]
        assert data["skipped"] == []
        assert len(fake_task.calls) == 1

        # 空集合（无启用脚本）→ 400
        col2 = _create_collection(v2_client, headers)
        resp = v2_client.post(f"{BASE}/collections/{col2}/run", headers=headers)
        assert resp.status_code == 400
        assert resp.json()["message"] == "用例集内没有可执行脚本"


# ---------------------------------------------------------------------------
# 5. SSRF：内网地址被拒绝（动作执行前拦截）
# ---------------------------------------------------------------------------

class TestSSRF:
    INTERNAL_URLS = [
        "http://127.0.0.1:8080/",
        "http://192.168.1.10/admin",
        "http://10.0.0.5/",
        "http://169.254.169.254/latest/meta-data/",
    ]

    @pytest.mark.parametrize("url", INTERNAL_URLS)
    def test_explore_blocks_internal_url(self, v2_client, make_user, auth_headers, monkeypatch, url):
        def _boom(*args, **kwargs):
            raise AssertionError("SSRF 校验失败后不应执行探索任务")

        monkeypatch.setattr(wt, "run_exploration_task", _boom)

        headers = auth_headers(make_user(_username()))
        resp = v2_client.post(f"{BASE}/ai/explore", json={"start_url": url}, headers=headers)
        assert resp.status_code == 400
        assert resp.json()["code"] == 400

    @pytest.mark.parametrize("url", INTERNAL_URLS)
    def test_explore_stream_blocks_internal_url(self, v2_client, make_user, auth_headers, monkeypatch, url):
        def _boom(*args, **kwargs):
            raise AssertionError("SSRF 校验失败后不应执行探索任务")

        monkeypatch.setattr(wt, "run_exploration_task", _boom)

        headers = auth_headers(make_user(_username()))
        resp = v2_client.post(f"{BASE}/ai/explore/stream", json={"start_url": url}, headers=headers)
        assert resp.status_code == 400

    @pytest.mark.parametrize("url", INTERNAL_URLS)
    def test_record_start_blocks_internal_url(self, v2_client, make_user, auth_headers, monkeypatch, url):
        """录制路由：SSRF 校验必须发生在 Popen 启动 codegen 之前"""
        _no_browser_popen(monkeypatch)

        def _boom(*args, **kwargs):
            raise AssertionError("SSRF 校验失败后不应启动 codegen 进程")

        headers = auth_headers(make_user(_username()))
        resp = v2_client.post(f"{BASE}/record/start", json={"url": url}, headers=headers)
        assert resp.status_code == 400
        assert resp.json()["code"] == 400

    def test_allocator_unsafe_url_never_called(self, v2_client, make_user, auth_headers, monkeypatch):
        """live view allocator：不安全 allocator 地址永不发起 HTTP 请求（走模板回退）"""

        def _boom(*args, **kwargs):
            raise AssertionError("不安全 allocator 地址不应被请求")

        monkeypatch.setattr("requests.post", _boom)
        monkeypatch.setattr("requests.delete", _boom)

        user_id = make_user(_username())
        session = wt._allocate_live_view_session(
            {"live_view_allocator_url": "http://127.0.0.1:6379/alloc"},
            "https://example.com",
            " objective",
            10,
            user_id,
        )
        assert session == {}

    def test_start_url_missing_400(self, v2_client, make_user, auth_headers):
        headers = auth_headers(make_user(_username()))
        resp = v2_client.post(f"{BASE}/ai/explore", json={}, headers=headers)
        assert resp.status_code == 400
        assert resp.json()["message"] == "start_url is required"


# ---------------------------------------------------------------------------
# 6. AI 端点（全 mock，零外呼）
# ---------------------------------------------------------------------------

class TestAIEndpoints:
    def test_generate_missing_prompt_400(self, v2_client, make_user, auth_headers):
        headers = auth_headers(make_user(_username()))
        resp = v2_client.post(f"{BASE}/ai/generate", json={"prompt": "  "}, headers=headers)
        assert resp.status_code == 400
        assert resp.json()["message"] == "prompt is required"

    def test_generate_success(self, v2_client, make_user, auth_headers, monkeypatch):
        calls = {}

        def fake_generate(prompt, test_type, runtime_config, user_id=None):
            calls.update(prompt=prompt, test_type=test_type, user_id=user_id)
            return "def run(): pass"

        monkeypatch.setattr(wt, "generate_test_script", fake_generate)

        user_id = make_user(_username())
        headers = auth_headers(user_id)
        resp = v2_client.post(f"{BASE}/ai/generate", json={"prompt": "登录页冒烟"}, headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["message"] == "AI 脚本生成成功"
        assert body["data"]["script_content"] == "def run(): pass"
        assert calls == {"prompt": "登录页冒烟", "test_type": "web", "user_id": user_id}

    def test_analyze_error_missing_params_400(self, v2_client, make_user, auth_headers):
        headers = auth_headers(make_user(_username()))
        resp = v2_client.post(f"{BASE}/ai/analyze-error", json={"script_id": 1}, headers=headers)
        assert resp.status_code == 400
        assert resp.json()["message"] == "script_id and error_log are required"

    def test_analyze_error_success(self, v2_client, make_user, auth_headers, monkeypatch):
        captured = {}

        def fake_analyze(script_content, error_log, test_type, config=None):
            captured.update(script_len=len(script_content), error_log=error_log, test_type=test_type)
            return {"root_cause": "selector 失效", "suggestions": ["更新 selector"]}

        monkeypatch.setattr(wt, "analyze_test_error", fake_analyze)

        user_id = make_user(_username())
        headers = auth_headers(user_id)
        sid = _create_script(v2_client, headers)

        resp = v2_client.post(
            f"{BASE}/ai/analyze-error",
            json={"script_id": sid, "error_log": "Timeout 30000ms exceeded"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["message"] == "AI 诊断完成"
        assert body["data"]["root_cause"] == "selector 失效"
        assert captured["test_type"] == "web"
        assert captured["error_log"] == "Timeout 30000ms exceeded"


# ---------------------------------------------------------------------------
# 7. AI 探索（非流式 + SSE 流式，探索任务全 mock）
# ---------------------------------------------------------------------------

def _fake_explorer(log_lines, report=None, exc=None):
    def _run(start_url=None, max_steps=None, objective=None, config=None,
             log_callback=None, progress_callback=None, *args, **kwargs):
        for line in log_lines:
            if log_callback:
                log_callback(line)
        if exc is not None:
            raise exc
        return report if report is not None else {"pages_visited": len(log_lines), "start_url": start_url}

    return _run


class TestExplore:
    def test_explore_success(self, v2_client, make_user, auth_headers, monkeypatch):
        monkeypatch.setattr(
            wt, "run_exploration_task", _fake_explorer(["打开 https://example.com"])
        )

        headers = auth_headers(make_user(_username()))
        resp = v2_client.post(
            f"{BASE}/ai/explore",
            json={"start_url": "https://example.com", "max_steps": 3},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["message"] == "AI 探索测试完成"
        assert body["data"]["start_url"] == "https://example.com"

    def test_explore_error_500(self, v2_client, make_user, auth_headers, monkeypatch):
        monkeypatch.setattr(
            wt,
            "run_exploration_task",
            _fake_explorer([], exc=RuntimeError("browser crashed")),
        )

        headers = auth_headers(make_user(_username()))
        resp = v2_client.post(f"{BASE}/ai/explore", json={"start_url": "https://example.com"}, headers=headers)
        assert resp.status_code == 500
        assert "AI 探索测试失败" in resp.json()["message"]

    def test_explore_stream_events(self, v2_client, make_user, auth_headers, monkeypatch):
        monkeypatch.setattr(
            wt, "run_exploration_task", _fake_explorer(["step-1", "step-2"], report={"pages_visited": 2})
        )

        headers = auth_headers(make_user(_username()))
        resp = v2_client.post(
            f"{BASE}/ai/explore/stream",
            json={"start_url": "https://example.com", "live_view_url": "http://live.example.test/v/1"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("text/event-stream")

        text = resp.text
        # live_view（手动直连）→ log → report → done
        assert "event: live_view" in text
        assert "http://live.example.test/v/1" in text
        assert '"source": "manual"' in text
        assert "event: log" in text
        assert "step-1" in text
        assert "event: report" in text
        assert '"pages_visited": 2' in text
        assert "event: done" in text
        assert '"ok": true' in text

    def test_explore_stream_worker_error(self, v2_client, make_user, auth_headers, monkeypatch):
        monkeypatch.setattr(
            wt,
            "run_exploration_task",
            _fake_explorer([], exc=RuntimeError("headless failed")),
        )

        headers = auth_headers(make_user(_username()))
        resp = v2_client.post(
            f"{BASE}/ai/explore/stream",
            json={"start_url": "https://example.com"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        text = resp.text
        assert "event: error" in text
        assert "headless failed" in text
        assert "event: done" in text
        assert '"ok": false' in text


# ---------------------------------------------------------------------------
# 8. Playwright 录制（Popen 全 mock，零浏览器启动）
# ---------------------------------------------------------------------------

class TestRecord:
    def test_start_status_stop(self, v2_client, make_user, auth_headers, monkeypatch):
        _no_browser_popen(monkeypatch)

        user_id = make_user(_username())
        headers = auth_headers(user_id)

        # 启动
        resp = v2_client.post(
            f"{BASE}/record/start",
            json={"url": "https://example.com", "browser": "firefox"},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["pid"] == 4321
        assert data["browser"] == "firefox"
        assert data["url"] == "https://example.com"

        # 状态：录制中
        resp = v2_client.get(f"{BASE}/record/status", headers=headers)
        assert resp.status_code == 200, resp.text
        status = resp.json()["data"]
        assert status["is_recording"] is True
        assert status["pid"] == 4321
        assert "python_path" in status

        # 停止
        resp = v2_client.post(f"{BASE}/record/stop", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "录制已停止"

        # 停止后状态：无录制
        resp = v2_client.get(f"{BASE}/record/status", headers=headers)
        assert resp.json()["data"]["is_recording"] is False

        # 再次停止 → 400
        resp = v2_client.post(f"{BASE}/record/stop", headers=headers)
        assert resp.status_code == 400
        assert resp.json()["message"] == "没有正在运行的录制进程"

    def test_start_duplicate_400(self, v2_client, make_user, auth_headers, monkeypatch):
        _no_browser_popen(monkeypatch)
        headers = auth_headers(make_user(_username()))

        resp = v2_client.post(f"{BASE}/record/start", json={"url": "https://example.com"}, headers=headers)
        assert resp.status_code == 200

        resp = v2_client.post(f"{BASE}/record/start", json={"url": "https://example.com"}, headers=headers)
        assert resp.status_code == 400
        assert resp.json()["message"] == "已有录制进程在运行，请先停止"

    def test_start_process_exits_immediately_500(self, v2_client, make_user, auth_headers, monkeypatch):
        def fake_popen(cmd, **kwargs):
            return _FakeProcess(pid=99, exit_code=1)  # 启动即退出

        monkeypatch.setattr(
            wt, "subprocess", types.SimpleNamespace(Popen=fake_popen, CREATE_NEW_CONSOLE=0x10, DEVNULL=-3)
        )
        import time as _time

        monkeypatch.setattr(wt, "time", types.SimpleNamespace(sleep=lambda s: None, time=_time.time))

        headers = auth_headers(make_user(_username()))
        resp = v2_client.post(f"{BASE}/record/start", json={"url": "https://example.com"}, headers=headers)
        assert resp.status_code == 500
        assert resp.json()["message"] == "录制器启动失败，进程立即退出，请检查 Playwright 是否正确安装"

        # 失败后无残留状态
        resp = v2_client.get(f"{BASE}/record/status", headers=headers)
        assert resp.json()["data"]["is_recording"] is False

    def test_status_no_recording(self, v2_client, make_user, auth_headers):
        headers = auth_headers(make_user(_username()))
        resp = v2_client.get(f"{BASE}/record/status", headers=headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["is_recording"] is False
        assert "python_path" in data

    def test_record_isolated_per_user(self, v2_client, make_user, auth_headers, monkeypatch):
        """A 的录制会话不能被 B 停止/查看（属主隔离）"""
        _no_browser_popen(monkeypatch)

        user_a = make_user(_username())
        user_b = make_user(_username())
        headers_a = auth_headers(user_a)
        headers_b = auth_headers(user_b)

        resp = v2_client.post(f"{BASE}/record/start", json={"url": "https://example.com"}, headers=headers_a)
        assert resp.status_code == 200

        # B 无录制状态
        resp = v2_client.get(f"{BASE}/record/status", headers=headers_b)
        assert resp.json()["data"]["is_recording"] is False

        # B 停止 → 400（B 名下无录制）
        resp = v2_client.post(f"{BASE}/record/stop", headers=headers_b)
        assert resp.status_code == 400

        # A 仍录制中
        resp = v2_client.get(f"{BASE}/record/status", headers=headers_a)
        assert resp.json()["data"]["is_recording"] is True


# ---------------------------------------------------------------------------
# 9. 视觉快照（视觉回归基准/actual/diff 图片）
# ---------------------------------------------------------------------------

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-png-data-for-test"


class TestSnapshots:
    @pytest.fixture()
    def snapshot_env(self, v2_client, app, make_user, auth_headers):
        """创建脚本 + 落一张 baseline 快照文件，用后清理"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        sid = _create_script(v2_client, headers)

        from app.tasks.common import get_backend_root

        work_dir = os.path.join(
            os.path.dirname(get_backend_root()), "data", "web_tests", str(sid)
        )
        image_dir = os.path.join(work_dir, "snapshots", "baseline")
        os.makedirs(image_dir, exist_ok=True)
        image_path = os.path.join(image_dir, "homepage.png")
        with open(image_path, "wb") as f:
            f.write(PNG_BYTES)

        yield {"sid": sid, "headers": headers, "work_dir": work_dir}

        shutil.rmtree(work_dir, ignore_errors=True)

    def test_snapshot_image_200(self, v2_client, snapshot_env):
        resp = v2_client.get(
            f"{BASE}/scripts/{snapshot_env['sid']}/snapshots/baseline/homepage.png",
            headers=snapshot_env["headers"],
        )
        assert resp.status_code == 200, resp.text
        assert resp.content == PNG_BYTES
        assert resp.headers["content-type"].startswith("image/png")

    def test_snapshot_auto_append_png_suffix(self, v2_client, snapshot_env):
        resp = v2_client.get(
            f"{BASE}/scripts/{snapshot_env['sid']}/snapshots/baseline/homepage",
            headers=snapshot_env["headers"],
        )
        assert resp.status_code == 200, resp.text
        assert resp.content == PNG_BYTES

    def test_snapshot_invalid_image_type_400(self, v2_client, snapshot_env):
        resp = v2_client.get(
            f"{BASE}/scripts/{snapshot_env['sid']}/snapshots/evil/homepage.png",
            headers=snapshot_env["headers"],
        )
        assert resp.status_code == 400
        assert resp.json()["message"] == "无效的图片类型"

    def test_snapshot_missing_404(self, v2_client, snapshot_env):
        resp = v2_client.get(
            f"{BASE}/scripts/{snapshot_env['sid']}/snapshots/actual/missing.png",
            headers=snapshot_env["headers"],
        )
        assert resp.status_code == 404
        assert resp.json()["message"] == "图片不存在"

    def test_snapshot_traversal_404(self, v2_client, snapshot_env):
        resp = v2_client.get(
            f"{BASE}/scripts/{snapshot_env['sid']}/snapshots/baseline/..%2F..%2Fconftest.png",
            headers=snapshot_env["headers"],
        )
        assert resp.status_code == 404
