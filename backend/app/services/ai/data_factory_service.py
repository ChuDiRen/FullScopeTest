"""
AI 测试数据工厂服务
"""

import random
import time
from typing import Dict, Any, List
from ...core.logging import get_logger

logger = get_logger(__name__)

BUILTIN_TEMPLATES = {
    "user": {
        "name": "用户数据",
        "fields": {
            "username": {"type": "string", "pattern": "user_{random_int}"},
            "email": {"type": "string", "pattern": "user_{random_int}@test.com"},
            "phone": {"type": "string", "pattern": "138{random_int_8}"},
            "age": {"type": "integer", "min": 18, "max": 65},
            "status": {"type": "enum", "values": ["active", "inactive", "pending"]},
        },
    },
    "order": {
        "name": "订单数据",
        "fields": {
            "order_no": {"type": "string", "pattern": "ORD{timestamp}"},
            "amount": {"type": "float", "min": 10.0, "max": 10000.0},
            "status": {"type": "enum", "values": ["pending", "paid", "completed"]},
        },
    },
    "product": {
        "name": "商品数据",
        "fields": {
            "name": {"type": "string", "pattern": "商品_{random_int}"},
            "price": {"type": "float", "min": 1.0, "max": 9999.0},
            "stock": {"type": "integer", "min": 0, "max": 1000},
        },
    },
}


class DataFactoryService:
    """测试数据工厂服务"""

    def __init__(self):
        self._generated = {}
        self._id_counter = {}

    def get_templates(self):
        return [{"name": k, "display_name": v["name"], "fields": list(v["fields"].keys())}
                for k, v in BUILTIN_TEMPLATES.items()]

    def generate(self, template_name, count=1, custom_rules=None, seed=None):
        template = BUILTIN_TEMPLATES.get(template_name)
        if not template:
            raise ValueError(f"模板 {template_name} 不存在")
        if count < 1 or count > 10000:
            raise ValueError("生成数量必须在 1-10000 之间")
        if seed is not None:
            random.seed(seed)

        data = []
        for i in range(count):
            item = self._generate_one(template, template_name, custom_rules)
            data.append(item)

        if template_name not in self._generated:
            self._generated[template_name] = []
        self._generated[template_name].extend(data)

        logger.info("测试数据生成完成", template=template_name, count=count)
        return {"template": template_name, "count": count, "data": data}

    def cleanup(self, template_name=None):
        if template_name:
            count = len(self._generated.get(template_name, []))
            self._generated.pop(template_name, None)
            self._id_counter.pop(template_name, None)
        else:
            count = sum(len(v) for v in self._generated.values())
            self._generated.clear()
            self._id_counter.clear()
        logger.info("测试数据已清理", template=template_name, count=count)
        return {"cleaned": count}

    def generate_from_schema(self, schema, count=10, seed=None):
        """按前端字段 Schema [{name, type, rule}] 生成数据（规则可空）"""
        if seed is not None:
            random.seed(seed)
        fields = {}
        for f in schema:
            name = (f.get("name") or "").strip()
            if not name:
                continue
            ftype = f.get("type") or "string"
            rule = (f.get("rule") or "").strip()
            fields[name] = self._schema_field_def(ftype, rule)
        template = {"name": "custom", "fields": fields}
        template_name = "custom"
        data = [self._generate_one(template, template_name) for _ in range(max(1, min(count, 10000)))]
        logger.info("自定义 Schema 数据生成完成", count=len(data), fields=len(fields))
        return data

    def _schema_field_def(self, ftype, rule):
        """把前端字段类型+规则转为内部字段定义"""
        if ftype in ("number", "integer"):
            if "-" in rule:
                lo, _, hi = rule.partition("-")
                try:
                    return {"type": "integer", "min": int(lo or 0), "max": int(hi or 100)}
                except ValueError:
                    pass
            return {"type": "integer", "min": 0, "max": 100}
        if ftype == "email":
            return {"type": "pattern", "generator": "email"}
        if ftype == "phone":
            return {"type": "pattern", "generator": "phone"}
        if ftype == "name":
            return {"type": "pattern", "generator": "name"}
        if ftype == "date":
            return {"type": "pattern", "generator": "date"}
        if ftype == "boolean":
            return {"type": "enum", "values": [True, False]}
        if ftype == "uuid":
            return {"type": "pattern", "generator": "uuid"}
        if ftype == "address":
            return {"type": "pattern", "generator": "address"}
        if ftype == "url":
            return {"type": "pattern", "generator": "url"}
        # string：rule 作为前缀模式，空则随机词
        return {"type": "pattern", "generator": "string", "prefix": rule}

    def _generate_one(self, template, template_name, custom_rules=None):
        item = {}
        timestamp = int(time.time() * 1000)
        random_int = random.randint(10000, 99999)
        random_int_8 = str(random.randint(10000000, 99999999))

        if template_name not in self._id_counter:
            self._id_counter[template_name] = 0
        self._id_counter[template_name] += 1
        item_id = self._id_counter[template_name]

        for field_name, field_def in template["fields"].items():
            if custom_rules and field_name in custom_rules:
                rule = custom_rules[field_name]
                # 如果是字典规则（如 {"min": 20, "max": 30}），生成随机值
                if isinstance(rule, dict) and "min" in rule and "max" in rule:
                    if field_def.get("type") == "integer":
                        item[field_name] = random.randint(rule["min"], rule["max"])
                    elif field_def.get("type") == "float":
                        item[field_name] = round(random.uniform(rule["min"], rule["max"]), 2)
                    else:
                        item[field_name] = rule
                else:
                    item[field_name] = rule
                continue

            field_type = field_def.get("type", "string")
            if field_type == "string":
                pattern = field_def.get("pattern", "")
                value = pattern.replace("{random_int}", str(random_int))
                value = value.replace("{random_int_8}", random_int_8)
                value = value.replace("{timestamp}", str(timestamp))
                item[field_name] = value
            elif field_type == "integer":
                item[field_name] = random.randint(field_def.get("min", 0), field_def.get("max", 100))
            elif field_type == "float":
                item[field_name] = round(random.uniform(field_def.get("min", 0), field_def.get("max", 100)), 2)
            elif field_type == "enum":
                item[field_name] = random.choice(field_def.get("values", ["default"]))
            elif field_type == "pattern" and "generator" in field_def:
                item[field_name] = self._generate_by_kind(field_def["generator"], field_def)

        item["id"] = item_id
        return item


    def _generate_by_kind(self, kind, field_def):
        import string as _string
        import uuid as _uuid
        from datetime import datetime as _dt, timedelta as _td

        if kind == "email":
            return f"user{random.randint(1000, 9999)}@test.com"
        if kind == "phone":
            return "1" + random.choice("3589") + "".join(random.choices(_string.digits, k=9))
        if kind == "name":
            surnames = ["张", "王", "李", "赵", "陈", "刘", "杨", "黄"]
            givens = ["伟", "芳", "娜", "敏", "静", "磊", "军", "洋", "勇", "艳"]
            return random.choice(surnames) + random.choice(givens)
        if kind == "date":
            base = _dt(2024, 1, 1) + _td(days=random.randint(0, 730))
            return base.strftime("%Y-%m-%d")
        if kind == "uuid":
            return str(_uuid.uuid4())
        if kind == "address":
            cities = ["北京市朝阳区", "上海市浦东新区", "广州市天河区", "深圳市南山区", "杭州市西湖区"]
            return random.choice(cities) + f"某某路{random.randint(1, 999)}号"
        if kind == "url":
            return f"https://api.test.com/v1/resource/{random.randint(1, 9999)}"
        # string
        prefix = field_def.get("prefix", "")
        return f"{prefix}_{random.randint(10000, 99999)}" if prefix else "".join(
            random.choices(_string.ascii_lowercase, k=8))


_instance = None

def get_data_factory_service():
    global _instance
    if _instance is None:
        _instance = DataFactoryService()
    return _instance
