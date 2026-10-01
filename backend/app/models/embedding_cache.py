"""
向量缓存模型

缓存用例内容的向量化结果，避免重复计算。
通过 content_hash 去重：相同内容只计算一次向量。
"""

from datetime import datetime
from sqlalchemy import Column, DateTime, Index, Integer, String, Text
from ..database import Base


class EmbeddingCache(Base):
    """向量缓存表"""

    __tablename__ = 'embedding_cache'
    __table_args__ = (
        Index('idx_emb_cache_hash', 'content_hash'),
        Index('idx_emb_cache_feature', 'feature'),
    )

    id = Column(Integer, primary_key=True)
    content_hash = Column(String(64), nullable=False, unique=True, comment='内容 SHA256 哈希')
    feature = Column(String(50), nullable=False, default='test_case', comment='功能标识')
    model_name = Column(String(100), nullable=False, comment='嵌入模型名称')
    embedding = Column(Text, nullable=False, comment='向量 JSON 序列化')
    content_preview = Column(Text, comment='内容预览（前 200 字）')
    created_at = Column(DateTime, default=datetime.utcnow, comment='创建时间')

    def to_dict(self):
        return {
            'id': self.id,
            'content_hash': self.content_hash,
            'feature': self.feature,
            'model_name': self.model_name,
            'content_preview': self.content_preview,
            'created_at': self.created_at.isoformat() if self.created_at else None,
        }
