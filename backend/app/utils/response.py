"""
统一响应格式工具

提供标准化的 API 响应格式（零 Flask）：
返回 (payload, status_code) 元组；payload 为可直接 JSON 序列化的 dict，
由具体框架层（FastAPI 路由/JSONResponse）负责序列化与状态码。
"""

from datetime import datetime, timezone

from ..core.runtime import ctx


def success_response(data=None, message='success', code=200):
    """
    成功响应
    
    Args:
        data: 响应数据
        message: 响应消息
        code: HTTP 状态码
    
    Returns:
        tuple: (响应体 dict, 状态码)
    """
    response = {
        'code': code,
        'message': message,
        'data': data,
        'timestamp': datetime.now(timezone.utc).isoformat() + 'Z'
    }
    return response, code


def error_response(code, message, errors=None):
    """
    错误响应

    Args:
        code: HTTP 状态码
        message: 错误消息
        errors: 详细错误信息

    Returns:
        tuple: (响应体 dict, 状态码)
    """
    request_id = ctx.get_request_id()
    response = {
        'code': code,
        'message': message,
        'errors': errors,
        'request_id': request_id,
        'timestamp': datetime.now(timezone.utc).isoformat() + 'Z'
    }
    return response, code


def paginate_response(items, total, page, per_page, message='success'):
    """
    分页响应
    
    Args:
        items: 数据列表
        total: 总数量
        page: 当前页码
        per_page: 每页数量
        message: 响应消息
    
    Returns:
        tuple: (响应体 dict, 状态码)
    """
    response = {
        'code': 200,
        'message': message,
        'data': {
            'items': items,
            'pagination': {
                'total': total,
                'page': page,
                'per_page': per_page,
                'pages': (total + per_page - 1) // per_page
            }
        },
        'timestamp': datetime.now(timezone.utc).isoformat() + 'Z'
    }
    return response, 200
