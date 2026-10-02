"""
P19 生产运维服务测试（Config 热更新）
"""

import pytest
import os


class TestConfigService:
    """配置热更新服务测试"""

    def test_get_default(self, app):
        """获取默认值"""
        from app.services.config_service import ConfigService
        svc = ConfigService()
        assert svc.get("NONEXISTENT_KEY", "default") == "default"

    def test_set_reloadable(self, app):
        """设置可热更新配置"""
        from app.services.config_service import ConfigService
        svc = ConfigService()
        result = svc.set("PARALLEL_WORKERS", "10")
        assert result["success"] is True
        assert svc.get("PARALLEL_WORKERS") == "10"

    def test_set_requires_restart(self, app):
        """设置需重启的配置应失败"""
        from app.services.config_service import ConfigService
        svc = ConfigService()
        result = svc.set("DATABASE_URL", "sqlite:///new.db")
        assert result["success"] is False
        assert "重启" in result["message"]

    def test_rollback(self, app):
        """回滚配置"""
        from app.services.config_service import ConfigService
        svc = ConfigService()
        svc.set("PARALLEL_WORKERS", "5")
        svc.set("PARALLEL_WORKERS", "10")
        assert svc.get("PARALLEL_WORKERS") == "10"
        svc.rollback("PARALLEL_WORKERS")
        assert svc.get("PARALLEL_WORKERS") == "5"

    def test_history(self, app):
        """变更历史应被记录"""
        from app.services.config_service import ConfigService
        svc = ConfigService()
        svc.set("PARALLEL_WORKERS", "5")
        svc.set("PARALLEL_WORKERS", "10")
        history = svc.get_history()
        assert len(history) == 2

    def test_is_reloadable(self, app):
        """检查配置是否可热更新"""
        from app.services.config_service import ConfigService
        svc = ConfigService()
        assert svc.is_reloadable("PARALLEL_WORKERS") is True
        assert svc.is_reloadable("DATABASE_URL") is False
