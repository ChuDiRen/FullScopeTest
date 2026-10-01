"""
v1 app_test 平迁路由测试（/api/v1/app-test/*，源：app/api/app_test.py）

覆盖：
1. 公开端点 /api/v1/app-test/health 200；受保护端点未登录一律 401
2. 用例集/脚本 创建 → 列表 → 详情 → 更新 → 删除 全链路，响应信封与 v1 一致
3. 访问/更新/删除/执行他人脚本与用例集 → 404（IDOR：user_id 属主过滤验证）
4. 脚本执行（subprocess mock，Appium/Celery 全 mock，零外呼）、设备列表（requests mock）
"""
from app.core.runtime import get_config

import gc
import subprocess
import sys
import uuid

import pytest

BASE = "/api/v1/app-test"


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    """测试环境无 Redis：把 token 黑名单/限流服务的 _get_redis 打桩为 None。

    token_blacklist 与 rate_limit_service 对 Redis 不可用均有文档化的降级路径
    （版本号取 0、黑名单放行、限流放行），打桩后签发/校验 JWT 全程零 socket，
    避免测试环境对 redis://localhost:6379 的 TCP 连接挂起。
    """
    import app.services.rate_limit_service as rate_limit_service
    import app.services.token_blacklist as token_blacklist

    monkeypatch.setattr(token_blacklist, "_get_redis", lambda: None)
    monkeypatch.setattr(rate_limit_service, "_get_redis", lambda: None, raising=False)
    yield


def _username() -> str:
    return f"apptest_{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------------------
# 数据工厂
# ---------------------------------------------------------------------------

def _create_collection(v2_client, headers, name="APP 用例集", **extra) -> int:
    resp = v2_client.post(
        f"{BASE}/collections",
        json={"name": name, **extra},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["id"]


def _create_script(v2_client, headers, name="冒烟脚本", **extra) -> int:
    payload = {"name": name, **extra}
    resp = v2_client.post(f"{BASE}/scripts", json=payload, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]["id"]


# ---------------------------------------------------------------------------
# 1. 公开端点 + 未登录 401
# ---------------------------------------------------------------------------

class TestAppTestAuth:
    def test_health_public(self, v2_client):
        """健康检查是 v1 公开端点，无鉴权应 200"""
        resp = v2_client.get(f"{BASE}/health")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == 200
        assert body["message"] == "APP 测试模块正常"
        assert body["data"]["status"] == "ok"

    @pytest.mark.parametrize(
        "method,url",
        [
            ("get", f"{BASE}/collections"),
            ("post", f"{BASE}/collections"),
            ("put", f"{BASE}/collections/1"),
            ("delete", f"{BASE}/collections/1"),
            ("get", f"{BASE}/scripts"),
            ("post", f"{BASE}/scripts"),
            ("get", f"{BASE}/scripts/1"),
            ("put", f"{BASE}/scripts/1"),
            ("delete", f"{BASE}/scripts/1"),
            ("post", f"{BASE}/scripts/1/run"),
            ("get", f"{BASE}/devices"),
        ],
    )
    def test_protected_endpoints_require_auth(self, v2_client, method, url):
        """未登录访问受保护端点 → 401"""
        resp = getattr(v2_client, method)(url)
        assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# 2. 用例集 CRUD
# ---------------------------------------------------------------------------

class TestAppCollectionCRUD:
    def test_create_list_detail_flow(self, v2_client, make_user, auth_headers):
        """创建 → 列表 → 更新后列表可见详情字段"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)

        cid = _create_collection(v2_client, headers, name="回归集合", description="描述A")

        # 列表
        resp = v2_client.get(f"{BASE}/collections", headers=headers)
        assert resp.status_code == 200, resp.text
        items = resp.json()["data"]
        assert any(c["id"] == cid and c["name"] == "回归集合" for c in items)

        # 更新
        resp = v2_client.put(
            f"{BASE}/collections/{cid}",
            json={"name": "回归集合V2", "sort_order": 3},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["message"] == "更新成功"
        assert body["data"]["name"] == "回归集合V2"

        # 列表可见更新结果
        resp = v2_client.get(f"{BASE}/collections", headers=headers)
        items = resp.json()["data"]
        target = next(c for c in items if c["id"] == cid)
        assert target["name"] == "回归集合V2"

    def test_create_collection_missing_name_400(self, v2_client, make_user, auth_headers):
        """缺 name 字段 → 400（与 v1 validate_json 消息一致）"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)

        resp = v2_client.post(f"{BASE}/collections", json={"description": "no name"}, headers=headers)
        assert resp.status_code == 400, resp.text
        assert resp.json()["message"] == "缺少必需字段: name"

    def test_delete_collection(self, v2_client, make_user, auth_headers):
        """删除 → 列表不再包含"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        cid = _create_collection(v2_client, headers)

        resp = v2_client.delete(f"{BASE}/collections/{cid}", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "删除成功"

        resp = v2_client.get(f"{BASE}/collections", headers=headers)
        assert all(c["id"] != cid for c in resp.json()["data"])

    def test_others_collection_404(self, v2_client, make_user, auth_headers):
        """他人用例集：更新/删除 → 404（IDOR 属主过滤）"""
        owner = make_user(_username())
        stranger = make_user(_username())
        owner_headers = auth_headers(owner)
        stranger_headers = auth_headers(stranger)

        cid = _create_collection(v2_client, owner_headers)

        resp = v2_client.put(f"{BASE}/collections/{cid}", json={"name": "hijack"}, headers=stranger_headers)
        assert resp.status_code == 404, resp.text
        resp = v2_client.delete(f"{BASE}/collections/{cid}", headers=stranger_headers)
        assert resp.status_code == 404, resp.text

        # 属主不受影响
        resp = v2_client.get(f"{BASE}/collections", headers=owner_headers)
        assert any(c["id"] == cid for c in resp.json()["data"])


# ---------------------------------------------------------------------------
# 3. 脚本 CRUD
# ---------------------------------------------------------------------------

class TestAppScriptCRUD:
    def test_create_list_detail_flow(self, v2_client, make_user, auth_headers):
        """创建脚本 → 列表 → 详情 → 更新 → 删除"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        cid = _create_collection(v2_client, headers)

        sid = _create_script(
            v2_client,
            headers,
            name="Android 冒烟",
            collection_id=cid,
            app_package="com.example.app",
            script_content="print('hello appium')",
        )

        # 列表（含 collection 过滤）
        resp = v2_client.get(f"{BASE}/scripts?collection_id={cid}", headers=headers)
        assert resp.status_code == 200, resp.text
        items = resp.json()["data"]
        assert any(s["id"] == sid and s["name"] == "Android 冒烟" for s in items)

        # 详情
        resp = v2_client.get(f"{BASE}/scripts/{sid}", headers=headers)
        assert resp.status_code == 200, resp.text
        detail = resp.json()["data"]
        assert detail["app_package"] == "com.example.app"
        assert detail["platform"] == "android"  # v1 默认值
        assert detail["appium_server"] == "http://localhost:4723"  # v1 默认值

        # 更新
        resp = v2_client.put(
            f"{BASE}/scripts/{sid}",
            json={"description": "更新后的描述", "is_enabled": False},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["description"] == "更新后的描述"

        # 删除
        resp = v2_client.delete(f"{BASE}/scripts/{sid}", headers=headers)
        assert resp.status_code == 200, resp.text
        resp = v2_client.get(f"{BASE}/scripts/{sid}", headers=headers)
        assert resp.status_code == 404

    def test_create_script_missing_name_400(self, v2_client, make_user, auth_headers):
        """缺 name → 400"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        resp = v2_client.post(f"{BASE}/scripts", json={"platform": "ios"}, headers=headers)
        assert resp.status_code == 400, resp.text

    def test_others_script_404(self, v2_client, make_user, auth_headers):
        """他人脚本：详情/更新/删除/执行 → 404（IDOR 属主过滤）"""
        owner = make_user(_username())
        stranger = make_user(_username())
        owner_headers = auth_headers(owner)
        stranger_headers = auth_headers(stranger)

        sid = _create_script(v2_client, owner_headers)

        for method, kwargs in [
            ("get", {}),
            ("put", {"json": {"name": "hijack"}}),
            ("delete", {}),
            ("post", {}),
        ]:
            url = f"{BASE}/scripts/{sid}" + ("/run" if method == "post" else "")
            resp = getattr(v2_client, method)(url, headers=stranger_headers, **kwargs)
            assert resp.status_code == 404, f"{method} {url}: {resp.text}"


# ---------------------------------------------------------------------------
# 4. 脚本执行（subprocess mock，零外呼）
# ---------------------------------------------------------------------------

class _FakeCompleted:
    returncode = 0
    stdout = "hello from fake appium script"
    stderr = ""


class TestAppScriptRun:
    def test_run_script_success(self, v2_client, make_user, auth_headers, monkeypatch):
        """执行脚本 → subprocess mock 通过 → status=passed 且结果落库（Celery 关闭走同步路径）"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        sid = _create_script(v2_client, headers, script_content="print('x')")

        called = {}

        def _fake_run(*args, **kwargs):
            called["cmd"] = args[0]
            return _FakeCompleted()

        monkeypatch.setattr(subprocess, "run", _fake_run)

        resp = v2_client.post(f"{BASE}/scripts/{sid}/run", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["message"] == "脚本执行完成"
        assert body["data"]["status"] == "passed"
        assert body["data"]["result"]["stdout"] == "hello from fake appium script"
        assert called["cmd"][0] == sys.executable
        assert called["cmd"][1].endswith(".py")

        # 详情状态同步更新
        resp = v2_client.get(f"{BASE}/scripts/{sid}", headers=headers)
        assert resp.json()["data"]["status"] == "passed"
        assert resp.json()["data"]["last_run_at"] is not None

    def test_run_script_not_found(self, v2_client, make_user, auth_headers):
        """执行不存在的脚本 → 404"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        resp = v2_client.post(f"{BASE}/scripts/999999/run", headers=headers)
        assert resp.status_code == 404, resp.text

    def test_run_script_celery_dispatch(self, v2_client, make_user, auth_headers, app, monkeypatch):
        """CELERY_ENABLE=true → 派发 run_app_test_task（Celery mock，零外呼）"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)
        sid = _create_script(v2_client, headers)

        dispatched = {}

        class _FakeTask:
            @staticmethod
            def apply_async(args=None, **kwargs):
                dispatched["args"] = args

                class _AsyncResult:
                    id = "fake-celery-task-id"

                return _AsyncResult()

        import app.tasks as tasks_pkg

        monkeypatch.setattr(tasks_pkg, "run_app_test_task", _FakeTask, raising=False)
        monkeypatch.setitem(get_config(), "CELERY_ENABLE", True)

        resp = v2_client.post(f"{BASE}/scripts/{sid}/run", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["data"]["task_id"] == "fake-celery-task-id"
        assert body["data"]["status"] == "running"
        assert dispatched["args"] == [sid, user_id]


# ---------------------------------------------------------------------------
# 5. 设备列表（Appium requests mock，零外呼）
# ---------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class TestAppDevices:
    def test_devices_appium_down(self, v2_client, make_user, auth_headers, monkeypatch):
        """Appium 不可达 → 200 + connected=False + 空列表（v1 降级行为）"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)

        import requests

        def _raise(*args, **kwargs):
            raise requests.ConnectionError("appium down")

        monkeypatch.setattr(requests, "get", _raise)

        resp = v2_client.get(f"{BASE}/devices", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()["data"]
        assert body["server_status"]["connected"] is False
        assert body["devices"] == []

    def test_devices_appium_connected(self, v2_client, make_user, auth_headers, monkeypatch):
        """Appium 正常 → 设备列表字段映射与 v1 一致"""
        user_id = make_user(_username())
        headers = auth_headers(user_id)

        import requests

        responses = {
            "status": _FakeResp(200, {"value": {"build": {"version": "2.1.0"}}}),
            "sessions": _FakeResp(200, {"value": [
                {
                    "id": "session-1",
                    "capabilities": {
                        "deviceName": "Pixel 7",
                        "platformName": "Android",
                        "platformVersion": "14",
                        "udid": "emu-1234",
                        "screenSize": "1080x2400",
                    },
                },
            ]}),
        }

        def _fake_get(url, *args, **kwargs):
            for key, resp in responses.items():
                if url.endswith(f"/{key}"):
                    return resp
            raise AssertionError(f"unexpected url {url}")

        monkeypatch.setattr(requests, "get", _fake_get)

        resp = v2_client.get(f"{BASE}/devices?server_url=http://localhost:4723", headers=headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["server_status"]["connected"] is True
        assert data["server_status"]["version"] == "2.1.0"
        assert data["server_status"]["device_count"] == 1
        device = data["devices"][0]
        assert device["id"] == "session-1"
        assert device["name"] == "Pixel 7"
        assert device["platform"] == "android"  # v1 统一小写
        assert device["version"] == "14"
        assert device["udid"] == "emu-1234"
