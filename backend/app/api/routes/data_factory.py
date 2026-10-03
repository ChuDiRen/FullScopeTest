"""AI 数据工厂 — 测试数据批量生成（模板生成，无需 AI key）"""

from typing import Any, Dict

from fastapi import APIRouter, Depends

from app.api.v2.deps import get_current_user, json_body, release_session
from app.core.logging import get_logger
from app.models.user import User
from app.services.ai.data_factory_service import get_data_factory_service

logger = get_logger(__name__)

router = APIRouter(prefix="/api/v1/ai/data-factory", tags=["data-factory"])

MAX_COUNT = 10000


@router.post("/generate")
@release_session
def generate(data: Dict[str, Any] = Depends(json_body), user: User = Depends(get_current_user)):
    """按字段 Schema 批量生成测试数据

    Body: {schema: [{name, type, rule}], count: int}
    兼容旧模板名调用：{template: "user", count: N}
    """
    svc = get_data_factory_service()
    try:
        template_name = data.get("template")
        if template_name:
            result = svc.generate(template_name, count=int(data.get("count", 10)))
            return {"code": 200, "data": result.get("data", [])}

        schema = data.get("schema")
        if not isinstance(schema, list) or not schema:
            return {"code": 400, "message": "schema 不能为空"}
        count = int(data.get("count", 10))
        if count < 1 or count > MAX_COUNT:
            return {"code": 400, "message": f"count 必须在 1-{MAX_COUNT} 之间"}

        rows = svc.generate_from_schema(schema, count)
        return {"code": 200, "data": rows}
    except ValueError as exc:
        return {"code": 400, "message": str(exc)}
    except Exception as exc:
        logger.error("数据工厂生成失败", error=str(exc), exc_info=True)
        return {"code": 500, "message": f"生成失败: {exc}"}
