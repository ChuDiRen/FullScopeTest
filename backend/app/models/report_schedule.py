"""
定时报告调度模型

按 daily/weekly/monthly 频率聚合测试执行统计并通过 webhook 通知。
调度执行见 app/scheduler.py: execute_report_schedule
"""

from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String
from ..database import Base


class ReportSchedule(Base):
    """定时报告调度表"""

    __tablename__ = 'report_schedules'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, comment='属主用户 ID')
    project_id = Column(Integer, ForeignKey('projects.id'), nullable=True, comment='限定项目 ID')
    name = Column(String(255), nullable=False, comment='调度名称')
    frequency = Column(String(20), default='daily', comment='频率: daily / weekly / monthly')
    recipients = Column(JSON, default=list, comment='接收人列表（展示/备注）')
    webhook_url = Column(String(500), default='', comment='通知 webhook 地址（钉钉/飞书等）')
    is_active = Column(Boolean, default=True, comment='是否启用')
    last_run_at = Column(DateTime, comment='最近执行时间')
    last_result = Column(JSON, comment='最近执行结果快照（统计聚合）')

    created_at = Column(DateTime, default=datetime.utcnow, comment='创建时间')
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, comment='更新时间')

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'project_id': self.project_id,
            'name': self.name,
            'frequency': self.frequency,
            'recipients': self.recipients or [],
            'webhook_url': self.webhook_url or '',
            'is_active': self.is_active,
            'last_run_at': self.last_run_at.isoformat() if self.last_run_at else None,
            'last_result': self.last_result,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self):
        return f'<ReportSchedule {self.name}>'
