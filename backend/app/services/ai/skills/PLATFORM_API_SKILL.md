# 大熊AI测试平台 — 智能体能力说明（SKILL）

本文档是平台智能体的技能手册：说明平台有哪些业务域接口、智能体工具怎么用、
典型任务的工作流。拿不准接口路径时，先 `search_platform_apis` 检索，再 `call_platform_api` 调用。

## 工具总览与选择顺序

| 工具 | 用途 | 什么时候用 |
|------|------|-----------|
| `search_platform_apis` | 按关键字检索平台接口目录（路径/方法/参数） | 需要调用平台任何接口前，先查到准确路径模板 |
| `call_platform_api` | 以当前用户身份调用平台任意接口（300+ 条全量） | 查询数据、创建/修改资源、触发操作 |
| `fetch_api_docs` | 抓取外部 API 文档并解析端点清单 | 用户给出外部服务的文档 URL / openapi.json |
| `probe_api_endpoint` | 对外部接口发真实请求探活 | 压测前验证目标接口可用性与响应结构 |
| `create_performance_test` | 创建性能测试场景（落库） | 用户明确要求建压测场景 |
| `generate_performance_script` | 为场景生成定制化 Locust 脚本 | create_performance_test 之后 |
| `load_skill` | 读取本技能手册（可按章节加载） | 需要回顾工作流/参数规范时 |
| `query_failed_web_tests` / `query_recent_api_cases` / `list_performance_scenarios` | 查询用户自有数据 | 用户询问已有测试资产 |

优先级：**专用工具 > call_platform_api**。创建压测场景走专用工具（有校验和默认模板）；
其余一律 search → call。

## call_platform_api 调用规范

1. `path` 必须是目录里的**路径模板原样**（含 `{param}` 占位符），如
   `/api/v1/perf-test/scenarios/{scenario_id}`；未命中目录会被拒绝，先 search。
2. `path_values` 填路径占位符：`'{"scenario_id": 12}'`
3. `query_json` 填查询参数：`'{"page": 1, "page_size": 20}'`
4. `body_json` 填 JSON 请求体（POST/PUT/PATCH）；`body_required=true` 的接口必须给
5. 返回 `{status, content_type, body}`：status 是 HTTP 状态码，body 是响应文本（截断 3000 字符）。
   平台信封为 `{"code": 200, "data": ...}`，**成功统一 200**（含创建类），400 是参数错误，
   404 多为"不属你所有"（属主过滤铁律），401/403 是鉴权问题。
6. 创建类操作在调用前把关键参数说给用户（名称/目标/并发数），意图不明先问，不要编造 ID。

## 平台业务域地图（接口目录速查）

| 业务域 | 前缀 | 能做什么 |
|--------|------|---------|
| 认证与账号 | `/api/v1/auth` | 用户信息（GET /me）、头像、令牌管理；登录/注册/找回已对智能体拉黑 |
| 项目管理 | `/api/v1/projects` | 项目 CRUD、成员、徽章、看板配置、评论 |
| 组织与团队 | `/api/v1/organizations` | 组织、团队、成员角色、团队指标 |
| API 测试 | `/api/v1/api-test`（api_test.py） | 用例 CRUD、执行、断言、批量导入导出、用例版本 |
| Web UI 自动化 | `/api/v1/web-test`（web_test.py） | UI 脚本 CRUD、Playwright 执行、AI 探索报告 |
| APP 测试 | `/api/v1/app-test`（app_test.py） | 移动端脚本与执行 |
| 性能压测 | `/api/v1/perf-test` | 场景 CRUD、启动/停止、结果查询、对比、gRPC/Dubbo3 Triple 协议 |
| 测试计划 | `/api/v1/test-plans` | 计划 CRUD、关联用例、执行跟踪 |
| 报告 | `/api/v1/reports` | 测试报告、模板、定时调度 |
| 环境与变量 | `/api/v1/environments` | 环境配置、环境变量（脚本引用 ${VAR}） |
| Mock 服务 | `/api/v1/mock`（mock_server.py） | Mock 规则 CRUD、请求命中记录 |
| Swagger 导入 | `/api/v1/swagger-gen` | OpenAPI 上传 → AI 生成测试用例 |
| 数据工厂 | `/api/v1/data-factory` | 测试数据模型与批量造数 |
| AI 能力 | `/api/v1/ai/*`、`/api/v1/copilot` | 用例评审、语义去重、根因分析、脚本自愈、RAG、Prompt 版本；**对智能体自身屏蔽** |
| 质量门禁 | `/api/v1/quality-gates` | 门禁规则与评估 |
| 告警 | `/api/v1/alert-rules` | 告警规则与通知记录 |
| CI 集成 | `/api/v1/github-*`、`/api/v1/gitlab-*`、`/api/v1/triggers`、`/api/v1/github-checks` | Webhook、CI 检查、触发器 |
| 系统管理 | `/api/v1/admin`、`/api/v1/audit-logs`、`/api/v1/notifications` | 管理员操作、审计、通知（admin 接口需管理员角色） |
| 全局搜索 | `/api/v1/search` | 跨资源检索 |

目录是运行时从 OpenAPI schema 自动生成的，新增接口自动可用，本文档只做领域导航。

## 典型工作流（Recipes）

### R1 外部 API 文档 → 压测方案
1. `fetch_api_docs(url)` 拿端点清单
2. `probe_api_endpoint` 对关键接口探活（GET 优先；POST 用文档示例值；401/404/5xx 必须在方案中注明）
3. 按用户旅程设计场景（浏览/下单链路），读接口高权重、写接口低权重
4. 每场景 `create_performance_test` → `generate_performance_script`（requirements 带接口链路与权重）
5. 提醒用户：mock 源站（如 FakeStore）写操作不落库、缓存命中率虚高，TPS 只当基线

### R2 平台内数据查询/汇总
1. `search_platform_apis("场景")` 找到 `GET /api/v1/perf-test/scenarios`
2. `call_platform_api(method="GET", path="...")` 拿数据
3. 需要详情再对 `{id}` 资源逐个调用；跨域汇总优先一次列表 + 前端字段说明

### R3 帮用户建一条 API 用例
1. search 定位 `POST /api/v1/api-test/...` 用例创建端点，确认必填参数
2. 和用户确认名称/方法/URL/断言（信息不足先问）
3. call 创建 → 回显 id；必要时再调执行端点跑一次

### R4 测试结果诊断
1. `query_failed_web_tests` / `call_platform_api` 查最近失败
2. 失败详情进 `root-cause`（AI 能力域）或按报告接口取日志
3. 结论引用工具返回的真实数据，不虚构

## 安全边界（不可越过）

- `/api/v1/ai*` 不在目录中：智能体不能调用 AI 能力接口（防自递归）
- 登录/注册/刷新/找回/SSO 令牌链路已拉黑
- 所有调用以**当前用户**身份执行，属主过滤由路由层强制——查不到别人的资源是正常现象，不要重试
- DELETE 类调用必须来自用户明确意图；批量删除先列出将删的 id 让用户确认
- 外部 URL（fetch/probe）过 SSRF 校验，内网地址会被拦截
