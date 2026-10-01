"""
仪表盘组件模型

存储用户的仪表盘布局配置。
每个用户独立保存自己的 Dashboard 配置。
"""
from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, JSON, String
from sqlalchemy.orm import relationship
from ..database import Base


class DashboardWidget(Base):
    """仪表盘组件配置表"""

    __tablename__ = 'dashboard_widgets'

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, comment='用户 ID')
    organization_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, comment='组织 ID')
    widget_type = Column(String(50), nullable=False, comment='组件类型')
    title = Column(String(100), comment='组件标题')
    config = Column(JSON, default=dict, comment='组件配置')
    position_x = Column(Integer, default=0, comment='网格 X 坐标')
    position_y = Column(Integer, default=0, comment='网格 Y 坐标')
    width = Column(Integer, default=1, comment='宽度（网格单位）')
    height = Column(Integer, default=1, comment='高度（网格单位）')
    is_visible = Column(Boolean, default=True, comment='是否可见')
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # 关联
    user = relationship('User', backref='dashboard_widgets')

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'widget_type': self.widget_type,
            'title': self.title,
            'config': self.config,
            'position_x': self.position_x,
            'position_y': self.position_y,
            'width': self.width,
            'height': self.height,
            'is_visible': self.is_visible,
        }


# 预设组件类型
WIDGET_TYPES = {
    'pass_rate': {'title': '测试通过率', 'default_width': 2, 'default_height': 1},
    'recent_runs': {'title': '最近执行', 'default_width': 2, 'default_height': 1},
    'failed_top10': {'title': '失败用例 Top 10', 'default_width': 1, 'default_height': 1},
    'ai_usage': {'title': 'AI 使用统计', 'default_width': 1, 'default_height': 1},
    'team_activity': {'title': '团队活跃度', 'default_width': 1, 'default_height': 1},
    'quality_gates': {'title': '质量门禁状态', 'default_width': 1, 'default_height': 1},
    'sla_rate': {'title': 'SLA 达成率', 'default_width': 1, 'default_height': 1},
    'cost_overview': {'title': '成本概览', 'default_width': 1, 'default_height': 1},
    'external_data': {'title': '外部数据源', 'default_width': 2, 'default_height': 1},
}


def create_default_widgets(user_id: int, organization_id: int):
    """为用户创建默认仪表盘布局"""
    widgets = []
    x, y = 0, 0
    for widget_type, config in WIDGET_TYPES.items():
        widget = DashboardWidget(
            user_id=user_id,
            organization_id=organization_id,
            widget_type=widget_type,
            title=config['title'],
            position_x=x,
            position_y=y,
            width=config['default_width'],
            height=config['default_height'],
        )
        widgets.append(widget)
        x += config['default_width']
        if x >= 4:
            x = 0
            y += 1
    return widgets
