"""
v1 平迁路由层（app/api/routes/，自 Flask 蓝图平迁；目录名只代表代码组织，
所有路由路径仍是 /api/v1/...，与 URL 版本无关）

每个模块定义 `router = APIRouter(...)`，路由路径与原 Flask 蓝图完全一致
（/api/v1/...），保证前端与 CI 脚本零改动。

`iter_routers()` 供 fastapi_app.py 自动发现并注册所有路由，新增模块
只需在包内新建文件并定义 `router` 即可，无需修改注册代码。
"""

import importlib
import pkgutil
from typing import Iterator

from fastapi import APIRouter


def iter_routers() -> Iterator[APIRouter]:
    """自动发现包内所有定义了 `router` 的模块"""
    for mod_info in pkgutil.iter_modules(__path__):
        if mod_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{mod_info.name}")
        router = getattr(module, "router", None)
        if router is not None and isinstance(router, APIRouter):
            yield router
