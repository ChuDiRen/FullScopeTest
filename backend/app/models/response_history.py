"""
响应历史模型

记录每次 API 请求的响应信息，用于历史趋势分析。
"""

from datetime import datetime, timezone
from sqlalchemy import Column, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text
from sqlalchemy.orm import backref, relationship
from ..database import Base


class ResponseHistory(Base):
    """响应历史记录表"""

    __tablename__ = "response_histories"
    __table_args__ = (
        Index("idx_response_hist_case_id", "case_id"),
        Index("idx_response_hist_created", "created_at"),
    )

    id = Column(Integer, primary_key=True)
    case_id = Column(Integer, ForeignKey("api_test_cases.id"), nullable=True, comment="关联用例 ID")
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, comment="用户 ID")
    url = Column(String(500), nullable=False, comment="请求 URL")
    method = Column(String(10), nullable=False, default="GET", comment="HTTP 方法")
    status_code = Column(Integer, comment="响应状态码")
    response_time = Column(Float, comment="响应时间（毫秒）")
    response_size = Column(String(20), comment="响应大小")
    request_headers = Column(JSON, comment="请求头快照")
    request_body = Column(JSON, comment="请求体快照")
    response_headers = Column(JSON, comment="响应头快照")
    response_body = Column(JSON, comment="响应体快照")
    error = Column(Text, comment="错误信息")
    environment_id = Column(Integer, ForeignKey("environments.id"), nullable=True, comment="使用的环境")
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None), comment="创建时间")

    # 关联
    case = relationship("ApiTestCase", backref=backref("response_histories", lazy="dynamic"))

    def to_dict(self):
        return {
            "id": self.id,
            "case_id": self.case_id,
            "url": self.url,
            "method": self.method,
            "status_code": self.status_code,
            "response_time": self.response_time,
            "response_size": self.response_size,
            "error": self.error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def to_detail_dict(self):
        result = self.to_dict()
        result["request_headers"] = self.request_headers
        result["request_body"] = self.request_body
        result["response_headers"] = self.response_headers
        result["response_body"] = self.response_body
        return result

