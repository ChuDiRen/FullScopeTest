"""
多租户组织模型

存储组织信息和成员关系
"""

import secrets
from datetime import datetime
from sqlalchemy import func
from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship
from ..database import Base
from sqlalchemy import select
from ..extensions import db


class Organization(Base):
    """组织表"""

    __tablename__ = 'organizations'
    __table_args__ = (
        Index('idx_organizations_owner_id', 'owner_id'),
        Index('idx_organizations_invite_code', 'invite_code'),
    )

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, comment='组织名称')
    slug = Column(String(100), unique=True, nullable=False, comment='组织 slug（URL 友好）')
    description = Column(Text, comment='组织描述')
    owner_id = Column(Integer, ForeignKey('users.id'), nullable=False, comment='创建者 ID')
    avatar = Column(String(500), comment='组织头像 URL')
    invite_code = Column(String(20), unique=True, nullable=True, comment='邀请码')
    settings = Column(JSON, default=dict, comment='组织设置')
    is_active = Column(Boolean, default=True, comment='是否激活')
    created_at = Column(DateTime, default=datetime.utcnow, comment='创建时间')
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, comment='更新时间')

    # 关联关系
    owner = relationship('User', backref='owned_organizations')
    members = relationship('OrganizationMember', backref='organization', lazy='dynamic', cascade='all, delete-orphan')
    projects = relationship('Project', backref='organization', lazy='dynamic')

    def generate_invite_code(self):
        """生成 8 位邀请码"""
        self.invite_code = secrets.token_urlsafe(6).upper()[:8]
        return self.invite_code

    def to_dict(self):
        # 使用子查询计算关联数量，避免 N+1 查询
        member_count = db.session.scalar(
            select(func.count(OrganizationMember.id)).filter(
                OrganizationMember.organization_id == self.id,
                OrganizationMember.is_active == True)
        ) or 0

        from .project import Project
        project_count = db.session.scalar(
            select(func.count(Project.id)).filter(Project.organization_id == self.id)
        ) or 0

        return {
            'id': self.id,
            'name': self.name,
            'slug': self.slug,
            'description': self.description,
            'owner_id': self.owner_id,
            'avatar': self.avatar,
            'invite_code': self.invite_code,
            'settings': self.settings,
            'is_active': self.is_active,
            'member_count': member_count,
            'project_count': project_count,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self):
        return f'<Organization {self.name}>'


class OrganizationMember(Base):
    """组织成员关系表"""

    __tablename__ = 'organization_members'
    __table_args__ = (
        UniqueConstraint('organization_id', 'user_id', name='uq_org_member'),
        Index('idx_org_members_org_id', 'organization_id'),
        Index('idx_org_members_user_id', 'user_id'),
    )

    id = Column(Integer, primary_key=True)
    organization_id = Column(Integer, ForeignKey('organizations.id'), nullable=False, comment='组织 ID')
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, comment='用户 ID')
    role = Column(String(20), default='member', comment='角色: owner/admin/member/viewer')
    invited_by = Column(Integer, ForeignKey('users.id'), nullable=True, comment='邀请人 ID')
    is_active = Column(Boolean, default=True, comment='是否激活')
    created_at = Column(DateTime, default=datetime.utcnow, comment='加入时间')
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, comment='更新时间')

    # 关联关系
    user = relationship('User', foreign_keys=[user_id], backref='organization_memberships')
    inviter = relationship('User', foreign_keys=[invited_by], backref='invited_members')

    def to_dict(self):
        return {
            'id': self.id,
            'organization_id': self.organization_id,
            'user_id': self.user_id,
            'role': self.role,
            'invited_by': self.invited_by,
            'is_active': self.is_active,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None,
        }

    def get_effective_role_name(self) -> str:
        """
        获取有效的 RBAC 角色名

        将旧角色名（owner/member）映射到新 RBAC 角色名。
        """
        from .role import LEGACY_ROLE_MAPPING
        return LEGACY_ROLE_MAPPING.get(self.role, self.role)

    def has_permission(self, resource: str, action: str) -> bool:
        """
        检查成员是否拥有指定权限

        优先使用 Role 表中的自定义权限，回退到系统角色映射。
        """
        effective_role = self.get_effective_role_name()
        from .role import get_effective_permissions
        permissions = get_effective_permissions(effective_role)
        allowed_actions = permissions.get(resource, [])
        return action in allowed_actions

    def get_permissions(self) -> dict:
        """获取成员的完整权限配置"""
        effective_role = self.get_effective_role_name()
        from .role import get_effective_permissions
        return get_effective_permissions(effective_role)

    def __repr__(self):
        return f'<OrganizationMember org={self.organization_id} user={self.user_id} role={self.role}>'
