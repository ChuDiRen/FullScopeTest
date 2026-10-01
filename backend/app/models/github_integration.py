"""
GitHub 集成模型

存储 GitHub App OAuth 认证信息和用户绑定关系
"""

from datetime import datetime, timezone
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import relationship
from ..database import Base


class GitHubIntegration(Base):
    """GitHub 集成表 - 存储用户 GitHub OAuth 绑定信息"""

    __tablename__ = 'github_integrations'
    __table_args__ = (
        Index('idx_github_integrations_user_id', 'user_id'),
        Index('idx_github_integrations_github_user_id', 'github_user_id'),
    )

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, comment='本地用户 ID')

    # GitHub OAuth 信息
    github_user_id = Column(String(50), nullable=False, comment='GitHub 用户 ID')
    github_username = Column(String(100), nullable=False, comment='GitHub 用户名')
    github_email = Column(String(200), comment='GitHub 邮箱')
    github_avatar = Column(String(500), comment='GitHub 头像 URL')

    # Token 信息（加密存储）
    access_token_encrypted = Column(Text, nullable=False, comment='加密的 Access Token')
    token_type = Column(String(50), default='bearer', comment='Token 类型')
    scope = Column(String(500), comment='授权的 scope')
    token_expires_at = Column(DateTime, comment='Token 过期时间（可为 null 表示不过期）')

    # Refresh Token（可选）
    refresh_token_encrypted = Column(Text, comment='加密的 Refresh Token')
    refresh_token_expires_at = Column(DateTime, comment='Refresh Token 过期时间')

    # 状态
    is_active = Column(Boolean, default=True, comment='是否激活')

    # 时间戳
    created_at = Column(DateTime, default=datetime.utcnow, comment='创建时间')
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, comment='更新时间')
    last_used_at = Column(DateTime, comment='最后使用时间')

    # 关联关系
    user = relationship('User', backref='github_integrations')

    def to_dict(self):
        """转换为字典（不包含敏感 Token 信息）"""
        return {
            'id': self.id,
            'user_id': self.user_id,
            'github_user_id': self.github_user_id,
            'github_username': self.github_username,
            'github_email': self.github_email,
            'github_avatar': self.github_avatar,
            'scope': self.scope,
            'token_expires_at': self.token_expires_at.isoformat() if self.token_expires_at else None,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
            'last_used_at': self.last_used_at.isoformat() if self.last_used_at else None,
            'token_valid': self._is_token_valid(),
        }

    def _is_token_valid(self) -> bool:
        """检查 Token 是否仍然有效"""
        if not self.is_active:
            return False
        if self.token_expires_at and self.token_expires_at < datetime.now(timezone.utc).replace(tzinfo=None):
            return False
        return True

    def __repr__(self):
        return f'<GitHubIntegration {self.github_username} for user {self.user_id}>'
