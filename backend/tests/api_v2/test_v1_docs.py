"""
v1 docs 平迁路由测试（FastAPI 实现，自 backend/app/api/docs.py 平迁）

覆盖：
1. 未登录访问 8 条受保护路由 → 401
2. 创建 → 列表 → 详情 → 更新 → 删除全链路（含分类/模板/分类与关键词筛选/导出）
3. 访问/修改他人资源 → 404（IDOR 修复验证：文档按 Project.owner_id 归属过滤）

环境说明：
- 本机 Redis 挂死/不可用：autouse fixture 把 token_blacklist 与
  rate_limit_service 的 _get_redis 替换为 lambda: None，用例内不再发起真实 Redis 连接。
"""

import uuid

import pytest
from app.extensions import db

DOCS_BASE = "/api/v1/docs"
PROJ_DOCS_BASE = "/api/v1/projects"


@pytest.fixture(autouse=True)
def _offline_redis(monkeypatch):
    """本机 Redis 挂死：token 黑名单/限流服务注入无网络实现（返回 None 即降级路径）"""
    import app.services.token_blacklist as tb
    import app.services.rate_limit_service as rls

    monkeypatch.setattr(tb, "_get_redis", lambda: None)
    monkeypatch.setattr(rls, "_get_redis", lambda: None)


def _uname() -> str:
    return f"doc_{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------------------
# 数据工厂
# ---------------------------------------------------------------------------

def _make_project(app, owner_id: int) -> int:
    from app.models.project import Project
    project = Project(
        name=f"proj_{owner_id}_{uuid.uuid4().hex[:8]}",
        owner_id=owner_id,
    )
    db.session.add(project)
    db.session.commit()
    return project.id

def _make_doc(app, project_id: int, user_id: int, **extra) -> int:
    from app.models.test_document import TestDocument
    title = extra.pop("title", "测试文档")
    doc = TestDocument(
        project_id=project_id,
        title=title,
        content=extra.pop("content", "# 文档\n内容"),
        category=extra.pop("category", "test_plan"),
        created_by=user_id,
        **extra,
    )
    db.session.add(doc)
    db.session.commit()
    return doc.id

# ---------------------------------------------------------------------------
# 1. 公开端点 + 未登录 401
# ---------------------------------------------------------------------------

class TestDocsAuth:
    @pytest.mark.parametrize(
        "method,url",
        [
            ("get", f"{DOCS_BASE}/categories"),
            ("get", f"{DOCS_BASE}/templates"),
            ("get", f"{DOCS_BASE}/1"),
            ("put", f"{DOCS_BASE}/1"),
            ("delete", f"{DOCS_BASE}/1"),
            ("get", f"{DOCS_BASE}/1/export"),
            ("get", f"{PROJ_DOCS_BASE}/1/docs"),
            ("post", f"{PROJ_DOCS_BASE}/1/docs"),
        ],
    )
    def test_unauthenticated_401(self, v2_client, method, url):
        """未登录访问受保护端点 → 401，响应结构与 error_response 一致"""
        # httpx 版 TestClient 的 get/delete 不接受 json= 参数
        kwargs = {"json": {}} if method in ("post", "put") else {}
        resp = getattr(v2_client, method)(url, **kwargs)
        assert resp.status_code == 401, f"{method.upper()} {url}: {resp.text}"
        assert resp.json()["code"] == 401


# ---------------------------------------------------------------------------
# 2. 全链路 CRUD + 分类/模板/筛选/导出
# ---------------------------------------------------------------------------

class TestDocsCRUD:
    def test_full_crud_cycle(self, v2_client, app, make_user, auth_headers):
        username = _uname()
        user_id = make_user(username)
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)

        # 创建
        resp = v2_client.post(
            f"{PROJ_DOCS_BASE}/{project_id}/docs",
            headers=headers,
            json={
                "title": "登录功能测试计划",
                "content": "# 计划\n\n覆盖登录/登出/锁定",
                "category": "test_plan",
                "tags": ["login", "p0"],
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["code"] == 200
        assert body["message"] == "创建成功"
        doc = body["data"]
        assert doc["title"] == "登录功能测试计划"
        assert doc["project_id"] == project_id
        assert doc["content"] == "# 计划\n\n覆盖登录/登出/锁定"
        assert doc["category"] == "test_plan"
        assert doc["tags"] == ["login", "p0"]
        assert doc["created_by"] == user_id
        assert doc["updated_by"] == user_id
        assert doc["author_name"] == username
        assert doc["version"] == "1.0"
        assert doc["is_published"] is False
        doc_id = doc["id"]

        # 列表（分页信封）
        resp = v2_client.get(f"{PROJ_DOCS_BASE}/{project_id}/docs", headers=headers)
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["pagination"]["total"] >= 1
        assert data["pagination"]["page"] == 1
        assert data["pagination"]["per_page"] == 20
        assert any(i["id"] == doc_id for i in data["items"])

        # category 筛选
        resp = v2_client.get(
            f"{PROJ_DOCS_BASE}/{project_id}/docs?category=test_case", headers=headers
        )
        assert resp.status_code == 200
        assert all(i["category"] == "test_case" for i in resp.json()["data"]["items"])

        # keyword 搜索（命中 content）
        resp = v2_client.get(
            f"{PROJ_DOCS_BASE}/{project_id}/docs?keyword=锁定", headers=headers
        )
        assert resp.status_code == 200
        assert any(i["id"] == doc_id for i in resp.json()["data"]["items"])

        # keyword 不命中
        resp = v2_client.get(
            f"{PROJ_DOCS_BASE}/{project_id}/docs?keyword=不存在的关键词xyz", headers=headers
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["items"] == []

        # 详情
        resp = v2_client.get(f"{DOCS_BASE}/{doc_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["data"]["title"] == "登录功能测试计划"

        # 更新
        resp = v2_client.put(
            f"{DOCS_BASE}/{doc_id}",
            headers=headers,
            json={
                "title": "登录功能测试计划 V2",
                "content": "# 计划 V2",
                "tags": ["login"],
                "is_published": True,
                "version": "1.1",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "更新成功"
        updated = resp.json()["data"]
        assert updated["title"] == "登录功能测试计划 V2"
        assert updated["content"] == "# 计划 V2"
        assert updated["tags"] == ["login"]
        assert updated["is_published"] is True
        assert updated["version"] == "1.1"

        # 删除
        resp = v2_client.delete(f"{DOCS_BASE}/{doc_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "删除成功"
        # 删除后详情 404
        assert v2_client.get(f"{DOCS_BASE}/{doc_id}", headers=headers).status_code == 404

    def test_create_validations(self, v2_client, app, make_user, auth_headers):
        user_id = make_user(_uname())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)

        # 空 body → 400（等价 v1 validate_json）
        resp = v2_client.post(f"{PROJ_DOCS_BASE}/{project_id}/docs", headers=headers, json={})
        assert resp.status_code == 400
        assert resp.json()["message"] == "请求体不能为空"

        # 缺 title → 400
        resp = v2_client.post(
            f"{PROJ_DOCS_BASE}/{project_id}/docs", headers=headers, json={"content": "x"}
        )
        assert resp.status_code == 400
        assert resp.json()["message"] == "缺少必需字段: title"

        # title 超长（256 字符）→ 400
        resp = v2_client.post(
            f"{PROJ_DOCS_BASE}/{project_id}/docs",
            headers=headers,
            json={"title": "长" * 256},
        )
        assert resp.status_code == 400
        assert resp.json()["message"] == "文档标题长度应为 1-255 个字符"

        # 更新时 title 超长 → 400
        doc_id = _make_doc(app, project_id, user_id)
        resp = v2_client.put(
            f"{DOCS_BASE}/{doc_id}", headers=headers, json={"title": "长" * 256}
        )
        assert resp.status_code == 400
        assert resp.json()["message"] == "文档标题长度应为 1-255 个字符"

    def test_categories_and_templates(self, v2_client, make_user, auth_headers):
        user_id = make_user(_uname())
        headers = auth_headers(user_id)

        # 分类列表
        resp = v2_client.get(f"{DOCS_BASE}/categories", headers=headers)
        assert resp.status_code == 200, resp.text
        categories = resp.json()["data"]
        assert [c["value"] for c in categories] == [
            "test_plan", "test_case", "test_report", "api_doc", "design", "other",
        ]
        assert categories[0]["label"] == "测试计划"

        # 模板列表
        resp = v2_client.get(f"{DOCS_BASE}/templates", headers=headers)
        assert resp.status_code == 200, resp.text
        templates = resp.json()["data"]
        assert [t["id"] for t in templates] == ["test_plan", "test_case", "api_doc"]
        assert all("content" in t and "# " in t["content"] for t in templates)

    def test_export_md_and_html(self, v2_client, app, make_user, auth_headers):
        user_id = make_user(_uname())
        headers = auth_headers(user_id)
        project_id = _make_project(app, user_id)
        doc_id = _make_doc(
            app, project_id, user_id,
            title="Export Doc", content="# Heading\n\n| a | b |\n|---|---|\n| 1 | 2 |",
        )

        # Markdown 导出
        resp = v2_client.get(f"{DOCS_BASE}/{doc_id}/export", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"] == "text/markdown; charset=utf-8"
        assert resp.headers["content-disposition"] == "attachment; filename=Export Doc.md"
        assert resp.text.startswith("# Export Doc\n\n")

        # HTML 导出
        resp = v2_client.get(f"{DOCS_BASE}/{doc_id}/export?format=html", headers=headers)
        assert resp.status_code == 200, resp.text
        assert "text/html" in resp.headers["content-type"]
        assert resp.headers["content-disposition"] == "attachment; filename=Export Doc.html"
        assert "<!DOCTYPE html>" in resp.text
        assert "Export Doc" in resp.text

        # 非 ASCII 文件名 → filename*=utf-8''
        doc_id2 = _make_doc(app, project_id, user_id, title="导出文档")
        resp = v2_client.get(f"{DOCS_BASE}/{doc_id2}/export", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-disposition"].startswith("attachment; filename*=utf-8''")

        # 不支持的格式 → 400
        resp = v2_client.get(f"{DOCS_BASE}/{doc_id}/export?format=pdf", headers=headers)
        assert resp.status_code == 400
        assert resp.json()["message"] == "不支持的导出格式"

    def test_missing_document_404(self, v2_client, make_user, auth_headers):
        user_id = make_user(_uname())
        headers = auth_headers(user_id)

        assert v2_client.get(f"{DOCS_BASE}/999999", headers=headers).status_code == 404
        assert v2_client.put(
            f"{DOCS_BASE}/999999", headers=headers, json={"title": "X"}
        ).status_code == 404
        assert v2_client.delete(f"{DOCS_BASE}/999999", headers=headers).status_code == 404
        assert (
            v2_client.get(f"{DOCS_BASE}/999999/export", headers=headers).status_code == 404
        )


# ---------------------------------------------------------------------------
# 3. 访问/修改他人资源 → 404（IDOR 修复）
# ---------------------------------------------------------------------------

class TestDocsIDOR:
    def test_other_user_document_404(self, v2_client, app, make_user, auth_headers):
        owner_id = make_user(_uname())
        attacker_id = make_user(_uname())
        owner_headers = auth_headers(owner_id)
        attacker_headers = auth_headers(attacker_id)

        project_id = _make_project(app, owner_id)
        doc_id = _make_doc(app, project_id, owner_id, title="Owner Doc")

        # 他人文档：读/改/删/导出 全部 404
        assert v2_client.get(f"{DOCS_BASE}/{doc_id}", headers=attacker_headers).status_code == 404
        assert (
            v2_client.put(
                f"{DOCS_BASE}/{doc_id}", headers=attacker_headers, json={"title": "Hacked"}
            ).status_code
            == 404
        )
        assert v2_client.delete(f"{DOCS_BASE}/{doc_id}", headers=attacker_headers).status_code == 404
        assert (
            v2_client.get(f"{DOCS_BASE}/{doc_id}/export", headers=attacker_headers).status_code
            == 404
        )

        # 他人项目文档列表/创建 → 404 项目不存在
        resp = v2_client.get(f"{PROJ_DOCS_BASE}/{project_id}/docs", headers=attacker_headers)
        assert resp.status_code == 404
        assert resp.json()["message"] == "项目不存在"
        assert (
            v2_client.post(
                f"{PROJ_DOCS_BASE}/{project_id}/docs",
                headers=attacker_headers,
                json={"title": "Steal"},
            ).status_code
            == 404
        )

        # owner 本人一切正常
        assert v2_client.get(f"{DOCS_BASE}/{doc_id}", headers=owner_headers).status_code == 200
        resp = v2_client.get(f"{PROJ_DOCS_BASE}/{project_id}/docs", headers=owner_headers)
        assert resp.status_code == 200
        assert any(i["id"] == doc_id for i in resp.json()["data"]["items"])
