"""
权限控制模块

提供角色和权限检查的装饰器和工具函数（零 Flask）。

身份来源：原 flask_jwt_extended 依赖全局 request 的 Authorization 头；
零 Flask 下由 ASGI 鉴权层把已验证的 JWT identity（sub，字符串用户 ID）
写入 request_local 槽的 ``jwt_identity`` 键，此处只做读取。
"""

from functools import wraps
from ..core.request_local import get_request_info
from ..models.user import User, ROLE_PERMISSIONS
from .response import error_response
from sqlalchemy import select
from ..extensions import db


def get_jwt_identity():
    """获取当前已验证的 JWT identity（未认证返回 None）"""
    return (get_request_info() or {}).get('jwt_identity')


def get_current_user() -> User:
    """获取当前用户对象"""
    identity = get_jwt_identity()
    if not identity:
        return None
    return db.session.get(User, int(identity))


def require_role(*roles):
    """
    角色检查装饰器

    用法:
        @require_role('admin', 'member')
        def my_endpoint():
            ...
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            user = get_current_user()

            if not user:
                return error_response(401, '未认证')

            if not user.is_active:
                return error_response(403, '账号已被禁用')

            if user.role not in roles:
                return error_response(403, f'需要角色: {", ".join(roles)}')

            return f(*args, **kwargs)
        return decorated_function
    return decorator


def require_permission(*permissions):
    """
    权限检查装饰器

    用法:
        @require_permission('write', 'delete')
        def my_endpoint():
            ...
    """
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            user = get_current_user()

            if not user:
                return error_response(401, '未认证')

            if not user.is_active:
                return error_response(403, '账号已被禁用')

            user_permissions = ROLE_PERMISSIONS.get(user.role, [])
            for perm in permissions:
                if perm not in user_permissions:
                    return error_response(403, f'缺少权限: {perm}')

            return f(*args, **kwargs)
        return decorated_function
    return decorator


def require_admin(f):
    """
    管理员检查装饰器

    用法:
        @require_admin
        def my_endpoint():
            ...
    """
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user = get_current_user()

        if not user:
            return error_response(401, '未认证')

        if not user.is_active:
            return error_response(403, '账号已被禁用')

        if not user.is_admin():
            return error_response(403, '需要管理员权限')

        return f(*args, **kwargs)
    return decorated_function


def check_project_permission(user_id: int, project_id: int) -> bool:
    """
    检查用户是否有项目访问权限

    Args:
        user_id: 用户 ID
        project_id: 项目 ID

    Returns:
        bool: 是否有权限
    """
    from ..models.project import Project

    # 管理员可以访问所有项目
    user = db.session.get(User, user_id)
    if user and user.is_admin():
        return True

    # 检查项目所有权
    project = db.session.get(Project, project_id)
    if not project:
        return False

    return project.user_id == user_id
