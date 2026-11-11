# 实施基线

本文档是 system-analyzer-service 1.0 的工程基线，由任务 0.1（MKACG-762）定稿。
后续 22 个任务直接引用本文档的结论，不各自另立技术选型、目录结构或命令。

- 任务依据：issue MKACG-762 正文与附件 Task Handoff；技术栈定稿见父 issue MKACG-761。
- 核实时间：2026-11-11。仓库此前为空（无分支、无提交），本文档对应的第一个提交直接推送到 `main`。
- 与本文档不一致的文档、代码或命令，以本文档为准；需要改动时先改本文档，再改代码。

目录：

1. 技术栈与版本
2. 五个未决项的结论与理由
3. 目录结构与模块边界
4. 命令清单
5. 配置与凭据
6. 新 clone 后的完整流程
7. 持续集成与本地门禁
8. 实际开发顺序
9. 与 23 项拆分的冲突
10. 偏差清单

## 1. 技术栈与版本

技术栈由 lxz 在父 issue MKACG-761 的「技术栈（定稿）」一节确定，本文档只补上 0.1 需要定稿的
实现层选择，并把实际解析到的版本固定下来。

### 后端

| 名称 | 选择 | 固定方式 | 本文档核实到的版本 |
| --- | --- | --- | --- |
| 语言 | Python 3.12 | `backend/.python-version` + `requires-python = ">=3.12"` | 3.12.16 |
| 依赖与虚拟环境 | uv | `backend/pyproject.toml` + `backend/uv.lock` | uv 0.13.7 |
| Web 框架 | FastAPI | `uv.lock` | 0.162.4 |
| ASGI 服务器 | Uvicorn | `uv.lock` | 0.54.0 |
| 数据库 | PostgreSQL | 本地用 Podman 容器 | `postgres:17-alpine` |
| 数据库访问 | SQLAlchemy 2（异步）+ asyncpg | `uv.lock` | 2.1.8 / 0.32.0 |
| 配置 | pydantic-settings | `uv.lock` | 2.16.2 |
| 迁移 | Alembic | `uv.lock` | 1.20.1 |
| 测试 | pytest + pytest-asyncio + httpx | `uv.lock` | 9.2.0 / 1.4.1 / 0.27+ |
| 静态检查与格式化 | Ruff（检查与格式化都用它） | `backend/pyproject.toml` 的 `[tool.ruff]` | 0.17.4 |

### 前端

| 名称 | 选择 | 固定方式 | 本文档核实到的版本 |
| --- | --- | --- | --- |
| 框架 | Next.js App Router | `frontend/package-lock.json` | 16.4.6 |
| 界面库 | React | 同上 | 19.3.1 |
| 语言 | TypeScript | 同上 | 5.9.3 |
| 样式 | Tailwind CSS v4 | 同上 | 4.3.5 |
| 组件 | shadcn/ui（`components.json` 已配好） | 同上 | 组件按需用 `npx shadcn@latest add <组件>` 加入 |
| 单元测试 | Vitest + React Testing Library | 同上 | 3.2.7 |
| 端到端测试 | Playwright | 同上 | 1.65.0 |
| API 客户端 | openapi-typescript | 同上 | 7.13.0 |
| 静态检查 | ESLint 9（`eslint-config-next` 的 flat 配置） | `frontend/eslint.config.mjs` | 9.39.4 |
| 格式化 | Prettier | `frontend/.prettierrc.json` | 3.9.10 |

图表与时间线的第三方库（Apache ECharts、自定义 Canvas/SVG）在任务 4.2、4.3 引入，
骨架里没有图表，不提前安装未使用的依赖。

## 2. 五个未决项的结论与理由

### 2.1 结论一：Worker 用 PostgreSQL 任务表实现，任务状态也存 PostgreSQL

**结论**

- 后台任务由一个独立进程 `sas-worker` 执行，它与 API 共用同一套 Python 代码。
- 任务队列是 PostgreSQL 的 `jobs` 表（迁移 `backend/migrations/versions/0001_jobs.py`），
  任务状态共四个：`queued`、`running`、`succeeded`、`failed`。
- 领取任务用 `SELECT ... FOR UPDATE SKIP LOCKED`，多个 Worker 可以同时跑，不需要额外的锁服务。
- 重试次数与重试延后时间存在任务行上（`attempts`、`max_attempts`、`run_after`）：
  `enqueue()` 的 `max_attempts` 参数写进任务行的同名列（默认 3），失败后是否重试由任务行自己的值决定。
  执行中的任务带 `locked_by` / `locked_at`，锁超时的任务会被重新放回队列，Worker 异常退出不会让任务永久卡住。
- 状态迁移由两条力量保证：`jobs` 表的写入条件（`WHERE status = ...`，同时起并发保护作用）
  与数据库的 `jobs_status_check` 约束。失败后回到队列还是进入失败终态，由
  `system_analyzer/jobs/models.py` 的纯函数 `status_after_failure` 计算，Worker 与测试共用这一处。

**理由**

- 父 issue 的约束明确「首版不引入消息队列」，而 Celery、arq、RQ、Dramatiq 都需要 Redis 或
  RabbitMQ 才能工作，属于计划外的中间件。
- 任务状态本身就是要展示给用户的信息（采集进度、导入进度），存进唯一的业务数据库，
  查询、分页、和历史记录都只有一条路径，不需要把 Redis 里的状态再同步一份到数据库。
- PostgreSQL 的 `SKIP LOCKED` 已经能满足当前的并发规模；换成独立队列服务，等到真有并发压力时再评估。

**已知限制**（任务 3.2 引入采集处理器之前必须处理）：

1. 任务执行期间不续租 `locked_at`。锁超时默认 900 秒，执行时间超过它的任务会被另一个 Worker
   当作失锁任务重新领取并再次执行 —— 采集任务可能跑几十秒到几十分钟，必须先解决：执行期间
   定期续租 `locked_at`，或按任务类型设置锁超时。
2. 数据库不可达时 Worker 直接退出，错误是原始 SQLAlchemy 堆栈；运行期间数据库断开同样立即退出，
   不重连、不重试，也没有包装成「数据库不可用」这类可读提示。采集任务跑到一半遇到数据库抖动
   会直接中断且无人重试。
3. `GET /api/health` 只反映进程存活，数据库已断时仍返回 `{"status": "ok"}`。至少要在健康检查里
   区分「进程存活」与「依赖可用」，避免调度方据此判断服务正常。

**被排除的候选**

| 候选 | 排除理由 |
| --- | --- |
| Celery | 需要 Redis 或 RabbitMQ 作为 broker，多一个必须运维的组件；配置与调试成本高 |
| arq | 需要 Redis；生态与文档较少 |
| RQ | 需要 Redis；调度与重试能力弱于 Celery，但同样引入 Redis |
| Dramatiq | 需要 Redis 或 RabbitMQ |
| FastAPI `BackgroundTasks` | 任务只活在请求进程里：进程重启即丢失，没有重试、没有状态查询、无法被多个 Worker 分摊，采集任务可能跑几十分钟，不能挂在请求上 |
| 内存队列 / 线程池 | 与上一条同样的问题，且无法跨进程 |

### 2.2 结论二：Python 3.12 + uv + pytest + Ruff

**结论**

- Python 版本 3.12：`backend/.python-version` 写 `3.12`，`pyproject.toml` 要求 `>=3.12`。
- 依赖与虚拟环境用 uv：`uv sync` 按 `uv.lock` 安装，锁文件进仓库，任何机器解析出同样的版本。
- 测试框架用 pytest（异步测试用 `pytest-asyncio` 的 `asyncio_mode = "auto"`，API 测试用 `httpx.ASGITransport`）。
- 静态检查与格式化都用 Ruff，不另外安装 black、isort、flake8。
- Ruff 规则集：`E`、`F`、`I`、`UP`、`B`、`ASYNC`、`RUF`；关闭 `RUF001/RUF002/RUF003`
  （中文注释里的全角标点是刻意写法）。
- 行宽 100。

**理由**

- uv 同时管 Python 版本、虚拟环境、依赖解析与锁文件，一条命令完成安装，比 poetry 少一层插件体系，
  安装速度也明显更快；CI 里用 `astral-sh/setup-uv` 一条 action 即可。
- 一个 Ruff 覆盖格式化和静态检查两件事，减少一个工具与一套配置。
- pytest 是 Python 事实标准，异步与夹具生态完整。

**被排除的候选**

| 候选 | 排除理由 |
| --- | --- |
| Poetry | 需要额外的虚拟环境管理与插件，锁文件解析慢；与 uv 功能重叠但没有速度优势 |
| pip + requirements.txt（或 pip-tools） | 没有统一的 Python 版本约束，锁文件要另装工具生成，虚拟环境要手工维护 |
| Conda | 面向科学计算的多语言环境，镜像与解析体积大，与纯 Python 后端不匹配 |
| unittest | 夹具、参数化、异步支持都比 pytest 弱 |
| black + isort + flake8 | 三个工具三套配置，Ruff 一个就能覆盖 |
| mypy（本阶段未启用） | 0.2、0.3 还没有定稿领域模型，现在开启只会产生大量待检查的空壳类型；等数据模型定稿后再评估是否加入，届时改这份文档 |

### 2.3 结论三：OpenAPI 客户端用 openapi-typescript 生成，产物进仓库

**结论**

- 生成分两步：
  1. 后端导出 OpenAPI 文档：`cd backend && uv run python -m system_analyzer.tools.export_openapi`
     写到 `backend/openapi.json`（该文件不进仓库）。
  2. 前端按它生成 TypeScript 类型：`cd frontend && npm run api:generate`，
     产物是 `frontend/src/lib/api/schema.d.ts`，这个文件进仓库。
- CI 有独立作业 `api-client-drift`：重新执行上面两步，然后 `git diff --exit-code` 检查
  `schema.d.ts` 是否与后端接口一致，不一致就让流水线失败。

**理由**

- openapi-typescript 只生成类型，不生成运行时请求代码，前端用什么发请求由页面自己决定；
  生成的 `.d.ts` 不参与打包，构建产物里没有额外体积。
- 产物进仓库：前端只装 Node 就能 `npm run build`、`npm test`、`npm run typecheck`，
  不需要在跑前端的地方再装 Python 与 uv。前后端仍然各自独立构建，互不依赖对方的构建产物。
- 进仓库的代价是可能与后端漂移，用 CI 的 `api-client-drift` 作业消除：谁改了后端接口却忘了重新生成，
  流水线直接失败。

**被排除的候选**

| 候选 | 排除理由 |
| --- | --- |
| openapi-generator | 需要 Java 运行时；生成的客户端代码量大，包含大量与当前页面无关的方法 |
| orval | 生成 TanStack Query 钩子，把取数方式绑死在特定数据请求库上；现在还没有确定前端取数方式 |
| 手写请求类型 | 与后端接口双份维护，必然漂移 |
| 产物不进仓库 | 跑前端就要先装 Python 与 uv，违反「前后端各自独立构建」的验收要求 |

### 2.4 结论四：采集、导入、分析任务都跑在 Worker，插件只在 Worker 侧注册

**结论**

- 任务分工：API 进程只做「业务 API、身份认证、数据查询、任务管理」，也就是查询与写队列；
  实际执行全部在 Worker 进程：
  - SSH 远程采集（任务 3.x）跑 Worker。
  - Journal 压缩包导入与 bootrec 导入（任务 1.3、1.4）跑 Worker。
  - Systemd 生命周期、OOM 等分析（任务 2.1、2.2）跑 Worker。
- 插件注册只有一份清单：`backend/src/system_analyzer/plugins/builtins.py` 的 `BUILTIN_PLUGINS`。
  API 启动（`api/app.py` 的 `create_app`）与 Worker 启动（`worker/main.py` 的 `main`）都调用
  同一个 `register_builtins()`，它在注册前跳过已存在的 id，所以两个进程各启动一次也只有一份注册。
- 插件实现文件按类别放在 `collectors/`、`importers/`、`analyzers/`，注册入口只有 `builtins.py` 一处。

**理由**

- 采集任务是长任务（SSH 连接、LightDM 登录等待、几十秒到几十分钟），放进 API 进程会让请求超时、
  进程重启丢任务，也无法用多个 Worker 分摊。
- 注册表放在共享代码里，两个进程用同一份清单，就从根本上避免了「同一个插件在 API 与 Worker 各注册一套」；
  `tests/test_plugin_registry.py` 用「API 构造两次 + Worker 再注册一次」验证注册表里仍然只有一份。

**被排除的候选**

| 候选 | 排除理由 |
| --- | --- |
| 采集跑在 API 进程 | 长任务阻塞请求、进程重启丢任务、无法横向扩展 Worker |
| API 与 Worker 各维护一份插件清单 | 必然出现两套注册与两套行为，正是父 issue 明确禁止的情况 |
| 让 Worker 通过 HTTP 调 API 注册插件 | 多一层网络依赖与启动顺序问题，本阶段没有收益 |

### 2.5 结论五：迁移用 Alembic，目录 `backend/migrations/`

**结论**

- 工具：Alembic，配置 `backend/alembic.ini`，迁移目录 `backend/migrations/`，修订文件放
  `backend/migrations/versions/`。
- 连接串来自 `Settings.database_url`（环境变量 `SAS_DATABASE_URL`），迁移文件里不写连接信息。
- 建表只由 `alembic upgrade head` 完成，应用启动时不自动建表。
- 首个修订 `0001_jobs` 建立后台任务表 `jobs`。

**理由**

- Alembic 是 SQLAlchemy 官方迁移工具，与已选的 SQLAlchemy 2 属于同一生态，不需要再引入一个 ORM 或迁移框架。
- 修订文件进仓库、按顺序执行，任何环境（本地、CI、生产）的建表过程一致，可以复现。
- 应用启动不自动建表，避免「启动时偷偷改表结构」带来的不可控风险；CI 的集成测试作业也是先跑迁移再跑测试。

**被排除的候选**

| 候选 | 排除理由 |
| --- | --- |
| 手写 SQL 脚本 + 自建版本表 | 要自己实现版本记录与顺序执行，重复 Alembic 已有功能 |
| Django 迁移 | 需要引入 Django，与 FastAPI 选型冲突 |
| 应用启动时 `create_all` | 无法表达变更（改列、改约束），多实例同时启动还会竞争建表 |
| 把迁移放在仓库根目录 | 迁移与后端代码同属一个交付单元，放在 `backend/` 下便于单独构建与运行 |

## 3. 目录结构与模块边界

下面的目录树就是仓库当前的实际结构（`git ls-files` 输出整理，不含构建产物与被忽略文件）。

```text
.
├── .github/
│   └── workflows/
│       └── ci.yml                     持续集成：后端检查、后端集成测试、前端检查、API 客户端漂移检查
├── backend/
│   ├── .env.example                   后端环境变量样例（复制为 backend/.env）
│   ├── .python-version                固定 Python 3.12
│   ├── alembic.ini                    Alembic 配置
│   ├── pyproject.toml                 依赖、Ruff、pytest 配置
│   ├── uv.lock                        依赖锁文件
│   ├── migrations/
│   │   ├── README
│   │   ├── env.py                     迁移运行环境，读取 SAS_DATABASE_URL
│   │   ├── script.py.mako             修订文件模板
│   │   └── versions/
│   │       └── 0001_jobs.py           建立后台任务表 jobs
│   ├── src/system_analyzer/
│   │   ├── __init__.py                版本号与模块边界说明
│   │   ├── api/
│   │   │   ├── __init__.py
│   │   │   ├── app.py                 FastAPI 应用工厂，唯一业务 API 层入口
│   │   │   ├── server.py              API 进程入口（uv run sas-api）
│   │   │   └── routers/
│   │   │       ├── __init__.py
│   │   │       └── health.py          健康检查路由
│   │   ├── core/
│   │   │   ├── __init__.py
│   │   │   ├── config.py              配置（环境变量 SAS_ 前缀）
│   │   │   └── db.py                  异步引擎与会话工厂
│   │   ├── domain/
│   │   │   └── __init__.py            统一数据模型（0.2、0.3 定稿后填充）
│   │   ├── storage/
│   │   │   └── __init__.py            仓储实现（1.1 起填充）
│   │   ├── plugins/
│   │   │   ├── __init__.py
│   │   │   ├── contracts.py           插件最小契约：PluginKind、PluginSpec（完整契约由 0.4 定稿）
│   │   │   ├── registry.py            进程内注册表，id 重复即报错
│   │   │   └── builtins.py            唯一的内置插件清单与注册入口
│   │   ├── jobs/
│   │   │   ├── __init__.py
│   │   │   ├── models.py              任务状态与状态迁移的纯函数
│   │   │   └── queue.py               PostgreSQL 任务队列（SKIP LOCKED）
│   │   ├── worker/
│   │   │   ├── __init__.py
│   │   │   ├── runner.py              任务执行循环（成功、失败、未知类型）
│   │   │   └── main.py                Worker 进程入口（uv run sas-worker）
│   │   ├── collectors/
│   │   │   └── __init__.py            SSH 采集插件目录（3.4 - 3.7）
│   │   ├── importers/
│   │   │   └── __init__.py            bootrec 与 Journal 导入插件目录（1.3、1.4）
│   │   ├── analyzers/
│   │   │   └── __init__.py            分析插件目录（2.1、2.2）
│   │   └── tools/
│   │       ├── __init__.py
│   │       └── export_openapi.py      导出 OpenAPI 文档
│   └── tests/
│       ├── conftest.py                测试环境变量与配置缓存清理
│       ├── test_api_app.py            API 冒烟测试
│       ├── test_config.py             配置读取测试
│       ├── test_job_state.py          任务状态机测试
│       ├── test_plugin_registry.py    插件注册表测试
│       ├── test_worker.py             Worker 主循环测试
│       └── integration/
│           └── test_job_queue.py      任务队列集成测试（需要 PostgreSQL）
├── frontend/
│   ├── .env.example                   前端环境变量样例（复制为 frontend/.env.local）
│   ├── .prettierignore
│   ├── .prettierrc.json
│   ├── components.json                shadcn/ui 配置
│   ├── eslint.config.mjs              ESLint 9 flat 配置
│   ├── next.config.ts                 Next.js 配置
│   ├── package.json                   依赖与全部前端命令
│   ├── package-lock.json              依赖锁文件
│   ├── playwright.config.ts           端到端测试配置
│   ├── postcss.config.mjs             Tailwind CSS v4 的 PostCSS 插件
│   ├── tsconfig.json                  TypeScript 配置（含 @/* 路径别名）
│   ├── vitest.config.ts               单元测试配置（jsdom 环境）
│   ├── vitest.setup.ts                测试环境准备（jest-dom、每例后清理）
│   ├── e2e/
│   │   └── home.spec.ts               端到端冒烟用例
│   └── src/
│       ├── app/
│       │   ├── globals.css            Tailwind 入口
│       │   ├── layout.tsx             根布局（Server Component）
│       │   └── page.tsx               首页（Server Component）
│       ├── components/
│       │   └── ui/
│       │       ├── button.tsx         shadcn/ui 风格的按钮
│       │       └── button.test.tsx    按钮单元测试
│       └── lib/
│           ├── utils.ts               cn()：合并 Tailwind 类名
│           └── api/
│               ├── config.ts          后端 API 基地址与路径拼接
│               ├── config.test.ts     地址拼接单元测试
│               └── schema.d.ts        由 openapi-typescript 生成，进仓库
├── docs/
│   └── implementation-baseline.md     本文档
├── scripts/
│   ├── dev-db.sh                      用 Podman 起本地 PostgreSQL
│   └── gate.sh                        本地门禁（与 CI 同一组检查）
├── .gitignore
└── README.md
```

### 模块边界

与父 issue 的并发边界一致：

| 目录 | 负责 | 不应该做 |
| --- | --- | --- |
| `api/` | 请求校验、身份认证、数据查询、任务入队 | 执行采集、导入、分析这类长任务 |
| `domain/` | 统一数据模型、时间基准与关联规则 | 读写数据库、访问网络 |
| `storage/` | PostgreSQL 读写 | 业务规则、HTTP 处理 |
| `plugins/` | 插件契约、注册表、内置清单 | 具体插件的业务逻辑 |
| `jobs/` | 任务状态机与任务队列 | 具体任务做什么 |
| `worker/` | 领取任务、调用处理器、记录结果 | 定义业务数据结构 |
| `collectors/` `importers/` `analyzers/` | 各插件实现 | 其他类别的插件逻辑 |
| `frontend/` | 页面、交互、数据展示 | 重新实现后端业务逻辑（不使用 Route Handlers 做业务） |

前端规则（来自父 issue，这里再明确一次）：普通页面优先 Server Components，
交互复杂的时间线、图表与回放组件用 Client Components；FastAPI 是唯一的业务 API 层。

## 4. 命令清单

所有命令都在仓库根目录执行，除非标注了 `cd`。

### 后端

| 用途 | 命令 |
| --- | --- |
| 安装依赖 | `cd backend && uv sync` |
| 安装依赖（CI，严格按锁文件） | `cd backend && uv sync --frozen` |
| 启动 API | `cd backend && uv run sas-api`（等价于 `uv run uvicorn system_analyzer.api.app:create_app --factory`） |
| 启动 Worker | `cd backend && uv run sas-worker` |
| 单元测试 | `cd backend && uv run pytest` |
| 集成测试（需要数据库） | `cd backend && uv run pytest -m integration`（测试库地址取自 `SAS_TEST_DATABASE_URL`，见第 5 节） |
| 静态检查 | `cd backend && uv run ruff check .` |
| 格式检查 | `cd backend && uv run ruff format --check .` |
| 格式化 | `cd backend && uv run ruff format .` |
| 建表/升级数据库 | `cd backend && uv run alembic upgrade head` |
| 新建一个迁移修订 | `cd backend && uv run alembic revision -m "说明"` |
| 导出 OpenAPI 文档 | `cd backend && uv run python -m system_analyzer.tools.export_openapi` |
| 构建分发包 | `cd backend && uv build` |

单元测试默认排除 `integration` 标记的用例（`pyproject.toml` 的 `addopts`），
所以 `uv run pytest` 不需要数据库就能通过。

### 前端

| 用途 | 命令 |
| --- | --- |
| 安装依赖 | `cd frontend && npm ci` |
| 开发服务器 | `cd frontend && npm run dev` |
| 生产构建 | `cd frontend && npm run build` |
| 启动生产构建 | `cd frontend && npm start` |
| 单元测试 | `cd frontend && npm test` |
| 单元测试（监听模式） | `cd frontend && npm run test:watch` |
| 端到端测试 | `cd frontend && npm run test:e2e`（首次需要 `npx playwright install chromium`） |
| 静态检查 | `cd frontend && npm run lint` |
| 类型检查 | `cd frontend && npm run typecheck` |
| 格式检查 | `cd frontend && npm run format:check` |
| 格式化 | `cd frontend && npm run format` |
| 生成 API 类型 | `cd frontend && npm run api:generate`（需要先导出 `backend/openapi.json`） |

### 数据库与门禁

| 用途 | 命令 |
| --- | --- |
| 起本地 PostgreSQL | `scripts/dev-db.sh start`（容器名 `sas-postgres`，端口 55432） |
| 停止并删除本地库 | `scripts/dev-db.sh stop` |
| 重建本地库 | `scripts/dev-db.sh reset` |
| 本地门禁 | `scripts/gate.sh` |
| 本地门禁（含集成测试） | `scripts/gate.sh --with-integration`（需先配置 `SAS_TEST_DATABASE_URL`） |

## 5. 配置与凭据

- 后端配置走环境变量，前缀 `SAS_`，由 `backend/src/system_analyzer/core/config.py` 的
  `Settings` 读取；本地开发可写进 `backend/.env`（不进仓库）。
- 集成测试的库地址 `SAS_TEST_DATABASE_URL` 用同一套读取方式（同一前缀、同一个 `backend/.env`，
  由 `backend/tests/conftest.py` 的 `TestDatabaseSettings` 读取），所以「导出环境变量」与
  「写进 `.env`」两种方式都生效。这个变量没有默认值：没配置时 `uv run pytest -m integration`
  直接以非 0 退出码报错，不会出现「全部跳过但退出码 0」的假通过。
- 必填项只有 `SAS_DATABASE_URL`。缺它就启动失败并报错，不做静默兜底，避免连到错误的库。
- 前端配置走 `NEXT_PUBLIC_API_BASE_URL`，本地写进 `frontend/.env.local`（不进仓库）。
  未配置时回落到 `http://127.0.0.1:8000`，新 clone 不配也能跑测试与构建。
- 仓库只提交 `.env.example`，`.gitignore` 屏蔽 `.env`、`.env.*`（`!.env.example` 例外）、
  `backend/.env`、`frontend/.env.local`。
- `scripts/dev-db.sh` 里出现的口令只属于本机开发容器，不是任何环境的真实凭据；
  真实环境的口令通过环境变量或部署平台注入，不写进仓库、不写进 issue 正文。

配置项一览：

| 变量 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `SAS_DATABASE_URL` | 是 | 无 | PostgreSQL 异步连接串 |
| `SAS_LOG_LEVEL` | 否 | `INFO` | 日志级别 |
| `SAS_API_HOST` | 否 | `127.0.0.1` | API 监听地址 |
| `SAS_API_PORT` | 否 | `8000` | API 监听端口 |
| `SAS_WORKER_ID` | 否 | `worker-1` | Worker 实例标识，写入任务锁 |
| `SAS_WORKER_POLL_INTERVAL_SECONDS` | 否 | `2.0` | 队列为空时的轮询间隔 |
| `SAS_WORKER_LOCK_TIMEOUT_SECONDS` | 否 | `900` | 任务锁超时，超时后任务可被重新领取 |
| `SAS_TEST_DATABASE_URL` | 跑集成测试时必填 | 无 | 集成测试使用的数据库；未配置时 `pytest -m integration` 直接报错退出 |
| `NEXT_PUBLIC_API_BASE_URL` | 否 | `http://127.0.0.1:8000` | 前端访问的后端地址 |

## 6. 新 clone 后的完整流程

```bash
git clone git@github.com:justforlxz/system-analyzer-service.git
cd system-analyzer-service

# 后端
cd backend
uv sync
uv run pytest                        # 单元测试，不需要数据库

# 需要数据库时（.env.example 已包含 SAS_TEST_DATABASE_URL，指向下面这个本地库）
../scripts/dev-db.sh start
cp .env.example .env
uv run alembic upgrade head
uv run pytest -m integration        # 期望输出：4 passed；未配置库地址会直接报错退出

# 前端
cd ../frontend
npm ci
npm test
npm run lint

# 全部检查
cd .. && scripts/gate.sh
```

前置条件：uv、Node.js 22 与 npm、Podman（只有数据库与集成测试需要）。

关于集成测试的两个要点：

- 库地址只从 `SAS_TEST_DATABASE_URL` 取，不会回落到 `SAS_DATABASE_URL`：这些用例会清空 `jobs` 表，
  绝不应当指向真实环境。
- 想临时指向别的库时，用环境变量把 `SAS_TEST_DATABASE_URL` 覆盖成目标库的连接串即可；
  连接串里的口令由运行者提供，不写进仓库、不写进文档。

## 7. 持续集成与本地门禁

`.github/workflows/ci.yml` 有四个作业，本地门禁 `scripts/gate.sh` 执行同一组检查：

| 作业 | 内容 |
| --- | --- |
| `backend` | `uv sync --frozen`、`ruff format --check`、`ruff check`、`pytest` |
| `backend-integration` | 起 PostgreSQL 服务容器、`alembic upgrade head`、`pytest -m integration` |
| `frontend` | `npm ci`、`lint`、`typecheck`、`format:check`、`test`、`build` |
| `api-client-drift` | 重新导出 OpenAPI 并生成 `schema.d.ts`，与仓库中的文件不一致即失败 |

`scripts/gate.sh` 覆盖前三个作业的本地等价检查（集成测试用 `--with-integration` 开启，
`api-client-drift` 需要同时装好后端与前端，本地按需手工执行）。

## 8. 实际开发顺序

顺序与父 issue 的阶段表一致，按 stage 推进；同一 stage 内的任务可以并行，stage 之间是闸门：

| stage | 任务 |
| --- | --- |
| 1 | 0.1 确认实施基线与工程骨架（本文档） |
| 2 | 0.2 统一数据模型与 bootrec v1、0.4 Core 与 Plugin 接口契约 |
| 3 | 0.3 时间基准与跨数据源关联规范 |
| 4 | 1.1 诊断数据存储与核心领域模型、1.2 进程内 Plugin Registry |
| 5 | 1.3 Bootrec Importer、1.4 Journal Archive Importer、1.5 统一时间线查询与回放引擎 |
| 6 | 1.6 数据导入与时间线查询 API |
| 7 | 2.1 Systemd Service Lifecycle Analyzer、2.2 OOM 与进程异常退出分析、3.1 SSH 目标机器管理与连接 |
| 8 | 3.2 远程采集任务状态机、4.2 启动过程统一时间线回放界面 |
| 9 | 3.3 远程采集环境准备与回滚、4.1 机器/采集任务/诊断记录管理界面 |
| 10 | 3.4 远程系统资源采集、3.5 远程 Journal 采集、3.6 LightDM 临时自动登录与桌面会话检测 |
| 11 | 3.7 远程采集数据打包与可靠回传、4.3 服务状态重建与异常诊断视图 |
| 12 | 5.1 v1 集成测试与验收 |

这个顺序决定了目录里哪些位置现在还是空的：`domain/`、`storage/`、`collectors/`、
`importers/`、`analyzers/` 目前只有模块说明，填内容的是上表对应任务。

## 9. 与 23 项拆分的冲突

无。本任务没有改动父 issue 的 23 项拆分，也没有改动 stage 分组。

## 10. 偏差清单

与计划相比的实际偏差，逐条记录：

1. 依赖安装方式：handoff 只要求「凭据不进仓库、本地依赖用 Podman 起」，未指定 Python 工具链。
   实际选择 uv，并要求开发机安装 uv（macOS/Linux 用 `curl -LsSf https://astral.sh/uv/install.sh | sh`
   或 `brew install uv`）。CI 用 `astral-sh/setup-uv` 自动安装，无需人工。
2. 本任务的代码直接推送到 `main`，没有分支与 PR：仓库此前为空，没有可做 PR 基线的分支。
   这是 PL 在 handoff 里已经作出的决定。后续任务恢复「一个任务一个分支一个 PR」。
3. Playwright 与 Apache ECharts：Playwright 配置与冒烟用例已进仓库，但浏览器二进制需要在执行
   `npm run test:e2e` 前用 `npx playwright install chromium` 安装，本机与 CI 都不预装；
   ECharts 在骨架里没有图表，未安装，由任务 4.2、4.3 引入。
4. `multica repo checkout` 对空仓库报「bare cache 没有可用引用」，改用 `git clone` 检出。
   属于工具对空仓库的限制，不影响交付内容。
