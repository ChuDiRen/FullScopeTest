"""
gRPC (Dubbo3 Triple) 压测脚本服务

职责：
- 将用户提供的 .proto 编译为 FileDescriptorSet（base64），保存后运行时免 protoc
- 校验 RPC 方法存在于 descriptor 中
- 生成自包含的 Locust gRPC 压测脚本（描述符池 + unary 调用 + 事件上报）

设计约束：
- 生成的脚本必须通过 check_script_safety(allow_network_libs=True)：
  只允许 import base64/json/time/grpc/google.protobuf/locust，
  禁止出现 os/sys/open/getattr 及 dunder 属性访问
- Dubbo3 Triple 协议与 gRPC 兼容（unary 调用），目标地址支持
  grpc://host:port / triple://host:port / host:port
"""

import base64
import json
import os
import re
import tempfile

from ...core.logging import get_logger

logger = get_logger(__name__)

METHOD_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.]*/[A-Za-z][A-Za-z0-9_]*$")

# 生成的 locustfile 里允许出现的顶层 import（与沙箱白名单对齐，新增需同步评估沙箱）
SCRIPT_TEMPLATE = '''"""Locust gRPC 压测脚本（自动生成，兼容 Dubbo3 Triple / gRPC unary）"""
import base64
import json
import time

import grpc
from google.protobuf import descriptor_pb2, descriptor_pool, json_format, message_factory
from locust import User, between, task

DESCRIPTOR_B64 = {descriptor_b64}
METHOD_FULL = {method_full}
REQUEST_JSON = {request_json}

_fds = descriptor_pb2.FileDescriptorSet()
_fds.ParseFromString(base64.b64decode(DESCRIPTOR_B64))
_pool = descriptor_pool.DescriptorPool()
for _fd in _fds.file:
    _pool.Add(_fd)

_service_name, _method_name = METHOD_FULL.strip("/").split("/")
_service = _pool.FindServiceByName(_service_name)
_method = None
for _m in _service.methods:
    if _m.name == _method_name:
        _method = _m
        break
if _method is None:
    raise RuntimeError("RPC method not found: " + METHOD_FULL)
_request_class = message_factory.GetMessageClass(
    _pool.FindMessageTypeByName(_method.input_type.full_name)
)
_response_class = message_factory.GetMessageClass(
    _pool.FindMessageTypeByName(_method.output_type.full_name)
)
_request_template = json.loads(REQUEST_JSON)


class GrpcUser(User):
    """gRPC unary 压测用户"""

    wait_time = between(1, 2)
    grpc_timeout = 10

    def on_start(self):
        target = (self.host or "").strip()
        for _prefix in ("grpc://", "triple://", "http://", "https://"):
            if target.startswith(_prefix):
                target = target[len(_prefix):]
                break
        self._channel = grpc.insecure_channel(target)
        self._call = self._channel.unary_unary(
            METHOD_FULL,
            request_serializer=lambda msg: msg.SerializeToString(),
            response_deserializer=_response_class.FromString,
        )

    @task
    def call_rpc(self):
        request = _request_class()
        json_format.ParseDict(_request_template, request)
        start = time.perf_counter()
        exception = None
        response_length = 0
        try:
            response = self._call(request, timeout=self.grpc_timeout)
            response_length = response.ByteSize()
        except Exception as exc:
            exception = exc
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        self.environment.events.request.fire(
            request_type="GRPC",
            name=METHOD_FULL,
            response_time=elapsed_ms,
            response_length=response_length,
            exception=exception,
            context={{}},
        )

    def on_stop(self):
        try:
            self._channel.close()
        except Exception:
            pass
'''


class GrpcScriptError(ValueError):
    """proto 编译 / 方法校验 / 请求 JSON 校验失败"""


def compile_proto_to_descriptor_b64(proto_content: str) -> str:
    """编译 .proto 源码为 FileDescriptorSet base64。编译失败抛 GrpcScriptError。"""
    if not proto_content or not proto_content.strip():
        raise GrpcScriptError("proto_content 不能为空")

    from grpc_tools import protoc

    with tempfile.TemporaryDirectory(prefix="fst_proto_") as work_dir:
        proto_path = os.path.join(work_dir, "perf_scenario.proto")
        with open(proto_path, "w", encoding="utf-8") as f:
            f.write(proto_content)
        descriptor_path = os.path.join(work_dir, "descriptor.pb")
        # grpc_tools.protoc 自带 google/protobuf well-known 类型，第三方 import 不支持
        rc = protoc.main([
            "protoc",
            f"-I{work_dir}",
            f"--descriptor_set_out={descriptor_path}",
            "--include_imports",
            proto_path,
        ])
        if rc != 0:
            raise GrpcScriptError("proto 编译失败，请检查 .proto 语法与 import（仅支持官方 well-known 类型）")
        with open(descriptor_path, "rb") as f:
            return base64.b64encode(f.read()).decode("ascii")


def validate_grpc_method(descriptor_b64: str, method: str) -> tuple:
    """校验 RPC 方法在 descriptor 中存在，返回 (service_full_name, method_name)。"""
    if not method or not METHOD_PATTERN.match(method.strip()):
        raise GrpcScriptError("grpc_method 格式应为 package.Service/Method，例如 helloworld.Greeter/SayHello")

    from google.protobuf import descriptor_pb2, descriptor_pool

    service_name, method_name = method.strip().strip("/").split("/")
    try:
        fds = descriptor_pb2.FileDescriptorSet()
        fds.ParseFromString(base64.b64decode(descriptor_b64))
        pool = descriptor_pool.DescriptorPool()
        for fd in fds.file:
            pool.Add(fd)
        service = pool.FindServiceByName(service_name)
    except Exception as exc:
        raise GrpcScriptError(f"service {service_name} 不存在于编译结果中: {exc}")

    for m in service.methods:
        if m.name == method_name:
            return service_name, method_name
    raise GrpcScriptError(
        f"method {method_name} 不存在于 service {service_name}，可用方法: "
        + ", ".join(m.name for m in service.methods)
    )


def validate_grpc_request_json(request_json: str) -> str:
    """校验请求 JSON 可解析，返回规范化后的 JSON 字符串。"""
    try:
        normalized = json.loads(request_json or "{}")
    except json.JSONDecodeError as exc:
        raise GrpcScriptError(f"grpc_request_json 不是合法 JSON: {exc}")
    if not isinstance(normalized, dict):
        raise GrpcScriptError("grpc_request_json 顶层必须是 JSON 对象")
    return json.dumps(normalized, ensure_ascii=False)


def build_grpc_locust_script(descriptor_b64: str, method: str, request_json: str) -> str:
    """生成自包含 Locust gRPC 压测脚本。"""
    return SCRIPT_TEMPLATE.format(
        descriptor_b64=repr(descriptor_b64),
        method_full=repr(method.strip()),
        request_json=repr(json.dumps(json.loads(request_json), ensure_ascii=False)),
    )


def prepare_grpc_scenario(
    target_url: str,
    proto_content: str,
    grpc_method: str,
    grpc_request_json: str,
    custom_script: str = None,
) -> tuple:
    """
    端到端准备 gRPC 场景字段（v1/v2 路由共用）。

    Returns:
        (fields, error): 成功时 error 为 None，fields 含
            target_url / protocol / proto_content / grpc_method /
            grpc_request_json / grpc_descriptor / script_content / _ssrf_probe；
            失败时 fields 为空 dict，error 为用户可读信息。
        _ssrf_probe 供路由调用 is_safe_url（SSRF 防护要求 http/https scheme），
        路由侧必须 pop 掉再落库。
    """
    target = (target_url or "").strip()
    if not target:
        return {}, "target_url 不能为空"
    if "://" not in target:
        target = "grpc://" + target
    if not re.match(r"^(grpc|triple|https?)://[A-Za-z0-9_.:\[\]-]+:\d+$", target):
        return {}, "gRPC 目标格式应为 host:port 或 grpc://host:port（不含路径）"

    probe = "http://" + target.split("://", 1)[1]

    try:
        descriptor_b64 = compile_proto_to_descriptor_b64(proto_content)
        validate_grpc_method(descriptor_b64, grpc_method)
        normalized_json = validate_grpc_request_json(grpc_request_json)
        # gRPC :path 分发要求前导斜杠，统一规范化为 /package.Service/Method
        method_full = "/" + grpc_method.strip().lstrip("/")
        script = custom_script if custom_script else build_grpc_locust_script(descriptor_b64, method_full, normalized_json)
    except GrpcScriptError as exc:
        return {}, str(exc)

    return {
        "target_url": target,
        "protocol": "grpc",
        "proto_content": proto_content,
        "grpc_method": method_full,
        "grpc_request_json": normalized_json,
        "grpc_descriptor": descriptor_b64,
        "script_content": script,
        "_ssrf_probe": probe,
    }, None
