"""
密码重置功能集成测试

测试完整的密码重置流程：
1. 请求重置 → 获取 token
2. 使用 token 重置密码
3. 用旧密码登录失败
4. 用新密码登录成功
"""

import pytest
from unittest.mock import patch


def _request_reset_token(client, email):
    """请求密码重置并从模拟邮件调用中捕获 token（响应不回显 token，防日志泄露）"""
    with patch('app.services.email_service.email_service.send_password_reset_email') as mock_send:
        resp = client.post('/api/v1/auth/forgot-password', json={'email': email})
        assert resp.status_code == 200
        assert mock_send.called, '未调用邮件服务发送重置邮件'
        return mock_send.call_args.kwargs['reset_token']




@pytest.fixture()
def registered_user(client):
    """注册一个测试用户"""
    client.post('/api/v1/auth/register', json={
        'username': 'reset_test_user',
        'email': 'reset@test.com',
        'password': 'OldPass@123',
    })
    return {'username': 'reset_test_user', 'email': 'reset@test.com', 'password': 'OldPass@123'}


class TestPasswordReset:
    """密码重置测试"""

    def test_forgot_password_returns_success(self, client, registered_user):
        """忘记密码接口返回成功消息"""
        resp = client.post('/api/v1/auth/forgot-password', json={
            'email': registered_user['email'],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data['message'] == '如果该邮箱已注册，重置链接已发送'

    def test_forgot_password_nonexistent_email(self, client):
        """不存在的邮箱也返回成功（防止邮箱枚举）"""
        resp = client.post('/api/v1/auth/forgot-password', json={
            'email': 'nonexistent@test.com',
        })
        assert resp.status_code == 200

    def test_reset_password_success(self, client, registered_user):
        """使用有效 token 重置密码成功"""
        # 获取 token
        token = _request_reset_token(client, registered_user['email'])

        # 重置密码
        resp = client.post('/api/v1/auth/reset-password', json={
            'token': token,
            'new_password': 'NewPass@456',
        })
        assert resp.status_code == 200

        # 用旧密码登录失败
        resp = client.post('/api/v1/auth/login', json={
            'username': registered_user['username'],
            'password': registered_user['password'],
        })
        assert resp.status_code == 401

        # 用新密码登录成功
        resp = client.post('/api/v1/auth/login', json={
            'username': registered_user['username'],
            'password': 'NewPass@456',
        })
        assert resp.status_code == 200

    def test_reset_password_invalid_token(self, client):
        """使用无效 token 重置密码失败"""
        resp = client.post('/api/v1/auth/reset-password', json={
            'token': 'invalid_token_123',
            'new_password': 'NewPass@456',
        })
        assert resp.status_code == 400

    def test_reset_password_weak_password(self, client, registered_user):
        """使用弱密码重置失败"""
        token = _request_reset_token(client, registered_user['email'])

        resp = client.post('/api/v1/auth/reset-password', json={
            'token': token,
            'new_password': 'weak',
        })
        assert resp.status_code == 400

    def test_token_single_use(self, client, registered_user):
        """重置 token 只能使用一次"""
        token = _request_reset_token(client, registered_user['email'])

        # 第一次使用成功
        resp = client.post('/api/v1/auth/reset-password', json={
            'token': token,
            'new_password': 'NewPass@456',
        })
        assert resp.status_code == 200

        # 第二次使用失败
        resp = client.post('/api/v1/auth/reset-password', json={
            'token': token,
            'new_password': 'AnotherPass@789',
        })
        assert resp.status_code == 400
