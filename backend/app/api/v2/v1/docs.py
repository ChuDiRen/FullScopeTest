"""
测试文档模块 - FastAPI 平迁（自 app/api/docs.py，蓝图挂载前缀 /api/v1）

路径/方法/状态码/响应字段与原 v1 完全一致，前端与 CI 脚本零改动。
全部端点为同步 def，运行于 RequestContextMiddleware push 的 app context 内，
直接复用 数据库会话（app/database.py ContextVar 作用域） 与模型层（v1 docs 模块无独立 service，
与 v1 一致直查模型）。

路由清单（9 条，与 v1 一一对应；静态路由先于 /{doc_id} 注册避免被路径参数吞掉）：
- GET    /api/v1/docs/health                        模块健康检查（公开端点，v1 无 @jwt_required）
- GET    /api/v1/docs/categories                    分类列表
- GET    /api/v1/docs/templates                     文档模板列表
- GET    /api/v1/projects/{project_id}/docs         项目文档列表（category/keyword/page/per_page）
- POST   /api/v1/projects/{project_id}/docs         创建文档（title 必填）
- GET    /api/v1/docs/{doc_id}                      文档详情
- PUT    /api/v1/docs/{doc_id}                      更新文档
- DELETE /api/v1/docs/{doc_id}                      删除文档
- GET    /api/v1/docs/{doc_id}/export               导出 md/html（文件下载）

鉴权映射：@jwt_required() → Depends(_current_user)（内部复用 deps.get_current_user）。

IDOR 修复（越权一律 404）：
- 文档级路由（详情/更新/删除/导出）沿用 v1 的 TestDocument join Project 按
  Project.owner_id 过滤（v1 即安全），不存在/越权一律 404 '文档不存在'。
- 项目级路由（列表/创建）校验 project 属主（v1 即有，404 '项目不存在'），保持。

文件下载映射：
- Flask send_file(tempfile) → fastapi Response(content=..., media_type=...)，
  Content-Disposition 与 原框架 send_file 规则一致（ASCII 文件名直接 attachment;
  filename=...，非 ASCII 用 filename*=utf-8''<quoted>）；text/* 的 charset=utf-8
  与 send_file 行为一致。HTML 生成复用 v1 的 generate_doc_html（请求期导入，
  避免包级循环，与 reports.py 范例一致）。

会话生命周期：同步端点统一加 @release_session（deps.release_session），
鉴权依赖包装为 async _current_user（与 api_test.py/reports.py 范例一致）。
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional
from urllib.parse import quote

import markdown

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import or_

from ..deps import get_current_user, json_body, query_int, query_str, release_session
from ....extensions import db
from ....models.project import Project
from ....models.test_document import TestDocument
from ....models.user import User
from sqlalchemy import select
from ....database import paginate

router = APIRouter(tags=["docs"])


# ---------------------------------------------------------------------------
# 鉴权包装 / 响应构造 / 属主过滤
# ---------------------------------------------------------------------------

async def _current_user(request: Request) -> User:
    """鉴权依赖：在事件循环线程内执行同步 get_current_user，session 随 app context 回收"""
    return get_current_user(request)


def _request_id() -> str:
    """读取 RequestContextMiddleware 写入 scope state 的 request_id"""
    try:
        from ....core.runtime import ctx

        return ctx.get_request_id() or ""
    except Exception:
        return ""


def _success(data=None, message: str = "success", code: int = 200) -> JSONResponse:
    """等价 v1 success_response：{"code","message","data","timestamp"}"""
    return JSONResponse(
        status_code=code,
        content={
            "code": code,
            "message": message,
            "data": data,
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
        },
    )


def _error(code: int, message: str, errors=None) -> JSONResponse:
    """等价 v1 error_response：{"code","message","errors","request_id","timestamp"}"""
    return JSONResponse(
        status_code=code,
        content={
            "code": code,
            "message": message,
            "errors": errors,
            "request_id": _request_id(),
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
        },
    )


def _paginate(items, total: int, page: int, per_page: int, message: str = "success") -> JSONResponse:
    """等价 v1 paginate_response"""
    return JSONResponse(
        status_code=200,
        content={
            "code": 200,
            "message": message,
            "data": {
                "items": items,
                "pagination": {
                    "total": total,
                    "page": page,
                    "per_page": per_page,
                    "pages": (total + per_page - 1) // per_page,
                },
            },
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
        },
    )


def _get_owned_document(user_id: int, doc_id: int) -> Optional[TestDocument]:
    """按 id 取文档并经 Project.owner_id 做属主过滤：不存在/越权一律 None → 404"""
    return (
        db.session.scalar(select(TestDocument).join(Project, TestDocument.project_id == Project.id).filter(TestDocument.id == doc_id, Project.owner_id == user_id)))


def _attachment_disposition(download_name: str) -> str:
    """等价 原框架 send_file 的 Content-Disposition 规则（ASCII 直接用，非 ASCII filename*）"""
    try:
        download_name.encode("ascii")
    except UnicodeEncodeError:
        return f"attachment; filename*=utf-8''{quote(download_name)}"
    return f"attachment; filename={download_name}"


# ==================== 健康检查（公开端点） ====================

# ==================== 文档 HTML 渲染（自旧蓝图 app/api/docs.py 原样迁入） ====================


def generate_doc_html(doc):
    """生成文档 HTML"""
    # 将 Markdown 转换为 HTML
    try:
        content_html = markdown.markdown(doc.content or '', extensions=['tables', 'fenced_code'])
    except Exception:
        content_html = doc.content or ''
    
    html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{doc.title}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif; line-height: 1.6; color: #333; max-width: 900px; margin: 0 auto; padding: 40px 20px; }}
        h1 {{ font-size: 2.5em; margin-bottom: 10px; color: #1a1a1a; border-bottom: 2px solid #667eea; padding-bottom: 10px; }}
        h2 {{ font-size: 1.8em; margin-top: 30px; margin-bottom: 15px; color: #333; }}
        h3 {{ font-size: 1.4em; margin-top: 25px; margin-bottom: 10px; color: #444; }}
        p {{ margin: 15px 0; }}
        code {{ background: #f4f4f4; padding: 2px 6px; border-radius: 4px; font-family: 'Consolas', 'Monaco', monospace; }}
        pre {{ background: #2d2d2d; color: #f8f8f2; padding: 15px; border-radius: 8px; overflow-x: auto; margin: 15px 0; }}
        pre code {{ background: none; padding: 0; }}
        table {{ width: 100%; border-collapse: collapse; margin: 20px 0; }}
        th, td {{ padding: 12px; text-align: left; border: 1px solid #ddd; }}
        th {{ background: #f5f5f5; font-weight: 600; }}
        ul, ol {{ margin: 15px 0; padding-left: 30px; }}
        li {{ margin: 5px 0; }}
        blockquote {{ border-left: 4px solid #667eea; padding-left: 20px; margin: 20px 0; color: #666; font-style: italic; }}
        .meta {{ color: #999; font-size: 14px; margin-bottom: 30px; }}
        .tags {{ margin-top: 5px; }}
        .tag {{ display: inline-block; background: #e8e8e8; padding: 2px 8px; border-radius: 4px; margin-right: 5px; font-size: 12px; }}
    </style>
</head>
<body>
    <h1>{doc.title}</h1>
    <div class="meta">
        <div>分类: {doc.category} | 版本: {doc.version} | 更新时间: {doc.updated_at.strftime('%Y-%m-%d %H:%M') if doc.updated_at else '-'}</div>
        <div class="tags">
            {''.join([f'<span class="tag">{tag}</span>' for tag in (doc.tags or [])])}
        </div>
    </div>
    <div class="content">
        {content_html}
    </div>
</body>
</html>'''
    
    return html


# ==================== 文档模板 ====================


@router.get("/api/v1/docs/health")
@release_session
def docs_health():
    """文档模块健康检查（公开端点，v1 无 @jwt_required，保持公开）"""
    return _success(message="文档模块正常")


# ==================== 文档分类 ====================

@router.get("/api/v1/docs/categories")
@release_session
def get_document_categories(user: User = Depends(_current_user)):
    """获取文档分类列表"""
    categories = [
        {"value": "test_plan", "label": "测试计划", "icon": "📋"},
        {"value": "test_case", "label": "测试用例", "icon": "📝"},
        {"value": "test_report", "label": "测试报告", "icon": "📊"},
        {"value": "api_doc", "label": "接口文档", "icon": "📡"},
        {"value": "design", "label": "设计文档", "icon": "🎨"},
        {"value": "other", "label": "其他", "icon": "📄"},
    ]
    return _success(data=categories)


# ==================== 文档模板 ====================

@router.get("/api/v1/docs/templates")
@release_session
def get_document_templates(user: User = Depends(_current_user)):
    """获取文档模板列表"""
    templates = [
        {
            "id": "test_plan",
            "name": "测试计划模板",
            "category": "test_plan",
            "content": '''# 测试计划

## 1. 项目概述

### 1.1 项目背景
[描述项目背景和测试目的]

### 1.2 测试范围
[描述测试覆盖的功能模块]

## 2. 测试策略

### 2.1 测试类型
- 功能测试
- 接口测试
- 性能测试
- 安全测试

### 2.2 测试环境
| 环境 | 地址 | 说明 |
|------|------|------|
| 开发环境 | | |
| 测试环境 | | |
| 预发环境 | | |

## 3. 测试进度

### 3.1 里程碑
| 阶段 | 开始时间 | 结束时间 | 负责人 |
|------|----------|----------|--------|
| 测试准备 | | | |
| 功能测试 | | | |
| 回归测试 | | | |

## 4. 风险与应对
[描述可能的风险及应对措施]

## 5. 交付物
- 测试用例
- 测试报告
- Bug 列表
''',
        },
        {
            "id": "test_case",
            "name": "测试用例模板",
            "category": "test_case",
            "content": '''# 测试用例设计

## 模块名称
[填写模块名称]

## 测试用例

### TC-001: [用例名称]

**前置条件：**
- [条件1]
- [条件2]

**测试步骤：**
1. [步骤1]
2. [步骤2]
3. [步骤3]

**预期结果：**
- [预期1]
- [预期2]

**测试数据：**
```json
{
  "key": "value"
}
```

---

### TC-002: [用例名称]

**前置条件：**
- 

**测试步骤：**
1. 

**预期结果：**
- 

''',
        },
        {
            "id": "api_doc",
            "name": "接口文档模板",
            "category": "api_doc",
            "content": '''# 接口文档

## 基本信息
- 接口名称：
- 接口地址：
- 请求方式：GET/POST/PUT/DELETE
- Content-Type：application/json

## 请求参数

### Headers
| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| Authorization | string | 是 | Bearer Token |

### Body
| 参数名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| | | | |

### 请求示例
```json
{
  
}
```

## 响应参数

| 参数名 | 类型 | 说明 |
|--------|------|------|
| code | int | 状态码 |
| message | string | 提示信息 |
| data | object | 数据 |

### 响应示例
```json
{
  "code": 200,
  "message": "success",
  "data": {}
}
```

## 错误码
| 错误码 | 说明 |
|--------|------|
| 400 | 请求参数错误 |
| 401 | 未授权 |
| 404 | 资源不存在 |
| 500 | 服务器错误 |
''',
        },
    ]

    return _success(data=templates)


# ==================== 项目文档管理 ====================

@router.get("/api/v1/projects/{project_id}/docs")
@release_session
def get_documents(project_id: int, request: Request, user: User = Depends(_current_user)):
    """
    获取项目文档列表

    查询参数:
        category: 分类筛选
        keyword: 搜索关键词
        page: 页码
        per_page: 每页数量
    """
    user_id = user.id

    # 验证项目权限
    project = db.session.scalar(select(Project).filter_by(id=project_id, owner_id=user_id))
    if not project:
        return _error(404, "项目不存在")

    # 获取查询参数
    category = query_str(request, "category")
    keyword = query_str(request, "keyword").strip()
    page = query_int(request, "page", 1)
    per_page = query_int(request, "per_page", 20)

    # 构建查询
    query = select(TestDocument).filter_by(project_id=project_id)

    if category:
        query = query.filter_by(category=category)
    if keyword:
        query = query.filter(
            or_(
                TestDocument.title.ilike(f"%{keyword}%"),
                TestDocument.content.ilike(f"%{keyword}%"))
        )

    # 分页
    pagination = paginate(
        query.order_by(TestDocument.updated_at.desc()),
        page=page, per_page=per_page,
    )

    return _paginate(
        items=[d.to_dict() for d in pagination.items],
        total=pagination.total,
        page=page,
        per_page=per_page,
    )


@router.post("/api/v1/projects/{project_id}/docs")
@release_session
def create_document(
    project_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """
    创建文档（等价 v1 @validate_json('title')）

    请求体:
        title: 文档标题
        content: 文档内容 (Markdown)
        category: 分类 (test_plan/test_case/test_report/other)
        tags: 标签列表
    """
    user_id = user.id

    # 验证项目权限
    project = db.session.scalar(select(Project).filter_by(id=project_id, owner_id=user_id))
    if not project:
        return _error(404, "项目不存在")

    data = data or {}
    if not data:
        return _error(400, "请求体不能为空")
    if "title" not in data:
        return _error(400, "缺少必需字段: title")

    title = str(data["title"]).strip()
    if len(title) < 1 or len(title) > 255:
        return _error(400, "文档标题长度应为 1-255 个字符")

    doc = TestDocument(
        project_id=project_id,
        title=title,
        content=data.get("content", ""),
        category=data.get("category", "other"),
        tags=data.get("tags", []),
        created_by=user_id,
        updated_by=user_id,
    )

    db.session.add(doc)
    db.session.commit()

    return _success(data=doc.to_dict(), message="创建成功", code=200)


# ==================== 文档详情 / 更新 / 删除 ====================

@router.get("/api/v1/docs/{doc_id}")
@release_session
def get_document(doc_id: int, user: User = Depends(_current_user)):
    """获取文档详情（属主过滤，越权 404）"""
    doc = _get_owned_document(user.id, doc_id)
    if not doc:
        return _error(404, "文档不存在")

    return _success(data=doc.to_dict())


@router.put("/api/v1/docs/{doc_id}")
@release_session
def update_document(
    doc_id: int,
    data: Dict[str, Any] = Depends(json_body),
    user: User = Depends(_current_user),
):
    """更新文档（属主过滤，越权 404）"""
    user_id = user.id

    doc = _get_owned_document(user_id, doc_id)
    if not doc:
        return _error(404, "文档不存在")

    data = data or {}

    # 更新字段
    if "title" in data:
        title = str(data["title"]).strip()
        if len(title) < 1 or len(title) > 255:
            return _error(400, "文档标题长度应为 1-255 个字符")
        doc.title = title

    if "content" in data:
        doc.content = data["content"]

    if "category" in data:
        doc.category = data["category"]

    if "tags" in data:
        doc.tags = data["tags"]

    if "is_published" in data:
        doc.is_published = data["is_published"]

    if "version" in data:
        doc.version = data["version"]

    doc.updated_by = user_id
    doc.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)

    db.session.commit()

    return _success(data=doc.to_dict(), message="更新成功")


@router.delete("/api/v1/docs/{doc_id}")
@release_session
def delete_document(doc_id: int, user: User = Depends(_current_user)):
    """删除文档（属主过滤，越权 404）"""
    doc = _get_owned_document(user.id, doc_id)
    if not doc:
        return _error(404, "文档不存在")

    db.session.delete(doc)
    db.session.commit()

    return _success(message="删除成功")


# ==================== 文档导出 ====================

@router.get("/api/v1/docs/{doc_id}/export")
@release_session
def export_document(doc_id: int, request: Request, user: User = Depends(_current_user)):
    """
    导出文档（属主过滤，越权 404）

    查询参数:
        format: 导出格式 (md/html)

    Flask send_file(tempfile) → fastapi Response，Content-Type 与
    Content-Disposition 下载文件名与 v1 一致。
    """
    export_format = query_str(request, "format", "md") or "md"

    doc = _get_owned_document(user.id, doc_id)
    if not doc:
        return _error(404, "文档不存在")

    if export_format == "md":
        # 导出 Markdown 格式
        content = f"# {doc.title}\n\n{doc.content or ''}"
        return Response(
            content=content,
            media_type="text/markdown; charset=utf-8",
            headers={
                "Content-Disposition": _attachment_disposition(f"{doc.title}.md"),
            },
        )

    if export_format == "html":
        # 导出 HTML 格式（generate_doc_html 已随旧蓝图删除原样迁入本模块）
        html_content = generate_doc_html(doc)
        return Response(
            content=html_content,
            media_type="text/html; charset=utf-8",
            headers={
                "Content-Disposition": _attachment_disposition(f"{doc.title}.html"),
            },
        )

    return _error(400, "不支持的导出格式")
