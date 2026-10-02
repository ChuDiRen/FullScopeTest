"""
AI 配置 Service

处理 AI 助手配置的读取和保存
"""

import os
import re

from .base import BaseService
from ..core.logging import get_logger
from ..core.runtime import get_config


logger = get_logger(__name__)

# 后端根目录（原 Flask current_app.root_path 的上级，即 .env 所在目录）
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


AI_CONFIG_ENV_MAP = {
    "base_url": "AI_ASSISTANT_BASE_URL",
    "model": "AI_ASSISTANT_MODEL",
    "api_key": "AI_ASSISTANT_API_KEY",
}


def _mask_secret(value: str) -> str:
    if not value:
        return value
    if len(value) > 8:
        return f"{value[:4]}...{value[-4:]}"
    return "***"


def _sanitize_env_value(value: str) -> str:
    return str(value or "").replace(chr(10), "").replace(chr(13), "").strip()


class AiConfigService(BaseService):

    def get_config(self):
        """获取当前 AI 配置"""
        config = {
            "base_url": get_config().get("AI_ASSISTANT_BASE_URL", ""),
            "model": get_config().get("AI_ASSISTANT_MODEL", ""),
            "api_key": get_config().get("AI_ASSISTANT_API_KEY", ""),
        }
        config["api_key"] = _mask_secret(config["api_key"])
        return config

    def save_config(self, data: dict):
        """保存 AI 配置到 .env 文件"""
        payload = {}
        for field_name, env_key in AI_CONFIG_ENV_MAP.items():
            if field_name in data:
                payload[env_key] = _sanitize_env_value(data.get(field_name))

        required_fields = ["AI_ASSISTANT_BASE_URL", "AI_ASSISTANT_MODEL", "AI_ASSISTANT_API_KEY"]
        for required_field in required_fields:
            value = payload.get(required_field) or get_config().get(required_field, "")
            if not str(value).strip():
                return {"success": False, "error": f"{required_field} is required"}

        env_path = os.path.join(_BACKEND_ROOT, ".env")
        try:
            self._upsert_env_file(env_path, payload)
            for key, value in payload.items():
                os.environ[key] = value
                get_config()[key] = value
        except Exception as exc:
            logger.error("save ai config failed", error=str(exc), exc_info=True)
            return {"success": False, "error": f"保存 AI 配置失败: {str(exc)}"}

        return {
            "success": True,
            "data": {
                "base_url": get_config().get("AI_ASSISTANT_BASE_URL", ""),
                "model": get_config().get("AI_ASSISTANT_MODEL", ""),
                "api_key": _mask_secret(get_config().get("AI_ASSISTANT_API_KEY", "")),
            }
        }


    def _upsert_env_file(self, file_path: str, mapping: dict):
        """更新或追加 .env 文件中的配置项"""
        if os.path.exists(file_path):
            with open(file_path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
        else:
            lines = []

        for env_key, env_value in mapping.items():
            pattern = re.compile(rf"^\s*{re.escape(env_key)}\s*=")
            replaced = False
            for idx, line in enumerate(lines):
                if pattern.match(line):
                    lines[idx] = f"{env_key}={env_value}"
                    replaced = True
                    break
            if not replaced:
                lines.append(f"{env_key}={env_value}")

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(chr(10).join(lines) + chr(10))
