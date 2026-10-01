"""
APP 测试脚本模型

存储 Appium 测试脚本及配置
"""

from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text
from ..database import Base


class AppTestScript(Base):
    """APP 测试脚本表"""

    __tablename__ = 'app_test_scripts'

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, comment='脚本名称')
    description = Column(Text, comment='描述')
    project_id = Column(Integer, ForeignKey('projects.id'), comment='所属项目')
    collection_id = Column(Integer, ForeignKey('app_test_collections.id'), comment='所属用例集')
    user_id = Column(Integer, ForeignKey('users.id'), comment='创建者')

    # Appium 配置
    platform = Column(String(20), default='android', comment='平台: android / ios')
    app_path = Column(String(500), comment='APK/IPA 路径或 URL')
    app_package = Column(String(200), comment='Android 包名')
    app_activity = Column(String(200), comment='Android Activity')
    bundle_id = Column(String(200), comment='iOS Bundle ID')
    device_name = Column(String(100), comment='设备名称')
    platform_version = Column(String(20), comment='系统版本')
    automation_name = Column(String(50), default='UiAutomator2', comment='自动化引擎: UiAutomator2 / XCUITest')
    appium_server = Column(String(200), default='http://localhost:4723', comment='Appium Server 地址')

    # 脚本内容
    script_content = Column(Text, comment='Python Appium 脚本')

    # 执行状态
    status = Column(String(20), default='pending', comment='状态: pending/running/passed/failed')
    last_result = Column(JSON, comment='最后一次执行结果')
    last_run_at = Column(DateTime, comment='最后执行时间')

    # 元数据
    is_enabled = Column(Boolean, default=True, comment='是否启用')
    sort_order = Column(Integer, default=0, comment='排序顺序')
    created_at = Column(DateTime, default=datetime.utcnow, comment='创建时间')
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, comment='更新时间')

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'project_id': self.project_id,
            'collection_id': self.collection_id,
            'platform': self.platform,
            'app_path': self.app_path,
            'app_package': self.app_package,
            'app_activity': self.app_activity,
            'bundle_id': self.bundle_id,
            'device_name': self.device_name,
            'platform_version': self.platform_version,
            'automation_name': self.automation_name,
            'appium_server': self.appium_server,
            'script_content': self.script_content,
            'status': self.status,
            'last_result': self.last_result,
            'last_run_at': self.last_run_at.isoformat() if self.last_run_at else None,
            'is_enabled': self.is_enabled,
            'collection_name': self.collection.name if self.collection else None,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self):
        return f'<AppTestScript {self.name}>'
