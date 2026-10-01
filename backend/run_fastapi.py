"""
FastAPI 开发/生产启动入口

用法：
    python run_fastapi.py                      # 开发，默认 127.0.0.1:5211（与前端 vite 代理对齐）
    PORT=5000 python run_fastapi.py            # 容器内生产运行
    APP_ENV=production python run_fastapi.py # 生产配置（强制校验 SECRET_KEY/JWT_SECRET_KEY）

生产推荐：uvicorn app.fastapi_app:app --host 0.0.0.0 --port 5000 --workers 4
（app.fastapi_app 模块级 app 为惰性创建，uvicorn 可直接引用）
"""

import os

import uvicorn


def main():
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5211"))
    is_production = os.environ.get("APP_ENV") == "production"

    uvicorn.run(
        "app.fastapi_app:app",
        host=host,
        port=port,
        reload=not is_production and os.environ.get("RELOAD", "false").lower() == "true",
        log_level="info",
        # 开发环境单 worker；生产多 worker 时建议直接用 uvicorn CLI + --workers
        workers=1 if not is_production else int(os.environ.get("WEB_CONCURRENCY", "1")),
    )


if __name__ == "__main__":
    main()
