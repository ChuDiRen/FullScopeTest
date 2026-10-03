"""
v1 organizations / projects / admin / audit-logs 平迁路由测试
（FastAPI 实现：app/api/routes/{organizations,projects,admin,audit_logs}.py）

覆盖：
1. 未登录访问受保护端点（org/project/admin/audit 各抽端点参数化）→ 401
2. 创建项目 → 列表 → 详情（含置顶、缺字段 400）
3. 用户 B 访问/修改/删除用户 A 的项目 → 404（IDOR 修复验证）
4. 非管理员调用 admin 端点 → 403
5. 管理员正常调用 admin 端点 → 200（列表/详情/角色/状态）
6. 审计日志写入与查询（可见域过滤：B 看不到 A 的日志 → 404；导出权限）
7. 组织：创建 → 邀请成员 → 角色变更 → 移除；非成员/低权限角色被拒
8. 组织项目：创建进组织，组织成员可见，非成员 404（属主或组织成员过滤）

环境说明：本机 Redis 挂死（无超时阻塞），autouse fixture 将
app.services.token_blacklist._get_redis 与 app.services.rate_limit_service._get_redis
monkeypatch 成 lambda: None，两个服务按既有降级策略放行（黑名单/版本校验跳过）。
"""

import gc
import uuid

import pytest
from app.extensions import db

# ---------------------------------------------------------------------------
# 环境守卫
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _offline_redis(monkeypatch):
    """
    本机 Redis 挂死：替换两个服务的 Redis 获取入口为 lambda: None
    - token_blacklist：_get_redis() → None → 黑名单/版本校验走降级放行分支
    - rate_limit_service：_get_redis() → None → 滑动窗口限流放行
    """
    import app.services.rate_limit_service as rls
    import app.services.token_blacklist as tb

    monkeypatch.setattr(tb, "_get_redis", lambda: None)
    monkeypatch.setattr(rls, "_get_redis", lambda: None)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _uname(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"

def _make_audit_log(app, user_id: int, action: str, resource_type: str = "project", **extra):
    from app.models.audit_log import AuditLog
    log = AuditLog(
        user_id=user_id,
        action=action,
        resource_type=resource_type,
    )
    db.session.add(log)
    db.session.commit()
    return log.id

def _create_org(v2_client, headers, name: str) -> dict:
    resp = v2_client.post("/api/v1/organizations", headers=headers, json={"name": name})
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


def _invite(v2_client, headers, org_id: int, target_user_id: int, role: str = "tester"):
    return v2_client.post(
        f"/api/v1/organizations/{org_id}/members",
        headers=headers,
        json={"user_id": target_user_id, "role": role},
    )


# ---------------------------------------------------------------------------
# 1. 未登录 401（admin / project / audit / org 各抽端点参数化）
# ---------------------------------------------------------------------------


class TestUnauthenticated:
    @pytest.mark.parametrize(
        "method,url",
        [
            ("post", "/api/v1/organizations"),
            ("get", "/api/v1/organizations/me"),
            ("get", "/api/v1/projects"),
            ("get", "/api/v1/admin/users"),
            ("get", "/api/v1/audit-logs"),
        ],
        ids=["org-create", "org-me", "projects", "admin-users", "audit-logs"],
    )
    def test_requires_auth_401(self, v2_client, method, url):
        resp = v2_client.request(method, url, json={"name": "x"})
        assert resp.status_code == 401, resp.text
        body = resp.json()
        assert body["code"] == 401

    def test_org_member_endpoints_401(self, v2_client):
        resp = v2_client.delete("/api/v1/organizations/1/members/2")
        assert resp.status_code == 401
        resp = v2_client.get("/api/v1/organizations/1/roles")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# 2. 创建项目 → 列表 → 详情
# ---------------------------------------------------------------------------


class TestProjectsLifecycle:
    def test_create_list_detail(self, v2_client, app, make_user, auth_headers):
        uid = make_user(_uname("projA"))
        headers = auth_headers(uid)
        pname = f"项目_{uuid.uuid4().hex[:8]}"

        # 创建
        resp = v2_client.post("/api/v1/projects", headers=headers, json={"name": pname})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == 200
        assert body["message"] == "创建成功"
        assert body["data"]["name"] == pname
        assert body["data"]["owner_id"] == uid
        assert body["data"]["organization_id"] is None
        project_id = body["data"]["id"]

        # 列表（paginate_response 信封）
        resp = v2_client.get("/api/v1/projects", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == 200
        pagination = body["data"]["pagination"]
        assert {"total", "page", "per_page", "pages"} <= set(pagination)
        ids = [it["id"] for it in body["data"]["items"]]
        assert project_id in ids

        # 详情
        resp = v2_client.get(f"/api/v1/projects/{project_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["id"] == project_id
        assert resp.json()["data"]["name"] == pname

        # 更新
        resp = v2_client.put(
            f"/api/v1/projects/{project_id}",
            headers=headers,
            json={"description": "updated desc"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "更新成功"
        assert resp.json()["data"]["description"] == "updated desc"

        # 置顶
        resp = v2_client.put(f"/api/v1/projects/{project_id}/pin", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "置顶成功"
        assert resp.json()["data"]["is_pinned"] is True

    def test_create_project_validation(self, v2_client, make_user, auth_headers):
        uid = make_user(_uname("projV"))
        headers = auth_headers(uid)

        # 空 body（v1 语义：空 dict → 请求体不能为空）
        resp = v2_client.post("/api/v1/projects", headers=headers, json={})
        assert resp.status_code == 400
        assert resp.json()["message"] == "请求体不能为空"

        # 缺少 name
        resp = v2_client.post("/api/v1/projects", headers=headers, json={"other": "x"})
        assert resp.status_code == 400
        assert resp.json()["message"] == "缺少必需字段: name"

        # 名称超长
        resp = v2_client.post("/api/v1/projects", headers=headers, json={"name": "x" * 101})
        assert resp.status_code == 400
        assert resp.json()["message"] == "项目名称长度应为 1-100 个字符"

        # 同名重复
        name = _uname("dup")
        resp = v2_client.post("/api/v1/projects", headers=headers, json={"name": name})
        assert resp.status_code == 200
        resp = v2_client.post("/api/v1/projects", headers=headers, json={"name": name})
        assert resp.status_code == 400
        assert resp.json()["message"] == "项目名称已存在"

    def test_delete_project(self, v2_client, make_user, auth_headers):
        uid = make_user(_uname("projD"))
        headers = auth_headers(uid)
        resp = v2_client.post("/api/v1/projects", headers=headers, json={"name": _uname("del")})
        pid = resp.json()["data"]["id"]

        resp = v2_client.delete(f"/api/v1/projects/{pid}", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "删除成功"

        # 删除后再读 → 404
        resp = v2_client.get(f"/api/v1/projects/{pid}", headers=headers)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# 3. 用户 B 访问/修改/删除用户 A 的项目 → 404
# ---------------------------------------------------------------------------


class TestProjectIDOR:
    def test_user_b_cannot_touch_user_a_project(self, v2_client, make_user, auth_headers):
        uid_a = make_user(_uname("idorA"))
        uid_b = make_user(_uname("idorB"))
        headers_a = auth_headers(uid_a)
        headers_b = auth_headers(uid_b)

        resp = v2_client.post("/api/v1/projects", headers=headers_a, json={"name": _uname("secret")})
        assert resp.status_code == 200, resp.text
        pid = resp.json()["data"]["id"]

        # B 的列表看不到 A 的项目
        resp = v2_client.get("/api/v1/projects", headers=headers_b)
        assert resp.status_code == 200
        assert pid not in [it["id"] for it in resp.json()["data"]["items"]]

        # 读 → 404
        resp = v2_client.get(f"/api/v1/projects/{pid}", headers=headers_b)
        assert resp.status_code == 404, resp.text
        body = resp.json()
        assert body["code"] == 404
        assert body["message"] == "项目不存在"

        # 改 → 404
        resp = v2_client.put(
            f"/api/v1/projects/{pid}", headers=headers_b, json={"name": "hijacked"}
        )
        assert resp.status_code == 404

        # 置顶 → 404
        resp = v2_client.put(f"/api/v1/projects/{pid}/pin", headers=headers_b)
        assert resp.status_code == 404

        # 删 → 404
        resp = v2_client.delete(f"/api/v1/projects/{pid}", headers=headers_b)
        assert resp.status_code == 404

        # A 自己仍可读（项目未被破坏）
        resp = v2_client.get(f"/api/v1/projects/{pid}", headers=headers_a)
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# 4./5. admin 端点：非管理员 403，管理员 200
# ---------------------------------------------------------------------------


class TestAdminEndpoints:
    def test_member_forbidden(self, v2_client, make_user, auth_headers):
        uid = make_user(_uname("plainm"))  # 默认 role='member'
        headers = auth_headers(uid)

        resp = v2_client.get("/api/v1/admin/users", headers=headers)
        assert resp.status_code == 403, resp.text
        body = resp.json()
        assert body["code"] == 403
        assert body["message"] == "需要管理员权限"

        resp = v2_client.get("/api/v1/admin/users/1", headers=headers)
        assert resp.status_code == 403

        resp = v2_client.patch("/api/v1/admin/users/1/role", headers=headers, json={"role": "viewer"})
        assert resp.status_code == 403

    def test_admin_full_flow(self, v2_client, make_user, auth_headers):
        uid_admin = make_user(_uname("admink"), role="admin")
        uid_target = make_user(_uname("admint"))
        headers = auth_headers(uid_admin)

        # 列表
        resp = v2_client.get("/api/v1/admin/users", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == 200
        data = body["data"]
        assert {"items", "total", "page", "per_page", "pages"} <= set(data)
        assert data["total"] >= 2

        # search 过滤
        resp2 = v2_client.get(
            "/api/v1/admin/users", headers=headers, params={"search": "no_such_user_xyz"}
        )
        assert resp2.status_code == 200
        assert resp2.json()["data"]["total"] == 0

        # 详情（含敏感字段）
        resp = v2_client.get(f"/api/v1/admin/users/{uid_target}", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["id"] == uid_target
        assert "sso_provider" in resp.json()["data"]

        # 详情不存在 → 404
        resp = v2_client.get("/api/v1/admin/users/99999999", headers=headers)
        assert resp.status_code == 404
        assert resp.json()["message"] == "用户不存在"

        # 改角色
        resp = v2_client.patch(
            f"/api/v1/admin/users/{uid_target}/role", headers=headers, json={"role": "viewer"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "角色已从 member 修改为 viewer"

        # 非法角色
        resp = v2_client.patch(
            f"/api/v1/admin/users/{uid_target}/role", headers=headers, json={"role": "root"}
        )
        assert resp.status_code == 400
        assert resp.json()["message"] == "无效的角色，可选: admin, member, viewer"

        # 不能改自己
        resp = v2_client.patch(
            f"/api/v1/admin/users/{uid_admin}/role", headers=headers, json={"role": "viewer"}
        )
        assert resp.status_code == 400
        assert resp.json()["message"] == "不能修改自己的角色"

        # 改状态
        resp = v2_client.patch(
            f"/api/v1/admin/users/{uid_target}/status", headers=headers, json={"is_active": False}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "用户已禁用"

        # 缺参数
        resp = v2_client.patch(
            f"/api/v1/admin/users/{uid_target}/status", headers=headers, json={}
        )
        assert resp.status_code == 400
        assert resp.json()["message"] == "请提供 is_active 参数"

        # 重置密码（强度不足 → 400；合格 → 200）
        resp = v2_client.post(
            f"/api/v1/admin/users/{uid_target}/reset-password",
            headers=headers,
            json={"password": "weak"},
        )
        assert resp.status_code == 400
        resp = v2_client.post(
            f"/api/v1/admin/users/{uid_target}/reset-password",
            headers=headers,
            json={"password": "Str0ng!Pass9"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "密码已重置"


# ---------------------------------------------------------------------------
# 6. 审计日志写入与查询（可见域过滤）
# ---------------------------------------------------------------------------


class TestAuditLogs:
    def test_write_query_and_visible_domain(self, v2_client, app, make_user, auth_headers):
        uid_a = make_user(_uname("audA"))
        uid_b = make_user(_uname("audB"))
        headers_a = auth_headers(uid_a)
        headers_b = auth_headers(uid_b)

        # 写入（A 两条，B 一条）
        log_create = _make_audit_log(app, uid_a, "project_create", resource_type="project")
        log_delete = _make_audit_log(app, uid_a, f"act_{uuid.uuid4().hex[:8]}", resource_type="org")
        _make_audit_log(app, uid_b, "project_create", resource_type="project")

        # A 查询列表 → 能看到自己的日志
        resp = v2_client.get("/api/v1/audit-logs", headers=headers_a)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == 200
        data = body["data"]
        assert {"items", "total", "page", "per_page", "pages"} <= set(data)
        ids = [it["id"] for it in data["items"]]
        assert log_create in ids and log_delete in ids

        # 按 action 过滤
        resp = v2_client.get(
            "/api/v1/audit-logs",
            headers=headers_a,
            params={"resource_type": "org"},
        )
        assert resp.status_code == 200
        assert [it["id"] for it in resp.json()["data"]["items"]] == [log_delete]

        # 无效时间格式 → 400
        resp = v2_client.get(
            "/api/v1/audit-logs", headers=headers_a, params={"start_time": "not-a-date"}
        )
        assert resp.status_code == 400
        assert resp.json()["message"] == "start_time 格式无效"

        # 详情
        resp = v2_client.get(f"/api/v1/audit-logs/{log_create}", headers=headers_a)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["id"] == log_create

        # B 读 A 的日志 → 404（可见域修复；v1 无过滤可读全部）
        resp = v2_client.get(f"/api/v1/audit-logs/{log_create}", headers=headers_b)
        assert resp.status_code == 404, resp.text
        assert resp.json()["message"] == "审计日志不存在"

        # B 的列表看不到 A 的日志
        resp = v2_client.get("/api/v1/audit-logs", headers=headers_b)
        assert resp.status_code == 200
        assert log_create not in [it["id"] for it in resp.json()["data"]["items"]]

        # 统计（按可见域）
        resp = v2_client.get("/api/v1/audit-logs/stats", headers=headers_a)
        assert resp.status_code == 200, resp.text
        stats = resp.json()["data"]
        assert stats["period_days"] == 30
        assert "project_create" in stats["by_action"]
        assert any(u["user_id"] == uid_a for u in stats["active_users"])

    def test_export_permissions(self, v2_client, app, make_user, auth_headers):
        uid_member = make_user(_uname("expM"))
        uid_admin = make_user(_uname("expA"), role="admin")
        headers_member = auth_headers(uid_member)
        headers_admin = auth_headers(uid_admin)

        _make_audit_log(app, uid_member, "project_create", resource_type="project")

        # 非管理员导出 → 403
        resp = v2_client.get("/api/v1/audit-logs/export", headers=headers_member)
        assert resp.status_code == 403, resp.text
        assert resp.json()["message"] == "仅管理员可导出审计日志"

        # 管理员 CSV 导出
        resp = v2_client.get("/api/v1/audit-logs/export", headers=headers_admin)
        assert resp.status_code == 200, resp.text
        assert "text/csv" in resp.headers["content-type"]
        assert "audit-logs-" in resp.headers.get("content-disposition", "")
        assert "ID,用户ID,操作" in resp.text

        # 管理员 JSON 导出
        resp = v2_client.get(
            "/api/v1/audit-logs/export", headers=headers_admin, params={"format": "json"}
        )
        assert resp.status_code == 200, resp.text
        assert isinstance(resp.json()["data"], list)


# ---------------------------------------------------------------------------
# 7. 组织：创建 / 邀请 / 角色变更 / 移除（v1 校验链）
# ---------------------------------------------------------------------------


class TestOrganizations:
    def test_org_create_and_member_management(self, v2_client, make_user, auth_headers):
        uid_a = make_user(_uname("orgA"))
        uid_b = make_user(_uname("orgB"))
        uid_c = make_user(_uname("orgC"))
        headers_a = auth_headers(uid_a)
        headers_b = auth_headers(uid_b)
        headers_c = auth_headers(uid_c)

        # 创建组织（创建者默认 admin）
        org_name = f"组织_{uuid.uuid4().hex[:8]}"
        data = _create_org(v2_client, headers_a, org_name)
        org_id = data["id"]
        assert data["name"] == org_name
        assert data["owner_id"] == uid_a
        assert data["member_count"] == 1

        # 我的组织列表
        resp = v2_client.get("/api/v1/organizations/me", headers=headers_a)
        assert resp.status_code == 200
        assert org_id in [o["id"] for o in resp.json()["data"]]

        # 组织名称校验
        resp = v2_client.post("/api/v1/organizations", headers=headers_a, json={"name": ""})
        assert resp.status_code == 400
        assert resp.json()["message"] == "组织名称长度应为 1-100 个字符"

        # slug 冲突
        resp = v2_client.post(
            "/api/v1/organizations",
            headers=headers_c,
            json={"name": f"other_{uuid.uuid4().hex[:6]}", "slug": data["slug"]},
        )
        assert resp.status_code == 400
        assert resp.json()["message"] == "组织 slug 已存在"

        # 非成员邀请 → 403（成员身份校验）
        resp = _invite(v2_client, headers_c, org_id, uid_c)
        assert resp.status_code == 403, resp.text
        assert resp.json()["message"] == "无权限"

        # A 邀请 B（tester）
        resp = _invite(v2_client, headers_a, org_id, uid_b, role="tester")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["message"] == "成员邀请成功"
        assert body["data"]["role"] == "tester"

        # 重复邀请 → 400
        resp = _invite(v2_client, headers_a, org_id, uid_b)
        assert resp.status_code == 400
        assert resp.json()["message"] == "用户已在组织中"

        # 非法角色 → 400
        resp = _invite(v2_client, headers_a, org_id, uid_c, role="superman")
        assert resp.status_code == 400
        assert resp.json()["message"] == "无效的角色"
        assert resp.json()["errors"] == {"valid_roles": ["admin", "manager", "tester", "viewer"]}

        # B（tester）现在已加入，尝试邀请 → 403
        resp = _invite(v2_client, headers_b, org_id, uid_c)
        assert resp.status_code == 403
        assert resp.json()["message"] == "无权限邀请成员"

        # B 的 my-permissions
        resp = v2_client.get(f"/api/v1/organizations/{org_id}/my-permissions", headers=headers_b)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["role"] == "tester"
        assert resp.json()["data"]["original_role"] == "tester"

        # 非成员 C 查 my-permissions → 403
        resp = v2_client.get(f"/api/v1/organizations/{org_id}/my-permissions", headers=headers_c)
        assert resp.status_code == 403
        assert resp.json()["message"] == "不属于该组织"

        # B（tester）改角色 → 403
        resp = v2_client.patch(
            f"/api/v1/organizations/{org_id}/members/{uid_b}/role",
            headers=headers_b,
            json={"role": "admin"},
        )
        assert resp.status_code == 403
        assert resp.json()["message"] == "仅管理员可修改角色"

        # 非法角色 → 400
        resp = v2_client.patch(
            f"/api/v1/organizations/{org_id}/members/{uid_b}/role",
            headers=headers_a,
            json={"role": "god"},
        )
        assert resp.status_code == 400
        assert resp.json()["message"] == "无效的角色"

        # A 把 B 提为 manager
        resp = v2_client.patch(
            f"/api/v1/organizations/{org_id}/members/{uid_b}/role",
            headers=headers_a,
            json={"role": "manager"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "角色修改成功"
        assert resp.json()["data"]["role"] == "manager"

        # admin 查看成员权限
        resp = v2_client.get(
            f"/api/v1/organizations/{org_id}/members/{uid_b}/permissions", headers=headers_a
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["user_id"] == uid_b
        assert resp.json()["data"]["role"] == "manager"

        # B（manager）查自己权限
        resp = v2_client.get(f"/api/v1/organizations/{org_id}/my-permissions", headers=headers_b)
        assert resp.status_code == 200
        assert resp.json()["data"]["role"] == "manager"

        # 组织角色列表（系统角色）
        resp = v2_client.get(f"/api/v1/organizations/{org_id}/roles", headers=headers_a)
        assert resp.status_code == 200, resp.text
        role_names = [r["name"] for r in resp.json()["data"]]
        assert {"admin", "manager", "tester", "viewer"} <= set(role_names)

        # 非成员查组织角色 → 403
        resp = v2_client.get(f"/api/v1/organizations/{org_id}/roles", headers=headers_c)
        assert resp.status_code == 403
        assert resp.json()["message"] == "不属于该组织"

        # A 移除 B
        resp = v2_client.delete(
            f"/api/v1/organizations/{org_id}/members/{uid_b}", headers=headers_a
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "成员已移除"

        # 移除不存在的成员 → 404
        resp = v2_client.delete(
            f"/api/v1/organizations/{org_id}/members/{uid_b}", headers=headers_a
        )
        assert resp.status_code == 404
        assert resp.json()["message"] == "成员不存在"

        # B 被移除后再查权限 → 403
        resp = v2_client.get(f"/api/v1/organizations/{org_id}/my-permissions", headers=headers_b)
        assert resp.status_code == 403

        # C 邀请到不存在的组织 → 404
        resp = _invite(v2_client, headers_c, 99999999, uid_c)
        assert resp.status_code == 404
        assert resp.json()["message"] == "组织不存在"

    def test_last_admin_guard(self, v2_client, make_user, auth_headers):
        uid_a = make_user(_uname("lastA"))
        headers_a = auth_headers(uid_a)
        org_id = _create_org(v2_client, headers_a, _uname("lastorg"))["id"]

        # 不能降级组织唯一的管理员
        resp = v2_client.patch(
            f"/api/v1/organizations/{org_id}/members/{uid_a}/role",
            headers=headers_a,
            json={"role": "tester"},
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["message"] == "不能降级组织唯一的管理员"

        # 不能删除组织唯一的管理员
        resp = v2_client.delete(
            f"/api/v1/organizations/{org_id}/members/{uid_a}", headers=headers_a
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["message"] == "不能删除组织唯一的管理员"


# ---------------------------------------------------------------------------
# 8. 组织项目：属主或组织成员过滤
# ---------------------------------------------------------------------------


class TestOrgProjectAccess:
    def test_org_project_visible_to_members_only(self, v2_client, make_user, auth_headers):
        uid_a = make_user(_uname("orgpA"))
        uid_b = make_user(_uname("orgpB"))
        uid_c = make_user(_uname("orgpC"))
        headers_a = auth_headers(uid_a)
        headers_b = auth_headers(uid_b)
        headers_c = auth_headers(uid_c)

        org_id = _create_org(v2_client, headers_a, _uname("orgp"))["id"]
        resp = _invite(v2_client, headers_a, org_id, uid_b, role="tester")
        assert resp.status_code == 200, resp.text

        # A 属于唯一组织 → 自动归属组织上下文（复刻 v1 租户中间件语义）
        pname = f"org项目_{uuid.uuid4().hex[:8]}"
        resp = v2_client.post("/api/v1/projects", headers=headers_a, json={"name": pname})
        assert resp.status_code == 200, resp.text
        pid = resp.json()["data"]["id"]
        assert resp.json()["data"]["organization_id"] == org_id

        # 组织成员 B 可见（列表 + 详情）
        resp = v2_client.get("/api/v1/projects", headers=headers_b)
        assert resp.status_code == 200
        assert pid in [it["id"] for it in resp.json()["data"]["items"]]

        resp = v2_client.get(f"/api/v1/projects/{pid}", headers=headers_b)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["name"] == pname

        # 非成员 C → 404
        resp = v2_client.get(f"/api/v1/projects/{pid}", headers=headers_c)
        assert resp.status_code == 404
        assert resp.json()["message"] == "项目不存在"

        resp = v2_client.put(f"/api/v1/projects/{pid}", headers=headers_c, json={"name": "x"})
        assert resp.status_code == 404

        resp = v2_client.delete(f"/api/v1/projects/{pid}", headers=headers_c)
        assert resp.status_code == 404

        # A（单组织用户）再次创建 → 仍自动归属组织（与 v1 租户中间件行为一致）
        resp = v2_client.post("/api/v1/projects", headers=headers_a, json={"name": _uname("orgp2")})
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["organization_id"] == org_id

        # A 的列表包含两个组织项目（属主 ∪ 组织成员过滤）
        resp = v2_client.get("/api/v1/projects", headers=headers_a)
        assert resp.status_code == 200
        ids = [it["id"] for it in resp.json()["data"]["items"]]
        assert pid in ids
        assert len(ids) >= 2
