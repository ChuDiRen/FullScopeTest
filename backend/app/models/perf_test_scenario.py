"""
性能测试场景模型

存储 Locust 性能测试配置
"""

from datetime import datetime
from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text
from ..database import Base


class PerfTestScenario(Base):
    """性能测试场景表"""

    __tablename__ = 'perf_test_scenarios'
    __table_args__ = (
        Index('idx_perf_test_scenarios_project_id', 'project_id'),
        Index('idx_perf_test_scenarios_user_id', 'user_id'),
        Index('idx_perf_test_scenarios_status', 'status'),
    )

    id = Column(Integer, primary_key=True)
    project_id = Column(Integer, ForeignKey('projects.id'), nullable=True, comment='项目 ID')
    user_id = Column(Integer, ForeignKey('users.id'), nullable=False, comment='用户 ID')
    name = Column(String(255), nullable=False, comment='场景名称')
    description = Column(Text, comment='场景描述')
    
    # 请求配置
    target_url = Column(String(500), nullable=False, comment='目标 URL')
    protocol = Column(String(20), default='http', comment='压测协议: http / grpc（Dubbo3 Triple 兼容）')
    method = Column(String(10), default='GET', comment='HTTP 方法')
    headers = Column(JSON, default=dict, comment='请求头')
    body = Column(JSON, comment='请求体')
    script_content = Column(Text, comment='Locust 脚本内容')

    # gRPC (Dubbo3 Triple) 配置
    proto_content = Column(Text, comment='.proto 定义源码（protocol=grpc 时必填）')
    grpc_method = Column(String(255), comment='RPC 全名，格式 package.Service/Method')
    grpc_request_json = Column(Text, comment='请求消息 JSON 模板')
    grpc_descriptor = Column(Text, comment='编译后的 FileDescriptorSet（base64），运行时免 protoc')
    
    # 负载配置
    user_count = Column(Integer, default=10, comment='并发用户数')
    spawn_rate = Column(Integer, default=1, comment='用户生成速率')
    duration = Column(Integer, default=60, comment='持续时间（秒）')
    ramp_up = Column(Integer, default=0, comment='爬坡时间（秒）')
    
    # 阶梯加压配置
    step_load_enabled = Column(Boolean, default=False, comment='是否启用阶梯加压')
    step_users = Column(Integer, default=10, comment='每步增加用户数')
    step_duration = Column(Integer, default=30, comment='每步持续时间')
    
    # 状态信息
    status = Column(String(20), default='pending', comment='当前状态: pending/running/completed/failed/stopped')
    last_run_at = Column(DateTime, comment='最后运行时间')
    last_result = Column(JSON, comment='最后执行结果')
    
    # 结果统计
    avg_response_time = Column(Float, comment='平均响应时间 (ms)')
    max_response_time = Column(Float, comment='最大响应时间 (ms)')
    min_response_time = Column(Float, comment='最小响应时间 (ms)')
    throughput = Column(Float, comment='吞吐量 (req/s)')
    error_rate = Column(Float, comment='错误率 (%)')
    
    # 其他配置
    tags = Column(JSON, default=list, comment='标签')
    is_enabled = Column(Boolean, default=True, comment='是否启用')
    
    created_at = Column(DateTime, default=datetime.utcnow, comment='创建时间')
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, comment='更新时间')
    
    def to_dict(self):
        """转换为字典"""
        return {
            'id': self.id,
            'project_id': self.project_id,
            'user_id': self.user_id,
            'name': self.name,
            'description': self.description,
            'target_url': self.target_url,
            'protocol': self.protocol or 'http',
            'method': self.method,
            'headers': self.headers,
            'body': self.body,
            'script_content': self.script_content,
            'proto_content': self.proto_content,
            'grpc_method': self.grpc_method,
            'grpc_request_json': self.grpc_request_json,
            'user_count': self.user_count,
            'spawn_rate': self.spawn_rate,
            'duration': self.duration,
            'ramp_up': self.ramp_up,
            'step_load_enabled': self.step_load_enabled,
            'step_users': self.step_users,
            'step_duration': self.step_duration,
            'status': self.status,
            'last_run_at': self.last_run_at.isoformat() if self.last_run_at else None,
            'last_result': self.last_result,
            'avg_response_time': self.avg_response_time,
            'max_response_time': self.max_response_time,
            'min_response_time': self.min_response_time,
            'throughput': self.throughput,
            'error_rate': self.error_rate,
            'tags': self.tags,
            'is_enabled': self.is_enabled,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'updated_at': self.updated_at.isoformat() if self.updated_at else None
        }
    
    def __repr__(self):
        return f'<PerfTestScenario {self.name}>'
