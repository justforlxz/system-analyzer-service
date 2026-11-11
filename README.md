# system-analyzer-service

Linux 远程诊断与启动过程分析平台。通过 SSH 临时采集目标机器的启动过程数据（systemd
Unit 生命周期、Journal、CPU/内存/磁盘 I/O），统一存储后在同一条时间轴上回放与分析。
目标机器不安装常驻采集程序。

当前仓库处于骨架阶段：技术栈、目录结构、构建与测试命令已经定稿，业务功能由后续任务加入。
实施基线与全部命令见 [`docs/implementation-baseline.md`](docs/implementation-baseline.md)。

## 组成

| 目录 | 内容 |
| --- | --- |
| `backend/` | FastAPI 应用与 Worker 进程，Python 3.12，依赖用 uv 管理 |
| `frontend/` | Next.js App Router 前端，TypeScript + Tailwind CSS + shadcn/ui |
| `docs/` | 实施基线、契约与设计文档 |
| `scripts/` | 本地开发库与本地门禁脚本 |
| `.github/workflows/ci.yml` | 持续集成 |

## 快速开始

```bash
# 1. 后端依赖
cd backend && uv sync

# 2. 本地 PostgreSQL（Podman，端口 55432）
../scripts/dev-db.sh start
cp .env.example .env
uv run alembic upgrade head

# 3. 后端单元测试
uv run pytest

# 4. 前端依赖与测试
cd ../frontend && npm ci && npm test

# 5. 全部检查
cd .. && scripts/gate.sh
```

运行服务：

```bash
cd backend && uv run sas-api        # API：http://127.0.0.1:8000
cd backend && uv run sas-worker     # 后台任务进程
cd frontend && npm run dev          # 前端：http://127.0.0.1:3000
```

## 约定

- FastAPI 是唯一的业务 API 层，前端不通过 Next.js Route Handlers 重做业务逻辑。
- 一个任务一个分支一个 PR。
- 凭据只通过环境变量注入，仓库里只有 `.env.example`。
- 数据缺失必须显式标记，不得写成 0。
