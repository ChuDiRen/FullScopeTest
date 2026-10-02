"""
测试用例模板模型

系统内置模板（user_id 为 NULL）+ 用户自定义模板，供新建接口用例时快速套用。
"""

from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text, select
from ..database import Base


class TestCaseTemplate(Base):
    """测试用例模板表"""

    __tablename__ = 'test_case_templates'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=True, comment='属主用户 ID（NULL=系统内置）')
    name = Column(String(255), nullable=False, comment='模板名称')
    description = Column(Text, comment='模板说明')
    category = Column(String(50), default='通用', comment='分类: CRUD/认证/监控/通用...')
    method = Column(String(10), default='GET', comment='HTTP 方法')
    endpoint = Column(String(500), default='', comment='URL 模式（支持 {{base_url}} 占位）')
    headers = Column(Text, default='{}', comment='请求头 JSON 字符串')
    body = Column(Text, default='', comment='请求体模板')
    assertions = Column(String(500), default='', comment='断言说明')

    created_at = Column(DateTime, default=datetime.utcnow, comment='创建时间')
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, comment='更新时间')

    @property
    def is_builtin(self):
        return self.user_id is None

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'is_builtin': self.is_builtin,
            'name': self.name,
            'description': self.description or '',
            'category': self.category,
            'method': self.method,
            'endpoint': self.endpoint,
            'headers': self.headers or '{}',
            'body': self.body or '',
            'assertions': self.assertions or '',
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f'<TestCaseTemplate {self.name}>'


BUILTIN_TEMPLATES = [
    dict(name='CRUD - Create', description='标准创建资源模板', category='CRUD', method='POST',
         endpoint='{{base_url}}/api/resource', headers='{"Content-Type": "application/json"}',
         body='{"name": "test"}', assertions='status=201, body.data.id exists'),
    dict(name='CRUD - Read', description='标准查询资源模板', category='CRUD', method='GET',
         endpoint='{{base_url}}/api/resource/{{id}}', headers='{}',
         body='', assertions='status=200, body.data.id equals {{id}}'),
    dict(name='Auth - Login', description='用户登录认证模板', category='认证', method='POST',
         endpoint='{{base_url}}/api/auth/login', headers='{"Content-Type": "application/json"}',
         body='{"username": "{{username}}", "password": "{{password}}"}',
         assertions='status=200, body.access_token exists'),
    dict(name='Pagination', description='列表分页查询模板', category='CRUD', method='GET',
         endpoint='{{base_url}}/api/resource?page=1&per_page=10', headers='{}',
         body='', assertions='status=200, body.data is array, body.total >= 0'),
    dict(name='Health Check', description='服务健康检查模板', category='监控', method='GET',
         endpoint='{{base_url}}/health', headers='{}',
         body='', assertions='status=200, response_time < 1000'),
]


def ensure_builtin_seed():
    """幂等种子：test_case_templates 表中无内置模板时插入 5 个系统模板"""
    from ..extensions import db

    exists = db.session.scalar(
        select(TestCaseTemplate.id).filter_by(user_id=None).limit(1)
    )
    if exists:
        return 0
    for item in BUILTIN_TEMPLATES:
        db.session.add(TestCaseTemplate(user_id=None, **item))
    db.session.commit()
    return len(BUILTIN_TEMPLATES)
