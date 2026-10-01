# 大熊AI测试平台 后端

## 技术栈

- **框架**: FastAPI + uvicorn（纯 ASGI，零 Flask）
- **数据库**: PostgreSQL 15
- **ORM**: SQLAlchemy 2.0（声明式，`db.Model`/`Model.query` 兼容门面）
- **认证**: PyJWT（`app/core/jwt.py`，token 双通道：httpOnly Cookie + Bearer）
- **Web 自动化**: Playwright
- **性能测试**: Locust
- **任务队列**: Celery + Redis

## 项目结构

```
backend/
├── app/
│   ├── fastapi_app.py      # FastAPI 主应用入口（create_fastapi_app）
│   ├── api/
│   │   ├── v2/v1/          # 主路由层（路径 100% 兼容 /api/v1/*，自动发现注册）
│   │   └── v2/             # FastAPI 原生 v2 增强接口（/api/v2/*）
│   ├── core/               # runtime（配置/上下文）、jwt、passwords（scrypt）
│   ├── models/             # SQLAlchemy 模型（models/__init__.py 统一注册）
│   ├── services/           # 业务逻辑
│   ├── tasks/              # Celery 任务
│   ├── utils/              # 工具函数（sandbox.py 为高危区）
│   ├── config.py           # 配置管理（按 APP_ENV 选择配置分支）
│   ├── database.py         # 纯 SQLAlchemy 数据层（ContextVar 会话作用域）
│   └── extensions.py       # 扩展实例（db 等）
├── migrations/             # Alembic 迁移（alembic.ini + env.py，零 Flask）
├── init_db.py              # 建表 + 管理员初始化
├── manage.py               # CLI 管理命令（click）
├── run_fastapi.py          # 开发启动入口
└── requirements.txt        # Python 依赖
```

## 快速开始

### 1. 创建虚拟环境

```bash
python -m venv venv
venv\Scripts\activate  # Windows
source venv/bin/activate  # Linux/Mac
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置环境变量

```bash
copy .env.example .env
# 编辑 .env 文件，修改数据库连接等配置
```

### 4. 初始化数据库

```bash
# 确保 PostgreSQL 已启动，并创建数据库
# 创建数据库: CREATE DATABASE fullscopetest_dev;

# 初始化数据库表 + 管理员（密码必须显式提供）
INIT_ADMIN_USERNAME=admin INIT_ADMIN_EMAIL=admin@example.com INIT_ADMIN_PASSWORD=<你的密码> python init_db.py
# 或使用交互式 CLI
python manage.py create_admin
```

### 5. 启动开发服务器

```bash
python run_fastapi.py
# 默认端口 5211，与 web/vite.config.ts 代理对齐
```

## API 文档

启动服务后访问: http://localhost:5211/api/v1/（Swagger: /api/v2/docs）

### 认证接口

| 方法 | 路径 | 描述 |
|------|------|------|
| POST | /api/v1/auth/register | 用户注册 |
| POST | /api/v1/auth/login | 用户登录 |
| GET | /api/v1/auth/me | 获取当前用户 |
| POST | /api/v1/auth/refresh | 刷新 Token |

### 项目接口

| 方法 | 路径 | 描述 |
|------|------|------|
| GET | /api/v1/projects | 获取项目列表 |
| POST | /api/v1/projects | 创建项目 |
| GET | /api/v1/projects/:id | 获取项目详情 |
| PUT | /api/v1/projects/:id | 更新项目 |
| DELETE | /api/v1/projects/:id | 删除项目 |

### 环境接口

| 方法 | 路径 | 描述 |
|------|------|------|
| GET | /api/v1/projects/:id/environments | 获取环境列表 |
| POST | /api/v1/projects/:id/environments | 创建环境 |
| PUT | /api/v1/environments/:id | 更新环境 |
| DELETE | /api/v1/environments/:id | 删除环境 |

## 数据库迁移

```bash
# 全新库先初始化表（SQLite/开发环境可用 create_all）
python init_db.py

# 生成迁移文件
alembic -c migrations/alembic.ini revision --autogenerate -m "描述"

# 执行迁移
alembic -c migrations/alembic.ini upgrade head

# 回滚一个版本
alembic -c migrations/alembic.ini downgrade -1
```

数据库连接从 `DATABASE_URL` 环境变量（或 `backend/.env`）读取，与运行时配置一致。
