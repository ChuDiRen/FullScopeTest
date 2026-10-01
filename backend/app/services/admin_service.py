"""
管理员后台服务

提供平台运营视角，监控整体使用情况。
仅 super_admin 角色可访问。
"""

from datetime import datetime, timezone, timedelta
from typing import Dict, Any, Optional
from ..extensions import db
from ..models.user import User
from ..models.organization import Organization, OrganizationMember
from ..models.project import Project
from ..models.test_run import TestRun
from ..core.logging import get_logger
from sqlalchemy import select
from sqlalchemy import func

logger = get_logger(__name__)


class AdminService:
    """管理员后台服务"""

    def get_platform_overview(self) -> Dict[str, Any]:
        """获取平台概览"""
        total_users = db.session.scalar(select(func.count()).select_from(select(User).subquery()))
        active_users = db.session.scalar(select(func.count()).select_from(select(User).filter_by(is_active=True).subquery()))
        total_orgs = db.session.scalar(select(func.count()).select_from(select(Organization).subquery()))
        total_projects = db.session.scalar(select(func.count()).select_from(select(Project).subquery()))

        # 最近 24 小时的执行量
        since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)
        daily_runs = db.session.scalar(select(func.count()).select_from(select(TestRun).filter(TestRun.created_at >= since).subquery()))

        return {
            "total_users": total_users,
            "active_users": active_users,
            "total_organizations": total_orgs,
            "total_projects": total_projects,
            "daily_test_runs": daily_runs,
        }

    def get_tenant_list(self) -> list:
        """获取租户列表"""
        orgs = db.session.scalars(select(Organization)).all()
        result = []
        for org in orgs:
            member_count = db.session.scalar(select(func.count()).select_from(select(OrganizationMember).filter_by(
                organization_id=org.id, is_active=True,
            ).subquery()))
            project_count = db.session.scalar(select(func.count()).select_from(select(Project).filter_by(organization_id=org.id).subquery()))
            result.append({
                "id": org.id, "name": org.name,
                "member_count": member_count,
                "project_count": project_count,
                "created_at": org.created_at.isoformat() if org.created_at else None,
            })
        return result

    def get_system_health(self) -> Dict[str, Any]:
        """获取系统健康状态"""
        from ..api.v2.v1.health import _check_database, _check_redis
        return {
            "database": _check_database(),
            "redis": _check_redis(),
        }


_instance = None


def get_admin_service():
    global _instance
    if _instance is None: _instance = AdminService()
    return _instance
