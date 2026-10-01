"""下线计费管理功能：drop billing_plans / subscriptions / usage_records 表

功能为空壳（无可用套餐、无真实支付渠道对接），随前端入口一并移除：
后端 billing 路由/服务/模型删除后，此处清理存量表结构。
列定义与被删除的 models/billing_plan.py、models/subscription.py 逐列一致，
downgrade 可精确重建表结构（数据本身不可恢复）。

Revision ID: 007_drop_billing
Revises: 006_drop_visitor_stats
Create Date: 2026-10-01

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '007_drop_billing'
down_revision = '006_drop_visitor_stats'
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table('usage_records')
    op.drop_table('subscriptions')
    op.drop_table('billing_plans')


def downgrade():
    op.create_table(
        'billing_plans',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=50), nullable=False, comment='套餐名称：free/pro/enterprise'),
        sa.Column('display_name', sa.String(length=100), nullable=False, comment='显示名称'),
        sa.Column('description', sa.Text(), nullable=True, comment='套餐描述'),
        sa.Column('price_monthly', sa.Float(), nullable=True, comment='月价格（元）'),
        sa.Column('price_yearly', sa.Float(), nullable=True, comment='年价格（元）'),
        sa.Column('currency', sa.String(length=10), nullable=True, comment='货币单位'),
        sa.Column('max_projects', sa.Integer(), nullable=True, comment='最大项目数'),
        sa.Column('max_test_cases', sa.Integer(), nullable=True, comment='最大用例数'),
        sa.Column('max_parallel_executions', sa.Integer(), nullable=True, comment='最大并行执行数'),
        sa.Column('max_ai_calls_monthly', sa.Integer(), nullable=True, comment='每月 AI 调用次数'),
        sa.Column('max_members', sa.Integer(), nullable=True, comment='最大成员数'),
        sa.Column('max_storage_mb', sa.Integer(), nullable=True, comment='最大存储空间（MB）'),
        sa.Column('features', sa.JSON(), nullable=True, comment='功能开关 {feature: enabled}'),
        sa.Column('is_active', sa.Boolean(), nullable=True, comment='是否可用'),
        sa.Column('sort_order', sa.Integer(), nullable=True, comment='排序'),
        sa.Column('created_at', sa.DateTime(), nullable=True, comment='创建时间'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )
    op.create_table(
        'subscriptions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('organization_id', sa.Integer(), nullable=False),
        sa.Column('plan_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=True, comment='状态：active/cancelled/expired/paused'),
        sa.Column('billing_cycle', sa.String(length=10), nullable=True, comment='计费周期：monthly/yearly'),
        sa.Column('started_at', sa.DateTime(), nullable=True, comment='开始时间'),
        sa.Column('current_period_start', sa.DateTime(), nullable=True, comment='当前计费周期开始'),
        sa.Column('current_period_end', sa.DateTime(), nullable=True, comment='当前计费周期结束'),
        sa.Column('cancelled_at', sa.DateTime(), nullable=True, comment='取消时间'),
        sa.Column('payment_method', sa.String(length=50), nullable=True, comment='支付方式：stripe/alipay/wechat'),
        sa.Column('external_subscription_id', sa.String(length=255), nullable=True, comment='外部订阅 ID（Stripe 等）'),
        sa.Column('created_at', sa.DateTime(), nullable=True, comment='创建时间'),
        sa.Column('updated_at', sa.DateTime(), nullable=True, comment='更新时间'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id']),
        sa.ForeignKeyConstraint(['plan_id'], ['billing_plans.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'usage_records',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('organization_id', sa.Integer(), nullable=False),
        sa.Column('year', sa.Integer(), nullable=False, comment='年份'),
        sa.Column('month', sa.Integer(), nullable=False, comment='月份'),
        sa.Column('projects_count', sa.Integer(), nullable=True, comment='项目数'),
        sa.Column('test_cases_count', sa.Integer(), nullable=True, comment='用例数'),
        sa.Column('ai_calls_count', sa.Integer(), nullable=True, comment='AI 调用次数'),
        sa.Column('storage_used_mb', sa.Float(), nullable=True, comment='已用存储（MB）'),
        sa.Column('members_count', sa.Integer(), nullable=True, comment='成员数'),
        sa.Column('created_at', sa.DateTime(), nullable=True, comment='创建时间'),
        sa.Column('updated_at', sa.DateTime(), nullable=True, comment='更新时间'),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('organization_id', 'year', 'month', name='uq_usage_org_period'),
    )
