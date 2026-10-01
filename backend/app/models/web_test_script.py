"""
Web 自动化测试脚本模型

存储 Playwright 测试脚本
"""

from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, JSON, String, Text
from ..database import Base


class WebTestScript(Base):
    """Web 测试脚本表"""
    
    __tablename__ = 'web_test_scripts'
    
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey('projects.id'), nullable=True, comment='项目 ID')
    collection_id = Column(Integer, ForeignKey('web_test_collections.id'), nullable=True, comment='用例集 ID')
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, comment='用户 ID')
    name = Column(String(255), nullable=False, comment='脚本名称')
    description = Column(Text, comment='脚本描述')
    
    # 脚本内容
    script_content = Column(Text, nullable=False, comment='Python 脚本代码')
    script_type = Column(String(20), default='playwright', comment='脚本类型: playwright/selenium')
    target_url = Column(String(500), comment='目标URL')
    
    # 执行配置
    browser = Column(String(20), default='chromium', comment='浏览器: chromium/firefox/webkit')
    headless = Column(Boolean, default=True, comment='是否无头模式')
    timeout = Column(Integer, default=30000, comment='超时时间(毫秒)')
    viewport_width = Column(Integer, default=1280, comment='视口宽度')
    viewport_height = Column(Integer, default=720, comment='视口高度')
    config = Column(JSON, default=dict, comment='其他配置')
    
    # 状态信息
    status = Column(String(20), default='pending', comment='当前状态: pending/running/passed/failed')
    last_status = Column(String(20), comment='最后执行状态')
    last_run_at = Column(DateTime, comment='最后运行时间')
    last_run_duration = Column(Float, comment='最后运行耗时(秒)')
    last_result = Column(JSON, comment='最后执行结果')
    step_count = Column(Integer, default=0, comment='测试步骤数')
    
    # 其他配置
    tags = Column(JSON, default=list, comment='标签')
    is_enabled = Column(Boolean, default=True, comment='是否启用')
    sort_order = Column(Integer, default=0, comment='排序顺序')
    
    created_at = Column(DateTime, default=datetime.utcnow, comment='创建时间')
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, comment='更新时间')

    @staticmethod
    def normalize_status(status):
        """Normalize legacy status values for API responses."""
        status_mapping = {
            'success': 'passed',
            'timeout': 'failed',
        }
        return status_mapping.get(status, status or 'pending')
    
    def to_dict(self):
        """转换为字典"""
        normalized_status = self.normalize_status(self.status)
        normalized_last_status = self.normalize_status(self.last_status)

        return {
            'id': self.id,
            'project_id': self.project_id,
            'collection_id': self.collection_id,
            'collection_name': self.collection.name if self.collection else None,
            'user_id': self.user_id,
            'name': self.name,
            'description': self.description,
            'script_content': self.script_content,
            'script_type': self.script_type,
            'target_url': self.target_url,
            'browser': self.browser,
            'headless': self.headless,
            'timeout': self.timeout,
            'viewport_width': self.viewport_width,
            'viewport_height': self.viewport_height,
            'config': self.config,
            'status': normalized_status,
            'last_status': normalized_last_status,
            'last_run_at': self.last_run_at.isoformat() if self.last_run_at else None,
            'last_run_duration': self.last_run_duration,
            'last_result': self.last_result,
            'step_count': self.step_count,
            'tags': self.tags,
            'is_enabled': self.is_enabled,
            'sort_order': self.sort_order,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }
    
    def __repr__(self):
        return f'<WebTestScript {self.name}>'
