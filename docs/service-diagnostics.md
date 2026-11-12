# 服务状态重建与异常诊断视图

本文说明任务 4.3（MKACG-783）交付的诊断读侧与界面：在时间线上选定一次启动和一个服务后，按游标
重建该服务当时的状态，展示生命周期、进程身份、关联 Journal、资源采样，以及带原始证据的分析结论。

- 页面入口：`frontend/src/app/diagnostics/page.tsx`（Server Component）。
- 交互组件：`frontend/src/components/diagnostics/diagnostics-view.tsx`（Client Component）。
- 后端读侧：`backend/src/system_analyzer/diagnostics/`。
- HTTP 路由：`backend/src/system_analyzer/api/routers/diagnostics.py`。

界面不重新分析 Journal，也不修改 2.1 / 2.2 的判定与结论文本；它只读取已有记录、按游标定位，
并把缺失信息写成「无法确定」。

## 1. 页面入口

| 地址                                                                 | 行为                                                     |
| -------------------------------------------------------------------- | -------------------------------------------------------- |
| `/diagnostics`                                                       | 按「机器 + 启动」列出已经导入的采集，选择后进入诊断视图  |
| `/diagnostics?machine_id=<id>&boot_id=<id>`                          | 打开这次启动，默认选第一个服务                           |
| `/diagnostics?machine_id=<id>&boot_id=<id>&entity=service:<unit>`    | 直接打开指定服务                                         |
| `/diagnostics?machine_id=<id>&boot_id=<id>&entity=service:<unit>&at_ns=<纳秒>` | 同时指定初始游标                               |

时间线页面与服务诊断页面互相有链接：时间线页头的「服务诊断」进入当前启动的诊断视图，诊断页头的
「回到时间线」返回同一台机器、同一次启动的时间线。

## 2. 读接口

两个接口都是只读，挂在 `/api` 下：

| 用途           | 接口                                                                                       |
| -------------- | ------------------------------------------------------------------------------------------ |
| 服务列表       | `GET /api/machines/{machine_id}/boots/{boot_id}/diagnostics/services`                      |
| 指定时刻诊断   | `GET /api/machines/{machine_id}/boots/{boot_id}/diagnostics/services/{entity_id}`          |

第二个接口的参数：

- `at_ns`：boot 内纳秒游标。不传时使用这次启动最早的时刻。
- `journal_window_ns`：关联 Journal 在游标前后各取多长，默认 60 秒，最大 1 小时。

`entity_id` 使用 `service:<unit>` 形式，例如 `service:nginx.service`。

响应字段与 `diagnostics/models.py` 一一对应，主要包含：

- `state`：`activating` / `active` / `inactive` / `failed` / `unknown`，以及状态起始时刻与说明。
- `identity`：PID、Invocation ID、cgroup；每项都带来源记录号，缺失时为 `null` 并附「无法确定」说明。
- `lifecycle`：2.1 写下的 `service.activating` 与 `service.active` 区间。
- `journal`：游标前后窗口内的服务事件，以及结论证据引用的原始记录。
- `resources`：游标之前最近的资源采样、采样曲线与不可用资源的原因。
- `annotations`：分析结论，包含置信度、原始证据记录号、证据正文与证据是否缺失。
- `uncertain`：本次查询里所有无法确定的信息及原因。

## 3. 状态重建规则

状态只由生命周期区间和游标位置决定：

- 游标落在 `service.activating` 区间内：`activating`。
- 游标落在 `service.active` 区间内：`active`。
- 游标在区间结束之后：使用该区间记录的终态 `inactive` 或 `failed`。
- 游标之前没有生命周期记录：`unknown`，并说明「无法确定」。
- 区间没有声明结束、且游标超过已有数据末尾：状态标成 `stale`，不把采集结束当成服务停止。

已结束区间的 `payload.state` 是终态，不能把它当成整个区间内的状态；诊断读侧按区间开始与结束时刻
重新定位，这正是「按游标重建」与直接读记录字段的区别。

## 4. 结论、证据与「无法确定」

- 结论正文、置信度、判定规则全部来自 2.1 / 2.2 写下的 annotation，诊断读侧不改写。
- 每条结论列出 `evidence_refs` 指向的原始记录；记录在库里时显示时间与正文，点击后跳到对应 Journal
  条目并高亮。记录不在库里时显示「无法确定：记录 … 不在库里」。
- PID、Invocation ID、cgroup 先取生命周期区间上的属性（2.1 分析器已经按发送方判断过），再取游标之前
  相关 Journal 的字段；两者都没有时返回 `null`，界面显示「无法确定」，不给出猜测值。
- Journal 里的 `_PID`、`_SYSTEMD_CGROUP`、`_SYSTEMD_INVOCATION_ID` 描述「谁写了这条日志」。systemd
  由 PID 1 打印生命周期消息时，这些字段是 `1`、`/init.scope` 与 systemd 自己的激活编号，不属于被操作
  的服务。只有发送方 Unit 就是被诊断服务时才采用这些字段；`INVOCATION_ID` / `USER_INVOCATION_ID`
  是被操作 Unit 写在消息里的字段，不受发送方限制。这条规则与 2.1 分析的归属判断一致，避免把
  `init.scope` 的 cgroup 和资源采样显示到目标服务上。
- 没有资源采样时不画 0：返回 `resources.note`，界面显示「无法确定资源使用」或具体原因。

## 5. 分析链路

导入任务成功后会入队一个 `analysis.run` 任务（`worker/import_jobs.py`）。Worker 的
`default_handlers()` 注册了分析处理器（`worker/analysis_jobs.py`），处理器：

1. 按 payload 里的 `machine_id` 与 `boot_id` 建立同步数据入口；
2. 经 `PluginRegistry.invoke` 依次执行 `systemd.lifecycle` 与 `oom.analyzer`；
3. 分析器通过 `ensure_entities` 补齐服务实体，再通过 `append_records` 写回区间与结论。

`DiagnosticStore.ensure_entities` 只向已有采集所属的启动补实体，唯一键冲突时跳过，重复分析不会重复
写实体；记录写入仍由原有 `append_records` 的幂等规则负责。

## 6. 验证

```bash
cd backend
uv run ruff check . && uv run ruff format --check . && uv run pytest
scripts/dev-db.sh start
cp -n .env.example .env && uv run alembic upgrade head
uv run pytest -m integration
uv run alembic heads

cd ../frontend
npm ci
cd ../backend && uv run python -m system_analyzer.tools.export_openapi
cd ../frontend
npm run api:generate
npm run lint && npm run typecheck && npm test && npm run build
```

端到端验证使用真实 PostgreSQL、真实 Worker 与真实页面：上传 Journal 归档，导入任务成功后 Worker
领取 `analysis.run`，分析结论写回数据库，再由诊断页面读回。两个场景的截图作为任务交付附件。

## 7. 已知限制

- Journal-only 归档没有 CPU、内存、磁盘 I/O 采样，诊断页会明确显示资源不可用，不伪造 0。
- 关联 Journal 默认只取游标前后各 60 秒；需要更多上下文时通过 `journal_window_ns` 扩大窗口。
- 诊断读侧只认 `service:<unit>` 实体；其它实体不会出现在服务选择器里。
