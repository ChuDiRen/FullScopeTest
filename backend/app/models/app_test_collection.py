"""
APP 测试用例集模型

管理 APP 测试脚本的分组
"""

from datetime import datetime
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship
from ..database import Base


class AppTestCollection(Base):
    """APP 测试用例集表"""

    __tablename__ = 'app_test_collections'

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, comment='用例集名称')
    description = Column(Text, comment='描述')
    project_id = Column(Integer, ForeignKey('projects.id'), comment='所属项目')
    user_id = Column(Integer, ForeignKey('users.id'), comment='创建者')
    sort_order = Column(Integer, default=0, comment='排序顺序')
    created_at = Column(DateTime, default=datetime.utcnow, comment='创建时间')
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, comment='更新时间')

    # 关联
    scripts = relationship('AppTestScript', backref='collection', lazy='dynamic', cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'project_id': self.project_id,
            'script_count': self.scripts.count(),
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self):
        return f'<AppTestCollection {self.name}>'
