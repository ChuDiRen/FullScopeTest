"""
gRPC (Dubbo3 Triple) 压测功能测试

覆盖：proto 编译、方法/请求校验、脚本生成与沙箱合规、场景 API（v1/v2）、
真实 localhost gRPC 压测冒烟（起服务跑 Locust）。
"""
import csv
import glob
import os
import subprocess
import sys
import time
from concurrent import futures

import pytest

from app.extensions import db
from app.models.perf_test_scenario import PerfTestScenario
from app.services.perf.grpc_script import (
    GrpcScriptError,
    build_grpc_locust_script,
    compile_proto_to_descriptor_b64,
    prepare_grpc_scenario,
    validate_grpc_method,
    validate_grpc_request_json,
)
from app.utils.sandbox import check_script_safety

HELLO_PROTO = """
syntax = "proto3";
package helloworld;
service Greeter {
  rpc SayHello (HelloRequest) returns (HelloReply) {}
}
message HelloRequest {
  string name = 1;
}
message HelloReply {
  string message = 1;
}
"""

METHOD = "helloworld.Greeter/SayHello"
REQUEST_JSON = '{"name": "压测"}'


def _auth_headers(client, username=None):
    import uuid
    uid = uuid.uuid4().hex[:8]
    username = username or f"grpc_{uid}"
    password = "Passw0rd!"
    client.post("/api/v1/auth/register", json={
        "username": username, "email": f"{username}@example.com", "password": password,
    })
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {resp.json()['data']['access_token']}"}


# ══════════════════════════════════════════════════════════════════════════════
# 单元：编译 / 校验 / 脚本生成
# ══════════════════════════════════════════════════════════════════════════════

def test_compile_proto_and_validate_method():
    b64 = compile_proto_to_descriptor_b64(HELLO_PROTO)
    service_name, method_name = validate_grpc_method(b64, METHOD)
    assert service_name == "helloworld.Greeter"
    assert method_name == "SayHello"


def test_bad_proto_rejected():
    with pytest.raises(GrpcScriptError):
        compile_proto_to_descriptor_b64('syntax = "proto3"; garbage here')


def test_bad_method_rejected():
    b64 = compile_proto_to_descriptor_b64(HELLO_PROTO)
    with pytest.raises(GrpcScriptError, match="不存在"):
        validate_grpc_method(b64, "helloworld.Greeter/Nope")


def test_bad_request_json_rejected():
    with pytest.raises(GrpcScriptError):
        validate_grpc_request_json("{not json")
    with pytest.raises(GrpcScriptError):
        validate_grpc_request_json("[1, 2]")


def test_generated_script_passes_sandbox():
    b64 = compile_proto_to_descriptor_b64(HELLO_PROTO)
    script = build_grpc_locust_script(b64, METHOD, REQUEST_JSON)
    safe, reason = check_script_safety(script, allow_network_libs=True)
    assert safe, f"生成的 gRPC 压测脚本必须通过沙箱检查: {reason}"
    # 硬约束：不得出现危险导入（Web/App 沙箱同源黑名单）
    for banned in ("import os", "import sys", "import subprocess", "open("):
        assert banned not in script, f"生成脚本不应包含 {banned}"


def test_method_path_canonicalized():
    """不带前导斜杠的方法名应规范化为 /pkg.Svc/Method（gRPC :path 分发要求）"""
    fields, err = prepare_grpc_scenario(
        "grpc://127.0.0.1:59999", HELLO_PROTO, METHOD, REQUEST_JSON)
    assert err is None
    assert fields["grpc_method"] == "/" + METHOD
    assert "METHOD_FULL = '" + "/" + METHOD + "'" in fields["script_content"]


def test_prepare_grpc_scenario_invalid_target():
    fields, err = prepare_grpc_scenario(
        "grpc://127.0.0.1:port/notapath", HELLO_PROTO, METHOD, REQUEST_JSON)
    assert fields == {}
    assert err


# ══════════════════════════════════════════════════════════════════════════════
# API：v1 场景创建 / 更新
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(autouse=True)
def _allow_localhost_ssrf(monkeypatch):
    """gRPC 压测目标固定为 localhost，走 SSRF 白名单（与 api_v2 套件同做法）"""
    monkeypatch.setenv("SSRF_ALLOWLIST_HOSTS", "127.0.0.1")


def test_v1_create_grpc_scenario(app, client, no_rate_limit):
    headers = _auth_headers(client)
    resp = client.post("/api/v1/perf-test/scenarios", headers=headers, json={
        "name": "grpc-hello",
        "target_url": "grpc://127.0.0.1:59999",
        "protocol": "grpc",
        "proto_content": HELLO_PROTO,
        "grpc_method": METHOD,
        "grpc_request_json": REQUEST_JSON,
        "user_count": 2,
        "spawn_rate": 2,
        "duration": 10,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["protocol"] == "grpc"
    assert data["grpc_method"] == "/" + METHOD
    assert "GrpcUser" in data["script_content"]


def test_v1_create_grpc_bad_proto_400(app, client, no_rate_limit):
    headers = _auth_headers(client)
    resp = client.post("/api/v1/perf-test/scenarios", headers=headers, json={
        "name": "grpc-bad",
        "target_url": "grpc://127.0.0.1:59999",
        "protocol": "grpc",
        "proto_content": "not a proto",
        "grpc_method": METHOD,
        "grpc_request_json": REQUEST_JSON,
    })
    assert resp.status_code == 400


def test_v1_update_http_to_grpc(app, client, no_rate_limit):
    headers = _auth_headers(client)
    resp = client.post("/api/v1/perf-test/scenarios", headers=headers, json={
        "name": "http-scenario",
        "target_url": "http://127.0.0.1:59999/api",
        "method": "GET",
        "user_count": 2, "spawn_rate": 2, "duration": 10,
    })
    sid = resp.json()["data"]["id"]

    resp = client.put(f"/api/v1/perf-test/scenarios/{sid}", headers=headers, json={
        "target_url": "grpc://127.0.0.1:59999",
        "protocol": "grpc",
        "proto_content": HELLO_PROTO,
        "grpc_method": METHOD,
        "grpc_request_json": REQUEST_JSON,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["protocol"] == "grpc"
    assert "GrpcUser" in data["script_content"]


def test_create_grpc_scenario(app, client, no_rate_limit):
    headers = _auth_headers(client)
    resp = client.post("/api/v1/perf-test/scenarios", headers=headers, json={
        "name": "grpc-hello",
        "target_url": "grpc://127.0.0.1:59999",
        "protocol": "grpc",
        "proto_content": HELLO_PROTO,
        "grpc_method": METHOD,
        "grpc_request_json": REQUEST_JSON,
        "user_count": 2, "spawn_rate": 2, "duration": 10,
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["protocol"] == "grpc"
    assert "GrpcUser" in data["script_content"]


# ══════════════════════════════════════════════════════════════════════════════
# 真实压测冒烟：进程内 gRPC 服务 + 生成脚本跑 Locust（localhost，非外呼）
# ══════════════════════════════════════════════════════════════════════════════

def test_real_grpc_load_run(app, client, no_rate_limit, tmp_path):
    grpc = pytest.importorskip("grpc")
    pytest.importorskip("grpc_tools")

    # 1) 编译 proto stubs 并启动进程内 gRPC 服务（OS 分配端口）
    from grpc_tools import protoc
    proto_path = tmp_path / "perf.proto"
    proto_path.write_text(HELLO_PROTO, encoding="utf-8")
    rc = protoc.main([
        "protoc", f"-I{tmp_path}",
        f"--python_out={tmp_path}", f"--grpc_python_out={tmp_path}",
        str(proto_path),
    ])
    assert rc == 0, "protoc 编译失败"
    sys.path.insert(0, str(tmp_path))
    try:
        import perf_pb2, perf_pb2_grpc  # noqa: E402

        class Greeter(perf_pb2_grpc.GreeterServicer):
            def SayHello(self, request, context):
                return perf_pb2.HelloReply(message="hello " + request.name)

        server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
        perf_pb2_grpc.add_GreeterServicer_to_server(Greeter(), server)
        port = server.add_insecure_port("127.0.0.1:0")
        assert port, "gRPC 测试服务端口分配失败"
        server.start()

        # 2) 创建 gRPC 场景
        headers = _auth_headers(client)
        resp = client.post("/api/v1/perf-test/scenarios", headers=headers, json={
            "name": "grpc-real-run",
            "target_url": f"grpc://127.0.0.1:{port}",
            "protocol": "grpc",
            "proto_content": HELLO_PROTO,
            "grpc_method": METHOD,
            "grpc_request_json": REQUEST_JSON,
            "user_count": 2, "spawn_rate": 2, "duration": 10,
        })
        assert resp.status_code == 200, resp.text
        script_content = resp.json()["data"]["script_content"]

        # 3) 直接以 Locust 子进程执行生成的脚本（模拟 run 任务的执行路径）
        locustfile = tmp_path / "locustfile.py"
        locustfile.write_text(script_content, encoding="utf-8")
        csv_prefix = str(tmp_path / "rt")
        proc = subprocess.run(
            [sys.executable, "-m", "locust", "-f", str(locustfile),
             "--host", f"grpc://127.0.0.1:{port}",
             "--users", "2", "--spawn-rate", "2", "--run-time", "6s",
             "--headless", "--csv", csv_prefix, "--loglevel", "WARNING",
             "--only-summary"],
            capture_output=True, text=True, timeout=90,
            stdin=subprocess.PIPE,
        )
        server.stop(0)

        agg = None
        if os.path.exists(csv_prefix + "_stats.csv"):
            with open(csv_prefix + "_stats.csv", encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if row["Name"] == "Aggregated":
                        agg = row
        assert proc.returncode == 0, (
            f"Locust 退出码 {proc.returncode}\n--- stdout tail ---\n{proc.stdout[-1200:]}"
            f"\n--- stderr tail ---\n{proc.stderr[-1500:]}")
        assert agg is not None, "Locust 未产出统计 CSV"
        assert int(agg["Request Count"]) > 0, "应产生 gRPC 请求"
        assert int(agg["Failure Count"]) == 0, \
            f"压测应全部成功，实际失败 {agg['Failure Count']} 次:\n{proc.stderr[-800:]}"
    finally:
        sys.path.remove(str(tmp_path))
