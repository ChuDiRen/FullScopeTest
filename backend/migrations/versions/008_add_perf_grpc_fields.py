"""性能测试场景支持 gRPC (Dubbo3 Triple) 压测

Revision ID: 008_add_perf_grpc_fields
Revises: 007_drop_billing
Create Date: 2026-10-02
"""
from alembic import op
import sqlalchemy as sa

revision = '008_add_perf_grpc_fields'
down_revision = '007_drop_billing'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('perf_test_scenarios', sa.Column('protocol', sa.String(length=20), nullable=True, server_default='http', comment='压测协议: http / grpc'))
    op.add_column('perf_test_scenarios', sa.Column('proto_content', sa.Text(), nullable=True, comment='.proto 定义源码'))
    op.add_column('perf_test_scenarios', sa.Column('grpc_method', sa.String(length=255), nullable=True, comment='RPC 全名 package.Service/Method'))
    op.add_column('perf_test_scenarios', sa.Column('grpc_request_json', sa.Text(), nullable=True, comment='请求消息 JSON 模板'))
    op.add_column('perf_test_scenarios', sa.Column('grpc_descriptor', sa.Text(), nullable=True, comment='FileDescriptorSet base64'))


def downgrade():
    op.drop_column('perf_test_scenarios', 'grpc_descriptor')
    op.drop_column('perf_test_scenarios', 'grpc_request_json')
    op.drop_column('perf_test_scenarios', 'grpc_method')
    op.drop_column('perf_test_scenarios', 'proto_content')
    op.drop_column('perf_test_scenarios', 'protocol')
