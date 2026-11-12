# 机器、采集任务与诊断记录管理界面

本文说明任务 4.1 交付的管理界面：页面、它们消费的接口、失败原因的来源，以及本版明确的边界。
时间线的展示规则见 `docs/timeline-ui.md`，导入与查询接口见 `docs/import-and-query-api.md`，
采集任务的状态机见 `docs/collection-orchestration.md`；本文不重复定义它们。

## 1. 页面与接口

| 页面 | 做什么 | 消费的接口 |
| --- | --- | --- |
| `/machines` | 机器列表、新增机器 | `GET /api/targets`、`POST /api/targets` |
| `/machines/{target_id}` | 配置、连接测试、编辑、删除、这台机器的采集任务 | `GET/PUT/DELETE /api/targets/{id}`、`POST /api/targets/{id}/test-connection`、`GET /api/collection-tasks?target_id=` |
| `/tasks` | 创建采集任务、看状态变化、取消 | `GET/POST /api/collection-tasks`、`POST /api/collection-tasks/{id}/cancel` |
| `/imports` | 上传 Journal 压缩包或 `.bootrec`，看上传、排队、执行与结果 | `POST /api/imports`、`GET /api/imports/{job_id}` |
| `/collections` | 已导入的采集与导入状态 | `GET /api/collections` |

页面分工与父 issue 的职责边界一致：普通页面是 Server Components（首屏数据在服务端取），
需要文件选择、轮询、取消这类交互的部分是 Client Components。前端不通过 Route Handlers
重写后端业务，`next.config.ts` 的同源转发只把字节交给 FastAPI。

## 2. 两套「采集」不是一回事

- **目标机器**（`ssh_targets`）是运维配置：host、port、username、口令变量名。界面上的
  「机器」指它，可以新增、编辑、删除。
- **采集**（`collections`）是导入出来的诊断数据，只读。界面上的「诊断记录」指它。
- **采集任务**（`jobs` 里 `kind = collect.target`）是让 Worker 去目标机器采一次的请求。

三者在本版没有关联字段：`ssh_targets` 与 `machines` 之间没有外键，所以机器详情页只展示
配置、连接测试结果与这台机器的采集任务，不展示导入出来的诊断数据。把两者挂起来需要先加
关联列与迁移，不在本版范围内。

## 3. 失败原因从哪里来

界面不自己拼失败文案，一律显示后端返回的字段：

| 场景 | 字段 |
| --- | --- |
| 连接测试失败 | `failure.kind`（`connect` / `auth` / `timeout` / `credential`）+ `failure.message` |
| 采集任务失败 | `failure.phase` + `failure.message`（来自编排写下的阶段说明） |
| 取消与中断 | `latest_attempt.status` 为 `cancelled` / `interrupted`，界面给出单独的说明，不算失败 |
| 导入失败 | `job.failure.kind`（`invalid_input` / `internal`）+ `job.failure.message` |
| 采集导入状态失败 | `import_error` |

`jobs.last_error` 带异常原文（可能包含文件路径或 SQL 片段），**不对外暴露**；只有采集记录里
没有写下原因时才给一句固定说明，并指向 Worker 日志。

## 4. 采集任务的状态与操作记录

状态只有 `queued` → `running` → `succeeded` / `failed` 四种，取自 `jobs.status`。失败后按任务行
自己的 `max_attempts` 重试，重试期间状态回到 `queued`，所以「任务还在重试」与「重试耗尽」要
看 `attempts / max_attempts`，不能只看状态。

任务进行中页面每 2 秒刷新一次列表，全部结束就停止轮询。每次尝试的阶段状态与操作记录
（阶段开始、步骤完成、失败原因）都能展开查看，它们是 Worker 写回 `payload.result.collection`
的记录，不是前端推断的。

取消是把 `payload.cancel_requested` 置为 true，只对排队中与执行中的任务有效（其他状态返回
`409`）。Worker 在两个步骤之间读一次标记，所以取消不是立即生效；取消后任务不算失败，最终
结果仍以采集记录为准。

## 5. 空状态

| 页面 | 空状态文案 |
| --- | --- |
| `/machines` | 「还没有机器」+ 提示填写上方表单 |
| `/tasks` | 「还没有采集任务」+ 说明状态会怎么变化；没有机器时提示先去新增机器 |
| `/imports` | 「还没有选择文件」 |
| `/collections` | 「还没有诊断记录」+ 指向导入页 |

空状态不报错、不显示假数据；读取失败是另一种面板（红色），区分「没有数据」与「读不到」。

## 6. 本版边界

- **连接状态不落库**：列表里统一显示「未测试」，详情页点过测试才显示结果，刷新后回到未测试。
  持久化需要给 `ssh_targets` 加列并做迁移，本版不做。
- **没有 `/api/machines` 列表接口**：本版的「机器列表」是目标机器列表；诊断数据按机器的
  查询走 `/api/collections` 与时间线接口。
- **采集任务只做管理**：真正对目标机器做什么由 3.4–3.7 的步骤填入，本版不新增采集动作。
- **口令不在界面上填写**：库里只存变量名，口令由 API 进程的运行环境提供。

## 7. 本地跑起来

```bash
scripts/dev-db.sh start
cd backend && cp -n .env.example .env && uv run alembic upgrade head
uv run sas-api           # 终端一
uv run sas-worker        # 终端二
cd ../frontend && npm run dev
```

含 `SAS_SSH_PASSWORD` 的进程才能对真实机器做连接测试；没有配置时连接测试返回
`kind = credential`，界面会显示「服务端未配置口令」。

端到端测试用 `e2e/mock-api` 的假后端（状态化：采集任务与导入任务每次被读取就往前走一步），
覆盖新增机器 → 测试连接 → 上传 → 导入记录、采集任务的状态变化与取消、四类连接失败与空状态。
