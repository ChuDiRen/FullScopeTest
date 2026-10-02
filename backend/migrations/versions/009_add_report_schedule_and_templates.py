"""报告调度/报告模板/用例模板三张表 + 内置用例模板种子

Revision ID: 009_report_schedule_templates
Revises: 008_add_perf_grpc_fields
Create Date: 2026-10-02
"""
from alembic import op
import sqlalchemy as sa

revision = '009_report_schedule_templates'
down_revision = '008_add_perf_grpc_fields'
branch_labels = None
depends_on = None

BUILTIN_TEMPLATES = [
    ('CRUD - Create', '标准创建资源模板', 'CRUD', 'POST', '{{base_url}}/api/resource',
     '{"Content-Type": "application/json"}', '{"name": "test"}', 'status=201, body.data.id exists'),
    ('CRUD - Read', '标准查询资源模板', 'CRUD', 'GET', '{{base_url}}/api/resource/{{id}}',
     '{}', '', 'status=200, body.data.id equals {{id}}'),
    ('Auth - Login', '用户登录认证模板', '认证', 'POST', '{{base_url}}/api/auth/login',
     '{"Content-Type": "application/json"}', '{"username": "{{username}}", "password": "{{password}}"}',
     'status=200, body.access_token exists'),
    ('Pagination', '列表分页查询模板', 'CRUD', 'GET', '{{base_url}}/api/resource?page=1&per_page=10',
     '{}', '', 'status=200, body.data is array, body.total >= 0'),
    ('Health Check', '服务健康检查模板', '监控', 'GET', '{{base_url}}/health',
     '{}', '', 'status=200, response_time < 1000'),
]


def upgrade():
    op.create_table(
        'report_schedules',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('project_id', sa.Integer(), sa.ForeignKey('projects.id'), nullable=True),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('frequency', sa.String(length=20), server_default='daily'),
        sa.Column('recipients', sa.JSON(), nullable=True),
        sa.Column('webhook_url', sa.String(length=500), server_default=''),
        sa.Column('is_active', sa.Boolean(), server_default=sa.true()),
        sa.Column('last_run_at', sa.DateTime(), nullable=True),
        sa.Column('last_result', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )
    op.create_table(
        'report_templates',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('modules', sa.JSON(), nullable=True),
        sa.Column('theme', sa.String(length=30), server_default='default'),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )
    op.create_table(
        'test_case_templates',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('name', sa.String(length=255), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('category', sa.String(length=50), server_default='通用'),
        sa.Column('method', sa.String(length=10), server_default='GET'),
        sa.Column('endpoint', sa.String(length=500), server_default=''),
        sa.Column('headers', sa.Text(), server_default='{}'),
        sa.Column('body', sa.Text(), server_default=''),
        sa.Column('assertions', sa.String(length=500), server_default=''),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )
    # 内置用例模板种子（user_id NULL = 系统内置）
    test_case_templates = sa.table(
        'test_case_templates',
        sa.column('name'), sa.column('description'), sa.column('category'),
        sa.column('method'), sa.column('endpoint'), sa.column('headers'),
        sa.column('body'), sa.column('assertions'),
    )
    op.bulk_insert(test_case_templates, [
        {'name': n, 'description': d, 'category': c, 'method': m,
         'endpoint': e, 'headers': h, 'body': b, 'assertions': a}
        for n, d, c, m, e, h, b, a in BUILTIN_TEMPLATES
    ])


def downgrade():
    op.drop_table('test_case_templates')
    op.drop_table('report_templates')
    op.drop_table('report_schedules')
