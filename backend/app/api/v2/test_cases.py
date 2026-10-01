"""
FastAPI 测试用例管理模块（v2 表面）

安全约定（修复历史审计问题）：
- 全部路由强制 get_current_user 鉴权（旧实现完全未鉴权，任何人可读写他人用例）
- 单对象读取/修改/删除均校验属主（user_id），防止 IDOR
- project_id 通过 query 参数显式传入
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ...extensions import db
from ...models.api_test_case import ApiTestCase, ApiTestCollection
from ...core.logging import get_logger
from .deps import get_current_user
from ...models.user import User
from sqlalchemy import select
from sqlalchemy import func

logger = get_logger(__name__)

router = APIRouter(tags=["test-cases"], dependencies=[Depends(get_current_user)])


# ====== Pydantic Schemas ======

class CollectionCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = None
    parent_id: Optional[int] = None


class CollectionUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = None
    parent_id: Optional[int] = None


class TestCaseCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    method: str = Field('GET', description='HTTP method')
    url: str = Field(..., min_length=1)
    description: Optional[str] = None
    collection_id: Optional[int] = None
    headers: Optional[dict] = None
    params: Optional[dict] = None
    body: Optional[dict] = None
    body_type: Optional[str] = 'json'
    assertions: Optional[list] = None
    pre_script: Optional[str] = None
    post_script: Optional[str] = None
    variables: Optional[dict] = None
    environment_id: Optional[int] = None
    priority: int = Field(2, ge=1, le=3)
    tags: Optional[list] = None
    is_enabled: bool = True


class TestCaseUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    method: Optional[str] = None
    url: Optional[str] = Field(None, min_length=1)
    description: Optional[str] = None
    collection_id: Optional[int] = None
    headers: Optional[dict] = None
    params: Optional[dict] = None
    body: Optional[dict] = None
    body_type: Optional[str] = None
    assertions: Optional[list] = None
    pre_script: Optional[str] = None
    post_script: Optional[str] = None
    variables: Optional[dict] = None
    environment_id: Optional[int] = None
    priority: Optional[int] = Field(None, ge=1, le=3)
    tags: Optional[list] = None
    is_enabled: Optional[bool] = None


class PaginatedResponse(BaseModel):
    items: list
    total: int
    page: int
    limit: int


# ====== Collection Routes ======

@router.get("/collections")
async def list_collections(
    project_id: int = Query(..., description="Project ID"),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
):
    """获取 API 测试集合列表（按属主过滤）"""
    query = select(ApiTestCollection).filter_by(
        project_id=project_id,
        user_id=user.id,
    )

    total = db.session.scalar(select(func.count()).select_from(query.subquery()))
    collections = db.session.scalars(query.offset((page - 1) * limit).limit(limit)).all()

    return PaginatedResponse(
        items=[c.to_dict() for c in collections],
        total=total,
        page=page,
        limit=limit,
    )


@router.post("/collections", status_code=200)
async def create_collection(
    data: CollectionCreate,
    project_id: Optional[int] = Query(None),
    user: User = Depends(get_current_user),
):
    """创建 API 测试集合"""
    collection = ApiTestCollection(
        name=data.name,
        description=data.description,
        parent_id=data.parent_id,
        project_id=project_id,
        user_id=user.id,
    )

    db.session.add(collection)
    db.session.commit()

    return collection.to_dict()


@router.get("/collections/{collection_id}")
async def get_collection(collection_id: int, user: User = Depends(get_current_user)):
    """获取 API 测试集合详情（仅属主可见）"""
    collection = db.session.scalar(select(ApiTestCollection).filter_by(id=collection_id, user_id=user.id))
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")
    return collection.to_dict()


@router.put("/collections/{collection_id}")
async def update_collection(
    collection_id: int, data: CollectionUpdate, user: User = Depends(get_current_user)
):
    """更新 API 测试集合（仅属主可改）"""
    collection = db.session.scalar(select(ApiTestCollection).filter_by(id=collection_id, user_id=user.id))
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(collection, key, value)

    db.session.commit()
    return collection.to_dict()


@router.delete("/collections/{collection_id}", status_code=204)
async def delete_collection(collection_id: int, user: User = Depends(get_current_user)):
    """删除 API 测试集合（仅属主可删）"""
    collection = db.session.scalar(select(ApiTestCollection).filter_by(id=collection_id, user_id=user.id))
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")

    db.session.delete(collection)
    db.session.commit()


# ====== Test Case Routes ======

@router.get("/cases")
async def list_cases(
    project_id: int = Query(..., description="Project ID"),
    collection_id: Optional[int] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
):
    """获取 API 测试用例列表（按属主过滤）"""
    query = select(ApiTestCase).filter_by(
        project_id=project_id,
        user_id=user.id,
    )

    if collection_id:
        query = query.filter_by(collection_id=collection_id)

    total = db.session.scalar(select(func.count()).select_from(query.subquery()))
    cases = db.session.scalars(query.offset((page - 1) * limit).limit(limit)).all()

    return PaginatedResponse(
        items=[c.to_dict() for c in cases],
        total=total,
        page=page,
        limit=limit,
    )


@router.post("/cases", status_code=200)
async def create_case(
    data: TestCaseCreate,
    project_id: Optional[int] = Query(None),
    user: User = Depends(get_current_user),
):
    """创建 API 测试用例"""
    case = ApiTestCase(
        name=data.name,
        method=data.method,
        url=data.url,
        description=data.description,
        collection_id=data.collection_id,
        headers=data.headers,
        params=data.params,
        body=data.body,
        body_type=data.body_type,
        assertions=data.assertions,
        pre_script=data.pre_script,
        post_script=data.post_script,
        variables=data.variables,
        environment_id=data.environment_id,
        priority=data.priority,
        tags=data.tags,
        is_enabled=data.is_enabled,
        project_id=project_id,
        user_id=user.id,
    )

    db.session.add(case)
    db.session.commit()

    return case.to_dict()


@router.get("/cases/{case_id}")
async def get_case(case_id: int, user: User = Depends(get_current_user)):
    """获取 API 测试用例详情（仅属主可见）"""
    case = db.session.scalar(select(ApiTestCase).filter_by(id=case_id, user_id=user.id))
    if not case:
        raise HTTPException(status_code=404, detail="Test case not found")
    return case.to_dict()


@router.put("/cases/{case_id}")
async def update_case(case_id: int, data: TestCaseUpdate, user: User = Depends(get_current_user)):
    """更新 API 测试用例（仅属主可改）"""
    case = db.session.scalar(select(ApiTestCase).filter_by(id=case_id, user_id=user.id))
    if not case:
        raise HTTPException(status_code=404, detail="Test case not found")

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(case, key, value)

    db.session.commit()
    return case.to_dict()


@router.delete("/cases/{case_id}", status_code=204)
async def delete_case(case_id: int, user: User = Depends(get_current_user)):
    """删除 API 测试用例（仅属主可删）"""
    case = db.session.scalar(select(ApiTestCase).filter_by(id=case_id, user_id=user.id))
    if not case:
        raise HTTPException(status_code=404, detail="Test case not found")

    db.session.delete(case)
    db.session.commit()
