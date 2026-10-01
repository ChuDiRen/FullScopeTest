"""
接口测试用例模型

存储 API 测试集合和用例
"""

from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.orm import backref, relationship
from ..database import Base


class ApiTestCollection(Base):
    """接口测试集合表"""

    __tablename__ = 'api_test_collections'
    __table_args__ = (
        Index('idx_api_test_collections_project_id', 'project_id'),
        Index('idx_api_test_collections_user_id', 'user_id'),
        Index('idx_api_test_collections_parent_id', 'parent_id'),
    )

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey('projects.id'), nullable=True, comment='项目 ID')
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, comment='用户 ID')
    parent_id = Column(Integer, ForeignKey('api_test_collections.id'), comment='父集合 ID')
    name = Column(String(100), nullable=False, comment='集合名称')
    description = Column(Text, comment='集合描述')
    sort_order = Column(Integer, default=0, comment='排序顺序')
    created_at = Column(DateTime, default=datetime.utcnow, comment='创建时间')
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, comment='更新时间')
    
    # 自引用关联（子集合）
    children = relationship('ApiTestCollection', backref=backref('parent', remote_side=[id]), lazy='dynamic')
    # 测试用例关联
    test_cases = relationship('ApiTestCase', backref='collection', lazy='dynamic', cascade='all, delete-orphan')
    
    def to_dict(self, include_children=False, include_cases=False):
        """转换为字典"""
        result = {
            'id': self.id,
            'project_id': self.project_id,
            'parent_id': self.parent_id,
            'name': self.name,
            'description': self.description,
            'sort_order': self.sort_order,
            'case_count': self.test_cases.count(),
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }
        
        if include_children:
            result['children'] = [c.to_dict() for c in self.children.all()]
        
        if include_cases:
            result['cases'] = [c.to_dict() for c in self.test_cases.all()]
        
        return result
    
    def __repr__(self):
        return f'<ApiTestCollection {self.name}>'


class ApiTestCase(Base):
    """接口测试用例表"""

    __tablename__ = 'api_test_cases'
    __table_args__ = (
        Index('idx_api_test_cases_collection_id', 'collection_id'),
        Index('idx_api_test_cases_project_id', 'project_id'),
        Index('idx_api_test_cases_user_id', 'user_id'),
        Index('idx_api_test_cases_method', 'method'),
    )

    id = Column(Integer, primary_key=True)
    collection_id = Column(Integer, ForeignKey('api_test_collections.id'), nullable=True, comment='集合 ID')
    project_id = Column(Integer, ForeignKey('projects.id'), nullable=True, comment='项目 ID')
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, comment='用户 ID')
    environment_id = Column(Integer, ForeignKey('environments.id'), nullable=True, comment='关联环境 ID')
    name = Column(String(255), nullable=False, comment='用例名称')
    description = Column(Text, comment='用例描述')
    
    # HTTP 请求配置
    method = Column(String(10), nullable=False, default='GET', comment='HTTP 方法')
    url = Column(String(500), nullable=False, comment='请求 URL')
    headers = Column(JSON, default=dict, comment='请求头')
    params = Column(JSON, default=dict, comment='URL 查询参数')
    body = Column(JSON, comment='请求体')
    body_type = Column(String(20), default='json', comment='请求体类型: json/form/raw/binary')
    
    # 断言配置
    assertions = Column(JSON, default=list, comment='断言规则列表')
    
    # 脚本配置
    pre_script = Column(Text, comment='前置脚本')
    post_script = Column(Text, comment='后置脚本')
    
    # 变量配置
    variables = Column(JSON, default=dict, comment='用例级变量')
    extract_variables = Column(JSON, default=list, comment='响应提取变量')
    
    # 其他配置
    timeout = Column(Integer, default=30, comment='超时时间(秒)')
    retry_count = Column(Integer, default=0, comment='重试次数')
    tags = Column(JSON, default=list, comment='标签')
    priority = Column(Integer, default=2, comment='优先级: 1-高 2-中 3-低')
    is_enabled = Column(Boolean, default=True, comment='是否启用')
    sort_order = Column(Integer, default=0, comment='排序顺序')
    
    # 执行状态
    last_run_at = Column(DateTime, comment='最后执行时间')
    last_status = Column(String(20), comment='最后执行状态: passed/failed/pending')
    last_result = Column(JSON, comment='最后执行结果')
    
    # Mock 配置
    mock_enabled = Column(Boolean, default=False, comment='是否启用 Mock')
    mock_response_code = Column(Integer, default=200, comment='Mock 响应状态码')
    mock_response_body = Column(Text, comment='Mock 响应体 (通常是 JSON 字符串)')
    mock_response_headers = Column(JSON, default=dict, comment='Mock 响应头')
    mock_delay_ms = Column(Integer, default=0, comment='Mock 响应延迟(毫秒)')
    
    created_at = Column(DateTime, default=datetime.utcnow, comment='创建时间')
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, comment='更新时间')
    
    def to_dict(self):
        """转换为字典"""
        return {
            'id': self.id,
            'collection_id': self.collection_id,
            'project_id': self.project_id,
            'user_id': self.user_id,
            'environment_id': self.environment_id,
            'name': self.name,
            'description': self.description,
            'method': self.method,
            'url': self.url,
            'headers': self.headers,
            'params': self.params,
            'body': self.body,
            'body_type': self.body_type,
            'assertions': self.assertions,
            'pre_script': self.pre_script,
            'post_script': self.post_script,
            'variables': self.variables,
            'extract_variables': self.extract_variables,
            'timeout': self.timeout,
            'retry_count': self.retry_count,
            'tags': self.tags,
            'priority': self.priority,
            'is_enabled': self.is_enabled,
            'sort_order': self.sort_order,
            'last_run_at': self.last_run_at.isoformat() if self.last_run_at else None,
            'last_status': self.last_status or 'pending',
            'last_result': self.last_result,
            'mock_enabled': self.mock_enabled,
            'mock_response_code': self.mock_response_code,
            'mock_response_body': self.mock_response_body,
            'mock_response_headers': self.mock_response_headers,
            'mock_delay_ms': self.mock_delay_ms,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }
    
    def __repr__(self):
        return f'<ApiTestCase {self.method} {self.name}>'
