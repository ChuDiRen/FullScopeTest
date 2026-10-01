"""
Prompt 版本管理模型

支持 Prompt 的版本控制、A/B 测试和效果对比
"""

from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import relationship
from ..database import Base


class PromptVersion(Base):
    """Prompt 版本表 - 管理 Prompt 的多版本和 A/B 测试"""

    __tablename__ = 'prompt_versions'
    __table_args__ = (
        Index('idx_prompt_versions_feature', 'feature'),
        Index('idx_prompt_versions_is_active', 'is_active'),
        Index('idx_prompt_versions_version', 'version'),
    )

    id = Column(Integer, primary_key=True)

    # Prompt 标识
    feature = Column(String(50), nullable=False, comment='功能模块: copilot/script_gen/swagger_gen/dedup/其他')
    name = Column(String(255), nullable=False, comment='版本名称/标签（如 v1、baseline、experiment-A）')

    # 版本管理
    version = Column(Integer, nullable=False, default=1, comment='版本号（同一 feature 下递增）')
    is_active = Column(Boolean, default=False, comment='是否为当前激活版本（每个 feature 可有多个激活版本用于 A/B 测试）')

    # Prompt 内容
    system_prompt = Column(Text, nullable=False, comment='系统提示词内容')
    user_prompt_template = Column(Text, comment='用户提示词模板（支持 {variable} 占位符）')
    temperature = Column(Float, default=0.3, comment='默认 temperature 参数')
    model_name = Column(String(100), comment='指定使用的模型（为空则使用全局默认）')

    # 效果统计（基于 AIInvocationLog 自动聚合）
    total_invocations = Column(Integer, default=0, comment='总调用次数')
    success_count = Column(Integer, default=0, comment='成功调用次数')
    failure_count = Column(Integer, default=0, comment='失败调用次数')
    avg_latency_ms = Column(Float, default=0.0, comment='平均延迟（毫秒）')
    avg_tokens = Column(Float, default=0.0, comment='平均 token 用量')
    avg_cost = Column(Float, default=0.0, comment='平均调用成本（美元）')

    # A/B 测试
    traffic_weight = Column(Float, default=1.0, comment='流量权重（0.0-1.0，多版本 A/B 时按权重分配）')

    # 变更追踪
    change_notes = Column(Text, comment='变更说明')
    created_by = Column(Integer, ForeignKey('users.id'), nullable=True, comment='创建者用户 ID')

    # 时间戳
    created_at = Column(DateTime, default=datetime.utcnow, comment='创建时间')
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, comment='更新时间')
    deactivated_at = Column(DateTime, comment='停用时间')

    # 关联关系
    creator = relationship('User', backref='prompt_versions')

    def to_dict(self):
        return {
            'id': self.id,
            'feature': self.feature,
            'name': self.name,
            'version': self.version,
            'is_active': self.is_active,
            'system_prompt': self.system_prompt,
            'user_prompt_template': self.user_prompt_template,
            'temperature': self.temperature,
            'model_name': self.model_name,
            'total_invocations': self.total_invocations,
            'success_count': self.success_count,
            'failure_count': self.failure_count,
            'success_rate': round(self.success_count / self.total_invocations * 100, 2) if self.total_invocations > 0 else 0.0,
            'avg_latency_ms': self.avg_latency_ms,
            'avg_tokens': self.avg_tokens,
            'avg_cost': self.avg_cost,
            'traffic_weight': self.traffic_weight,
            'change_notes': self.change_notes,
            'created_by': self.created_by,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'deactivated_at': self.deactivated_at.isoformat() if self.deactivated_at else None,
        }

    def __repr__(self):
        return f'<PromptVersion feature={self.feature} name={self.name} v{self.version}>'
