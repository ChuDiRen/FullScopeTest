"""
视觉回归测试基准截图模型

存储测试用例的基准截图记录
"""

from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import relationship
from ..database import Base


class VisualBaseline(Base):
    """视觉回归基准截图表"""

    __tablename__ = 'visual_baselines'
    __table_args__ = (
        Index('idx_visual_baselines_test_case_id', 'test_case_id'),
        Index('idx_visual_baselines_project_id', 'project_id'),
        Index('idx_visual_baselines_step_index', 'step_index'),
    )

    id = Column(Integer, primary_key=True)
    test_case_id = Column(Integer, nullable=False, comment='关联的测试用例 ID（api/web/app 测试）')
    test_type = Column(String(20), nullable=False, comment='测试类型: api/web/app')
    project_id = Column(Integer, ForeignKey('projects.id'), nullable=False, comment='项目 ID')

    # 截图信息
    step_index = Column(Integer, nullable=False, comment='测试步骤索引')
    step_name = Column(String(255), comment='测试步骤名称')
    baseline_image_path = Column(String(500), nullable=False, comment='基准截图存储路径')

    # 截图元数据
    viewport_width = Column(Integer, comment='视口宽度')
    viewport_height = Column(Integer, comment='视口高度')
    device_pixel_ratio = Column(Float, default=1.0, comment='设备像素比')
    full_page = Column(Boolean, default=False, comment='是否全页截图')

    # 状态
    status = Column(String(20), default='active', comment='状态: active/deprecated/pending')
    version = Column(Integer, default=1, comment='基准版本号')

    # 关联信息
    approved_by = Column(Integer, comment='批准人用户 ID')
    approved_at = Column(DateTime, comment='批准时间')
    notes = Column(Text, comment='备注')

    created_at = Column(DateTime, default=datetime.utcnow, comment='创建时间')
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, comment='更新时间')

    # 关联关系
    project = relationship('Project', backref='visual_baselines')

    def to_dict(self):
        """转换为字典"""
        return {
            'id': self.id,
            'test_case_id': self.test_case_id,
            'test_type': self.test_type,
            'project_id': self.project_id,
            'step_index': self.step_index,
            'step_name': self.step_name,
            'baseline_image_path': self.baseline_image_path,
            'viewport_width': self.viewport_width,
            'viewport_height': self.viewport_height,
            'device_pixel_ratio': self.device_pixel_ratio,
            'full_page': self.full_page,
            'status': self.status,
            'version': self.version,
            'approved_by': self.approved_by,
            'approved_at': self.approved_at.isoformat() if self.approved_at else None,
            'notes': self.notes,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }

    def __repr__(self):
        return f'<VisualBaseline {self.test_case_id} step:{self.step_index} v{self.version}>'
