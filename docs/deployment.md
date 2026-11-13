# 部署与运维

本文说明如何在一台干净的主机上部署 system-analyzer-service 1.0，并完成一次导入验证。
命令与 `scripts/gate.sh`、`.github/workflows/ci.yml` 一致；改动部署方式时先改本文档。

## 1. 组成

| 进程 | 命令 | 默认端口 | 职责 |
| --- | --- | --- | --- |
| API | `uv run sas-api` | 8000 | 业务接口、上传落盘、任务入队、查询 |
| Worker | `uv run sas-worker` | 无 | SSH 采集、导入、后台分析 |
| 前端 | `npm run start` | 3000 | 页面渲染与同源转发 |
| PostgreSQL | 外部服务或容器 | 5432 | 任务、诊断数据、目标机器记录 |

API、Worker 与前端必须在同一台主机或共享文件系统上，因为上传文件由 API 写入、Worker 读取
（`SAS_UPLOAD_DIR` 指向同一个路径）。

## 2. 前置条件

- Linux 主机，能访问目标机器的 22 端口。
- Python 3.12 与 [uv](https://docs.astral.sh/uv/)。
- Node.js 22（CI 固定版本）。**不要使用 Node 27 预发布版**：`next start` 会在服务端渲染时
  报 `hasOwnProperty is not a function`，所有页面返回 500。
- PostgreSQL 17，或本机 Podman/Docker（只用于开发与验收）。

## 3. 准备数据库

生产环境用一个专用库与账号，口令只通过环境变量提供：

```sql
CREATE ROLE sas LOGIN PASSWORD '<数据库口令>';
CREATE DATABASE system_analyzer OWNER sas;
```

本地开发与验收可以直接起容器（端口 55432）：

```bash
scripts/dev-db.sh start    # 创建 sas-postgres 容器
```

## 4. 配置

后端配置全部来自环境变量，前缀 `SAS_`。可以写进未提交的 `backend/.env`，也可以直接导出。

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `SAS_DATABASE_URL` | 是 | PostgreSQL 异步连接串，例如 `postgresql+asyncpg://sas:***@127.0.0.1:5432/system_analyzer` |
| `SAS_UPLOAD_DIR` | 否 | 上传目录，默认 `var/uploads`；API 与 Worker 必须一致，建议用绝对路径 |
| `SAS_SSH_PASSWORD` | 采集时必填 | 目标机器 SSH 口令；数据库里只存变量名，不存口令 |
| `SAS_API_HOST` / `SAS_API_PORT` | 否 | API 监听地址与端口，默认 `127.0.0.1:8000` |
| `SAS_LOG_LEVEL` | 否 | 日志级别，默认 `INFO` |
| `SAS_WORKER_ID` | 否 | Worker 实例标识，多实例时各自不同 |
| `SAS_WORKER_POLL_INTERVAL_SECONDS` | 否 | 队列空闲时的轮询间隔，默认 2 秒 |
| `SAS_WORKER_LOCK_TIMEOUT_SECONDS` | 否 | 任务锁超时，默认 900 秒 |
| `SAS_MAX_UPLOAD_BYTES` | 否 | 单文件上传上限，默认 512 MiB |

前端只需要一个变量，且在构建时读取：

| 变量 | 说明 |
| --- | --- |
| `NEXT_PUBLIC_API_BASE_URL` | FastAPI 基地址，默认 `http://127.0.0.1:8000` |

## 5. 安装与迁移

```bash
cd backend
uv sync --frozen
export SAS_DATABASE_URL='postgresql+asyncpg://sas:***@127.0.0.1:5432/system_analyzer'
export SAS_UPLOAD_DIR='/var/lib/system-analyzer/uploads'
mkdir -p "$SAS_UPLOAD_DIR"
uv run alembic upgrade head
uv run alembic heads        # 输出必须只有一行，带 (head)
```

`alembic heads` 出现多行说明迁移分叉，先合并再启动服务。

## 6. 启动

三个前台进程，生产环境用 systemd、supervisor 或容器编排托管：

```bash
# 终端 1：API
cd backend && uv run sas-api

# 终端 2：Worker（多实例时设置不同的 SAS_WORKER_ID）
cd backend && SAS_WORKER_ID=worker-1 uv run sas-worker

# 终端 3：前端
cd frontend
npm ci
NEXT_PUBLIC_API_BASE_URL='http://127.0.0.1:8000' npm run build
NEXT_PUBLIC_API_BASE_URL='http://127.0.0.1:8000' npm run start
```

一个最小的 systemd 单元示例（放在部署主机上，不是目标机器）：

```ini
[Unit]
Description=system-analyzer-service API
After=network.target postgresql.service

[Service]
WorkingDirectory=/opt/system-analyzer-service/backend
EnvironmentFile=/etc/system-analyzer-service.env
ExecStart=/usr/bin/env uv run sas-api
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Worker 用同样的单元，把 `Description` 与 `ExecStart` 换成 `sas-worker`，并设置不同的
`SAS_WORKER_ID`。`EnvironmentFile` 的权限设为 `0600`。

## 7. 验证部署

```bash
curl -s http://127.0.0.1:8000/api/health
# {"status":"ok","version":"0.1.0"}

cd backend
JOB=$(curl -s -F "file=@../docs/samples/bootrec-v1/full.bootrec" \
  http://127.0.0.1:8000/api/imports | python3 -c 'import json,sys; print(json.load(sys.stdin)["job_id"])')

curl -s "http://127.0.0.1:8000/api/imports/$JOB" | python3 -m json.tool
# job.status = "succeeded"，result.records_written = 17，collection.import_status = "imported"

curl -s "http://127.0.0.1:8000/api/machines/machine%3Auos-PC/boots/3f1c9a52-8f0e-4a10-9b1c-2b7d5c3a6e11/timeline/tracks" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(len(d["items"]), len(d["tracks"]))'
```

浏览器打开 `http://127.0.0.1:3000/collections`，应看到 `collection:full-0001`；进入时间线
（`/timeline`）应能看到事件、区间与三条资源轨道。

## 8. 日常运维

- **日志**：API 与 Worker 输出到各自进程的标准输出。导入失败时，任务行的 `last_error`
  含异常类型与原文，只写数据库与日志，不通过接口返回；接口只给受控的失败分类与说明。
- **重试**：任务失败按任务行的 `max_attempts` 重试，重试间隔由任务队列决定。采集与导入都
  可重复执行，重试不会产生重复数据。
- **数据库抖动**：Worker 主循环遇到数据库暂时不可用时只记录日志并等下一轮，不退出进程。
- **上传文件**：导入成功或提交失败后会删除本次上传文件；长期运行时定期清理
  `SAS_UPLOAD_DIR` 下没有任务引用的残留文件。
- **备份**：`pg_dump` 备份数据库；上传目录只保存进行中的导入，导入后数据已进库。
- **升级**：停止 API 与 Worker，执行 `uv run alembic upgrade head`，再启动新版本。

## 9. 故障排查

| 现象 | 先查什么 |
| --- | --- |
| 导入任务一直 `queued` | Worker 是否在运行；API 与 Worker 的 `SAS_UPLOAD_DIR` 是否同一路径 |
| 所有页面 500，日志有 `hasOwnProperty` | Node 版本；改用 Node 22 |
| 时间线没有资源轨道 | 这份采集是否 Journal-only；`resources.available` 是否为 false |
| 某条轨道显示「不可用」 | `metric_stream` 实体的 `unavailable_reason`；这是数据缺失，不是 0 |
| 采集任务失败 | 任务详情里的阶段与步骤说明；目标机器 SSH、sudo 与 LightDM 状态 |
| Worker 反复重启 | 检查 `SAS_DATABASE_URL` 与数据库连接数；单元退出原因在日志首行 |

## 10. 1.0 的部署边界

- 不校验目标机器的主机密钥（`known_hosts=None`），口令认证存在中间人风险，后续版本补主机密钥字段。
- 接口没有身份认证，部署在受信任内网，由反向代理做访问控制。
- 单机单体部署；插件在 API 与 Worker 进程内注册，不引入分布式插件运行时。
- 墙钟文字使用 boot 级锚点，不是 per-collection 锚点（见 `docs/timeline-query.md` 第 10 节）。
