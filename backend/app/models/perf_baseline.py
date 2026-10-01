"""
性能测试基线模型

存储性能测试基线数据，用于退化检测。
"""

from datetime import datetime, timezone
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, JSON, String
from ..database import Base


class PerfBaseline(Base):
    """性能测试基线表"""

    __tablename__ = "perf_baselines"
    __table_args__ = (
        Index("idx_perf_baselines_scenario", "scenario_id"),
        Index("idx_perf_baselines_active", "is_active"),
    )

    id = Column(Integer, primary_key=True)
    scenario_id = Column(Integer, nullable=False, comment="场景 ID")
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, comment="创建者")
    name = Column(String(200), nullable=False, comment="基线名称")
    is_active = Column(Boolean, default=True, comment="是否为当前活跃基线")
    metrics = Column(JSON, nullable=False, comment="基线指标 {p50, p90, p95, p99, avg, throughput, error_rate}")
    run_id = Column(Integer, comment="关联的测试运行 ID")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None))

    def to_dict(self):
        return {
            "id": self.id,
            "scenario_id": self.scenario_id,
            "name": self.name,
            "is_active": self.is_active,
            "metrics": self.metrics,
            "run_id": self.run_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

