# FullScopeTest — Agent 工作说明

AI 驱动的全链路自动化测试平台（API 测试 / Web UI 自动化 / APP 测试 / 性能压测）。
**后端为纯 FastAPI + SQLAlchemy 2.0（2026-09 起 Flask 已整体移除，仓库内零 flask 依赖）**，前端 React 18 + Vite + AntD 5。
技术栈：FastAPI + uvicorn + SQLAlchemy 2.0（声明式）+ PyJWT + hashlib.scrypt + Celery + Redis + PostgreSQL + Playwright + Locust。文档与注释以中文为主。

## 目录地图

- `backend/app/fastapi_app.py` — FastAPI 主应用入口（`create_fastapi_app`，模块级 `app` 惰性创建）；`app/api/v2/middleware.py` — ASGI 中间件（DB 会话作用域/限流/安全头/body 限制）
- `backend/app/api/v2/deps.py` — FastAPI 共享依赖（get_current_user/get_admin_user/client_ip/release_session 等）
- `backend/app/api/v2/v1/` — 主路由层（约 310 条，路径 100% 兼容 `/api/v1/*`，自动发现注册：模块内定义 `router` 即生效）
- `backend/app/api/v2/`（auth/test_cases/api_tests/perf_tests/ui_tests）— FastAPI 原生 v2 增强接口（`/api/v2/*`）
- `backend/app/core/runtime.py` — 运行时核心：配置（get_config）、请求上下文（ctx）、DB 生命周期、init_runtime()
- `backend/app/core/jwt.py` — PyJWT 令牌；`backend/app/core/passwords.py` — scrypt 哈希（纯 hashlib，格式 `scrypt$n$r$p$salt$hash`）
- `backend/app/database.py` — SQLAlchemy 2.0 数据层（`Base` 声明式基类、ContextVar 作用域 `db.session`、`select()` 查询、`paginate()` 分页助手）
- `backend/app/models/` SQLAlchemy 模型（43+，全部 `class X(Base)` 且显式 `__tablename__`，`models/__init__.py` 注册——新模型必须补注册否则 create_all 漏建表）、`backend/app/services/` 服务层、`backend/app/tasks/` Celery 任务、`backend/app/utils/sandbox.py`+`js_executor.py` 用户脚本执行（高危区）
- `backend/migrations/` Alembic 迁移（CLI：`alembic -c migrations/alembic.ini upgrade head`）；`backend/tests/` pytest（含 `tests/api_v2/` FastAPI 套件）
- `web/` React 前端；`e2e/` Playwright；`docker/`、`deploy/helm/`、`scripts/`、`document/USER_GUIDE.md`

## 常用命令

```bash
# 后端测试（本机必须禁用插件自动加载——logfire 插件损坏；conftest 自动用 SQLite 临时库）
cd backend && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/ -q
cd backend && PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/api_v2/ -q   # FastAPI API 套件

# 后端开发运行（端口 5211，与 web/vite.config.ts 代理对齐；本机无 Postgres，必须显式指 SQLite）
cd backend && DATABASE_URL='sqlite:///fullscopetest.db' python run_fastapi.py

# 初始化数据库 + 管理员（密码必须显式提供；init_runtime 含幂等 create_all，全新库可直接起服务）
cd backend && DATABASE_URL='sqlite:///fullscopetest.db' INIT_ADMIN_USERNAME=admin INIT_ADMIN_EMAIL=... INIT_ADMIN_PASSWORD=... python init_db.py

# 生产运行
cd backend && APP_ENV=production uvicorn app.fastapi_app:app --host 0.0.0.0 --port 5000 --workers 4

# 前端
cd web && npm install && npm run dev | build | lint | test

# E2E（需先 npm run e2e:install）
npm run e2e

# Docker
docker compose up -d                                  # 开发
docker compose -f docker-compose.prod.yml up -d      # 生产（backend=uvicorn, celery=celery_worker.celery）
```

## 架构边界与改动规则

- **新增/修改 API 的路径**：改 `app/api/v2/v1/` 对应模块（或新建文件定义 `router`），业务逻辑写进 `app/services/`；禁止把业务逻辑写回遗留 Flask 蓝图。
- **鉴权**：路由参数 `user: User = Depends(get_current_user)`；管理员 `get_admin_user`。token 双通道（httpOnly Cookie + Bearer）；签发走 `app/core/jwt.py`（PyJWT）；黑名单 + token_version fail-closed。公开端点必须注明"v1 设计即公开"。
- **属主过滤是铁律**：按 id 取资源一律带 user_id/owner 过滤（越权 404），历史上多起 IDOR 均源于此。
- **同步端点必须加 `@release_session`**（deps）：同步路由跑在 anyio 线程池，不加会泄漏 DB 会话（Windows 上表现为 SQLite 文件锁 WinError 32）。
- **用户脚本执行（sandbox.py/js_executor.py）**：AST 黑名单拦截 subprocess/os/sys/open 等（locust 画像可用 allow_network_libs=True 放行网络库）；子进程一律最小化 env（绝不继承后端全量环境变量）；新执行路径必须复用 check_script_safety。
- **限流**：全局 ASGI RateLimitMiddleware（Redis 滑动窗口，生产 fail-closed）；登录/注册等敏感端点另有路由级 sliding_window_rate_limit。测试环境 RATELIMIT_ENABLED=False。
- **客户端 IP**：一律用 deps.client_ip —— 默认不信任 X-Forwarded-For，仅 TRUST_PROXY_HEADERS=true 时信任。
- **时区**：展示用 Asia/Shanghai，存储一律 timezone-aware UTC；naive/aware 混用是本项目反复出 bug 的模式。
- **访客统计/geo 检测已整体下线（隐私合规，2026-10-01）**：原功能无告知收集访客 IP/设备/行为数据并向 ip-api.com 外呼，已删除前后端全部代码（visitor_stats/geo 路由、VisitorStat 模型、useVisitorTracker、隐藏页 /hidden/visitor-stats、geo 语言提示、Support 联系方式弹窗、Google Fonts 外链），表由迁移 006_drop_visitor_stats drop。**禁止重新引入任何访客 IP 采集、用户 IP 向第三方外呼、或无告知的行为追踪**。

## 约定

- Git：husky + commitlint，Conventional Commits；lint-staged 只对 `web/*.{ts,tsx}` 跑 eslint+prettier。
- 前端状态 zustand，API 统一走 `src/services/api.ts`（httpOnly Cookie 登录态，token 不进 localStorage）。
- i18n：`web/src/i18n/locales/{zh,en}.json` 双语言同步加。
- 配置：生产强制 SECRET_KEY/JWT_SECRET_KEY 环境变量；dev 默认密钥只允许在 development/testing 配置分支。
- 测试：本机 Redis 需认证且可能黑洞挂死，测试文件用 autouse fixture 把 `app.services.token_blacklist._get_redis` 与 `app.services.rate_limit_service._get_redis` stub 成 `lambda: None`；AI/外呼/Celery 全 mock，禁止真实外呼。

## 已知坑（2026-09 零 Flask 化后状态）

- **禁止重新引入 flask/werkzeug/flask-* 依赖与任何 Flask 风格兼容层**（requirements.txt 已清零；密码哈希用 core/passwords，JWT 用 core/jwt，配置用 core/runtime.get_config，请求上下文用 core/runtime.ctx 与 core/request_local；ORM 一律 SQLAlchemy 2.0：`select()` + `db.session`，无 `Model.query`/`db.Model`）。
- **API 成功响应统一 200**（含创建类端点，信封 `code=200`），不要新写 201；测试同步断言 200。
- **expire_on_commit=True（SQLAlchemy 默认）**：commit 后再访问实体属性会触发刷新；需要 id 时先 `flush()` 再取，别依赖 commit 后的属性读取（Flask-SQLAlchemy 时代的 expire_on_commit=False 写法已清除）。
- DB 会话作用域基于 ContextVar（database._session_scope）：每请求由 DbSessionMiddleware 放置新令牌并 teardown；**测试里不要再写 engine.dispose()/强关连接的 fixture**（会杀伤后续用例，此类 hack 已删除）。
- `init_db.py` 建表后才能起服务（全新库先 `python init_db.py`）；生产用 Alembic（单头链 001→…→005）。
- FastAPI 0.138 的 include_router 是惰性 `_IncludedRouter`，枚举路由要穿透 `original_router.routes`。
- OpenAPI 侧 OPTIONS 由 CORS 中间件处理、HEAD 随 GET 自动支持——路由对账不要把这算缺失。
- `web/package-lock.json` 被 .gitignore 规则误覆盖但已被 git 追踪，勿删。
- Windows 开发机：scheduler 的 fcntl 文件锁自动降级；lifespan 里 Postgres 不可达时会拖慢启动（连接超时后才就绪）。
- `tests/` 中存在少量"记录已知 bug"的反向断言用例（如 exposes_query_bug），修 bug 后需同步删除；遗留套件存在跨文件顺序依赖（单跑过、合跑可能挂）。
- 本机 Redis 需认证且可能黑洞挂死：测试文件用 autouse fixture 把 `token_blacklist._get_redis`/`rate_limit_service._get_redis` stub 成 `lambda: None`；AI/外呼/Celery 全 mock。
- **测试客户端是 httpx TestClient**：requests 时代 API（`resp.content_type`/`resp.get_data()`/`resp.get_json()`）不可用，用 `resp.headers`/`resp.text`/`resp.json()`；TestClient **默认跟随重定向**，测 302 必须传 `follow_redirects=False`（否则 Location 打回后端不存在的路由表现为 404）。
- **校验错误统一 400 信封**（全局 handler 把 Pydantic 422 转 `{"code":400,...}`），测试对缺参/非法参数断言 400 而非 422。
- `conftest.py` 全局设 `MAX_LOGIN_FAILURES=999999` 防测试间锁定泄漏，而 `password_policy` 的两个常量是**模块导入时固化**——测锁定行为的用例必须 `monkeypatch.setattr(pp, 'MAX_LOGIN_FAILURES', 5)` 显式恢复（LOCKOUT_DURATION 同理给足秒数，1 秒会被 `int()` 抹成 remaining=0）。
- 遗留套件测试**固定用户名/项目名会互相污染**（项目名 owner 内唯一、用户名全局唯一）——新测试一律 uuid 后缀；服务层统计/检索类接口（team_metrics/rag 等）合跑会撞共享库数据，测试用 org/project 隔离参数。
- **fixture 直建 Project 禁止硬编码 `owner_id=1`**：report/AI 类端点有属主校验（越权 404），owner 必须等于实际 API 用户 id——单跑碰巧 id=1 是假绿，合跑必挂（test_semantic_dedup/test_swagger_case_generator 曾踩）。
- **统计端点已全部收口到可访问域**：trend/trend-stats 按 `_accessible_project_ids`（自有+组织项目）过滤，team-metrics 无项目且无组织上下文时 fail-closed 返回空指标；沙箱契约=整模块 AST 黑名单（requests/pathlib/subprocess 默认全拦，仅 `allow_network_libs=True` 放行网络库）；`ApiTestCase.priority` 是整数枚举（1-高 2-中 3-低），不再接受 "P0" 字符串。
- 前端 `npm install --force`（package-lock 内有 `@esbuild/linux-x64` 平台残留，Windows 触发 EBADPLATFORM）；esbuild 的 postinstall 被 npm approve-scripts 拦截时先 `npm approve-scripts esbuild && npm rebuild esbuild` 再 `npm run dev`。
- **前端响应式约定**：移动端断点 `<768px`（responsive.css 与 `useIsMobile` hook 共用）；布局层行为（抽屉侧边栏、顶栏按钮收纳）用 `useIsMobile` 条件渲染，纯样式走 responsive.css——**禁止对 aside 做 display:none 一刀切**（移动端会失去全部导航）；顶栏新增控件须考虑移动端收纳（次要项包 `{!isMobile && ...}`）；vite HMR 遇 JSX 中间态报错后可能卡住不恢复，需重启 dev server（注意杀干净 3001 端口残留进程再启）。
- **同步 Redis 调用禁止直接跑在事件循环**：`rate_limit_service`/`token_blacklist` 是同步 redis 客户端（socket_timeout=2s），async 中间件/协程里必须 `run_in_threadpool` 包裹（RateLimitMiddleware 曾因此在 Redis 故障时每请求阻塞 loop 2s+，最终 Windows Proactor accept 循环 OSError 22 全站拒绝新连接）；两个服务的 `_get_redis` 带 30s 不可用熔断（`_redis_unavailable_until`），新增 Redis 调用方须复用该模式、且 `_get_redis()` 返回 None 时按降级处理（ping 失败的坏客户端必须重置为 None，否则永久复用每请求刷 ERROR）。

