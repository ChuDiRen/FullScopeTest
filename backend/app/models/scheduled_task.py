"""
定时任务模型
"""
from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String
from ..database import Base

class ScheduledTask(Base):
    """定时任务表"""
    
    __tablename__ = 'scheduled_tasks'
    
    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey('projects.id'), nullable=False, comment='项目 ID')
    name = Column(String(100), nullable=False, comment='任务名称')
    cron_expression = Column(String(100), nullable=False, comment='Cron 表达式')
    target_type = Column(String(50), nullable=False, comment='目标类型: api_collection, web_collection, perf_scenario')
    target_id = Column(Integer, nullable=False, comment='目标 ID')
    is_active = Column(Boolean, default=True, comment='是否启用')
    notify_webhook = Column(String(500), nullable=True, comment='通知 Webhook (如钉钉/飞书)')
    notify_events = Column(String(50), default='all', comment='通知事件: all, failed, passed')
    created_at = Column(DateTime, default=datetime.utcnow, comment='创建时间')
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, comment='更新时间')
    
    def to_dict(self):
        return {
            'id': self.id,
            'project_id': self.project_id,
            'name': self.name,
            'cron_expression': self.cron_expression,
            'target_type': self.target_type,
            'target_id': self.target_id,
            'is_active': self.is_active,
            'notify_webhook': self.notify_webhook,
            'notify_events': self.notify_events,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }
