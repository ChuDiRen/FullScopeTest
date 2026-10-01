"""
团队效能度量服务

提供团队维度的效能分析指标：
- 人均用例数：每个成员创建的测试用例数量
- 用例编写效率：每周新增用例数
- 缺陷发现率：失败用例占总用例的比例
- 回归效率：回归测试的通过率和执行速度
"""
from datetime import datetime, timezone, timedelta
from sqlalchemy import func as sa_func
from ..extensions import db
from ..models.api_test_case import ApiTestCase
from ..models.test_run import TestRun
from ..models.issue_link import IssueLink
from ..models.user import User
from ..core.logging import get_logger
from sqlalchemy import select

logger = get_logger(__name__)


def get_team_metrics(project_id: int = None, days: int = 30, organization_id: int = None) -> dict:
    """
    获取团队效能度量数据

    Args:
        project_id: 项目 ID（None 表示全组织）
        days: 统计范围天数
        organization_id: 组织 ID（提供时统计仅限该组织成员，防止跨组织泄露）

    Returns:
        {
            period_days,
            summary: {total_members, total_cases, total_runs, avg_cases_per_member},
            members: [{user_id, username, cases_created, runs_executed,
                       cases_per_week, defect_rate, regression_pass_rate}]
        }
    """
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)

    # 组织成员隔离：指定组织时只统计该组织成员
    org_member_ids = None
    if organization_id is not None:
        from ..models.organization import OrganizationMember
        org_member_ids = [
            om.user_id
            for om in db.session.scalars(
                select(OrganizationMember.user_id).filter_by(
                    organization_id=organization_id, is_active=True))
            .all()
        ]
        if not org_member_ids:
            org_member_ids = [-1]  # 空组织：不返回任何成员数据

    # 查询用例创建统计（按用户）
    case_query = select(
        ApiTestCase.user_id,
        sa_func.count(ApiTestCase.id).label('case_count'),
    ).filter(ApiTestCase.created_at >= since)
    if project_id:
        case_query = case_query.filter(ApiTestCase.project_id == project_id)
    if org_member_ids is not None:
        case_query = case_query.filter(ApiTestCase.user_id.in_(org_member_ids))
    case_stats = db.session.execute(case_query.group_by(ApiTestCase.user_id)).all()

    # 查询执行统计（按触发用户）
    run_query = select(
        TestRun.triggered_user_id,
        sa_func.count(TestRun.id).label('run_count'),
        sa_func.sum(TestRun.total_cases).label('total_cases'),
        sa_func.sum(TestRun.passed).label('total_passed'),
        sa_func.sum(TestRun.failed).label('total_failed'),
    ).filter(
        TestRun.created_at >= since,
        TestRun.triggered_user_id.isnot(None),
    )
    if project_id:
        run_query = run_query.filter(TestRun.project_id == project_id)
    if org_member_ids is not None:
        run_query = run_query.filter(TestRun.triggered_user_id.in_(org_member_ids))
    run_stats = db.session.execute(run_query.group_by(TestRun.triggered_user_id)).all()

    # 查询缺陷统计（按创建者）
    issue_query = select(
        IssueLink.user_id,
        sa_func.count(IssueLink.id).label('issue_count'),
    ).filter(IssueLink.created_at >= since)
    if project_id:
        issue_query = issue_query.filter(IssueLink.project_id == project_id)
    if org_member_ids is not None:
        issue_query = issue_query.filter(IssueLink.user_id.in_(org_member_ids))
    issue_stats = db.session.execute(
        issue_query.filter(IssueLink.user_id.isnot(None)).group_by(IssueLink.user_id)
    ).all()

    # 合并用户数据
    user_data = {}
    for row in case_stats:
        uid = row.user_id
        if uid not in user_data:
            user_data[uid] = _empty_member_data(uid)
        user_data[uid]['cases_created'] = row.case_count

    for row in run_stats:
        uid = row.triggered_user_id
        if uid not in user_data:
            user_data[uid] = _empty_member_data(uid)
        user_data[uid]['runs_executed'] = row.run_count or 0
        user_data[uid]['total_cases_run'] = row.total_cases or 0
        user_data[uid]['total_passed'] = row.total_passed or 0
        user_data[uid]['total_failed'] = row.total_failed or 0

    for row in issue_stats:
        uid = row.user_id
        if uid not in user_data:
            user_data[uid] = _empty_member_data(uid)
        user_data[uid]['issues_created'] = row.issue_count

    # 填充用户名并计算衍生指标
    weeks = max(1, days / 7)
    members = []
    for uid, data in user_data.items():
        user = db.session.get(User, uid)
        data['username'] = user.username if user else f'User #{uid}'

        # 用例编写效率（个/周）
        data['cases_per_week'] = round(data['cases_created'] / weeks, 1)

        # 缺陷发现率（失败用例 / 总执行用例）
        total_run = data['total_cases_run']
        data['defect_rate'] = round(
            data['total_failed'] / total_run * 100, 1
        ) if total_run > 0 else 0.0

        # 回归通过率
        data['regression_pass_rate'] = round(
            data['total_passed'] / total_run * 100, 1
        ) if total_run > 0 else 0.0

        members.append(data)

    # 按用例数降序排列
    members.sort(key=lambda m: m['cases_created'], reverse=True)

    # 汇总统计
    total_members = len(members)
    total_cases = sum(m['cases_created'] for m in members)
    total_runs = sum(m['runs_executed'] for m in members)
    avg_cases = round(total_cases / total_members, 1) if total_members > 0 else 0

    return {
        'period_days': days,
        'project_id': project_id,
        'summary': {
            'total_members': total_members,
            'total_cases': total_cases,
            'total_runs': total_runs,
            'avg_cases_per_member': avg_cases,
        },
        'members': members,
    }


def empty_team_metrics(days: int, project_id: int = None) -> dict:
    """空团队指标（结构与 get_team_metrics 一致，用于无可访问团队域时 fail-closed 返回）"""
    return {
        'period_days': days,
        'project_id': project_id,
        'summary': {
            'total_members': 0,
            'total_cases': 0,
            'total_runs': 0,
            'avg_cases_per_member': 0,
        },
        'members': [],
    }


def _empty_member_data(user_id: int) -> dict:
    """初始化空的成员数据"""
    return {
        'user_id': user_id,
        'username': '',
        'cases_created': 0,
        'runs_executed': 0,
        'total_cases_run': 0,
        'total_passed': 0,
        'total_failed': 0,
        'issues_created': 0,
        'cases_per_week': 0.0,
        'defect_rate': 0.0,
        'regression_pass_rate': 0.0,
    }