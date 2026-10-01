"""
测试文档模型

存储测试文档
"""

from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import relationship
from ..database import Base


class TestDocument(Base):
    """测试文档表"""
    
    __tablename__ = 'test_documents'
    
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey('projects.id'), nullable=False, comment='项目 ID')
    
    # 文档信息
    title = Column(String(255), nullable=False, comment='文档标题')
    content = Column(Text, comment='文档内容 (Markdown)')
    
    # 文档分类
    category = Column(String(50), default='other', comment='分类: test_plan/test_case/test_report/other')
    
    # 版本管理
    version = Column(String(20), default='1.0', comment='版本号')
    
    # 作者
    created_by = Column(Integer, ForeignKey('users.id'), comment='创建者 ID')
    updated_by = Column(Integer, comment='最后更新者 ID')
    
    # 其他
    tags = Column(JSON, default=list, comment='标签')
    is_published = Column(Boolean, default=False, comment='是否发布')
    
    created_at = Column(DateTime, default=datetime.utcnow, comment='创建时间')
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, comment='更新时间')
    
    # 关联
    author = relationship('User', foreign_keys=[created_by], backref='documents')
    
    def to_dict(self):
        """转换为字典"""
        return {
            'id': self.id,
            'project_id': self.project_id,
            'title': self.title,
            'content': self.content,
            'category': self.category,
            'version': self.version,
            'created_by': self.created_by,
            'updated_by': self.updated_by,
            'author_name': self.author.username if self.author else None,
            'tags': self.tags,
            'is_published': self.is_published,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }
    
    def __repr__(self):
        return f'<TestDocument {self.title}>'
