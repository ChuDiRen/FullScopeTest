"""
项目模型

存储测试项目信息
"""

from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import relationship
from ..database import Base


class Project(Base):
    """项目表"""
    
    __tablename__ = 'projects'
    
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, comment='项目名称')
    description = Column(Text, comment='项目描述')
    owner_id = Column(Integer, ForeignKey('users.id'), nullable=False, comment='所有者 ID')
    organization_id = Column(Integer, ForeignKey('organizations.id'), nullable=True, comment='组织 ID')
    is_pinned = Column(Boolean, default=False, comment='是否置顶')
    pinned_at = Column(DateTime, nullable=True, comment='置顶时间')
    settings = Column(JSON, default=dict, comment='项目设置')
    created_at = Column(DateTime, default=datetime.utcnow, comment='创建时间')
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, comment='更新时间')
    
    # 关联
    environments = relationship('Environment', backref='project', lazy='dynamic', cascade='all, delete-orphan')
    api_collections = relationship('ApiTestCollection', backref='project', lazy='dynamic', cascade='all, delete-orphan')
    web_collections = relationship('WebTestCollection', backref='project', lazy='dynamic', cascade='all, delete-orphan')
    web_scripts = relationship('WebTestScript', backref='project', lazy='dynamic', cascade='all, delete-orphan')
    perf_scenarios = relationship('PerfTestScenario', backref='project', lazy='dynamic', cascade='all, delete-orphan')
    test_runs = relationship('TestRun', backref='project', lazy='dynamic', cascade='all, delete-orphan')
    documents = relationship('TestDocument', backref='project', lazy='dynamic', cascade='all, delete-orphan')
    
    def to_dict(self):
        """转换为字典"""
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'owner_id': self.owner_id,
            'organization_id': self.organization_id,
            'is_pinned': self.is_pinned,
            'pinned_at': self.pinned_at.isoformat() if self.pinned_at else None,
            'settings': self.settings,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            # 统计信息
            'env_count': self.environments.count(),
            'api_collection_count': self.api_collections.count(),
            'web_script_count': self.web_scripts.count(),
            'perf_scenario_count': self.perf_scenarios.count()
        }
    
    def __repr__(self):
        return f'<Project {self.name}>'
