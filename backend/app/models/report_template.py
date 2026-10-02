"""
报告模板模型

自定义测试报告的展示模块组合与配色主题。
"""

from datetime import datetime
from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String
from ..database import Base


class ReportTemplate(Base):
    """报告模板表"""

    __tablename__ = 'report_templates'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, comment='属主用户 ID')
    name = Column(String(255), nullable=False, comment='模板名称')
    modules = Column(JSON, default=list, comment='启用的展示模块列表（含排序）')
    theme = Column(String(30), default='default', comment='配色主题')

    created_at = Column(DateTime, default=datetime.utcnow, comment='创建时间')
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, comment='更新时间')

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'name': self.name,
            'modules': self.modules or [],
            'theme': self.theme or 'default',
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self):
        return f'<ReportTemplate {self.name}>'
