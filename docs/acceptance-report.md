# system-analyzer-service 1.0 集成测试与验收报告

对应 issue：MKACG-784（5.1 完成 system-analyzer-service v1 集成测试与验收）。
验收日期：2026-11-14。
范围：本 issue 正文的 6 条验收标准，以及任务交接里点名的 7 条已知缺口。

## 1. 验收环境

| 项目 | 实际配置 |
| --- | --- |
| 后端 | Python 3.12，uv 0.13.7 |
| 数据库 | PostgreSQL 17，Podman 容器 `sas-postgres`，`127.0.0.1:55432` |
| 前端 | Node.js 25.8.2，Next.js 16.4.6 |
| 验证机 | `uos@10.20.50.152`，UOS Desktop 25 Professional |

仓库 CI 固定 Node.js 22；Node.js 27 预发布版不能用于 `next start`，原因见第 6 节。

## 2. 一条命令跑完验收

```bash
./scripts/acceptance.sh
```

脚本依次执行：启动本地 PostgreSQL、安装依赖、执行数据库迁移、检查迁移只有一个 head、
后端格式检查与静态检查、后端单元测试、集成测试、验收用例、前端 lint / 类型检查 / 格式检查 /
单元测试 / 构建、Playwright 界面回放。`--skip-e2e` 跳过 Playwright，`--with-real-machine`
追加验证机用例。

2026-11-14 03:2x 实际执行到最后一行为 `==> 验收全部通过`，退出码 0。

## 3. 验收标准逐条结论

| 验收标准 | 结论 |
| --- | --- |
| AC-1 核心数据链路有自动化测试覆盖，一条命令可跑 | 通过 |
| AC-2 数据格式与时间线关联符合 0.2、0.3 的规范 | 通过 |
| AC-3 异常任务不遗留未恢复的远程配置，以验证机的配置 diff 为证 | 通过 |
| AC-4 网络中断不丢失已采集数据，有记录为证 | 通过 |
| AC-5 Journal-only 与 SSH 采集两条闭环都能在界面回放 | 通过 |
| AC-6 按部署文档能在干净环境启动服务并完成一次导入 | 通过 |

### AC-1 核心数据链路有自动化测试覆盖，一条命令可跑

新增的验收用例：

- `backend/tests/test_acceptance_bootrec_chain.py`：四份仓库样例经真实 Worker 导入，检查时间线
  顺序、实体引用、三种资源状态与 Journal-only 无资源轨道。
- `backend/tests/test_acceptance_journal_chain.py`：Journal 归档导入、分析任务执行、结论入库、
  诊断视图读回，以及生命周期 Invocation ID 关联。
- `backend/tests/test_acceptance_remote_chain.py`：Mock SSH 下的命令执行、文件传输、取消、超时、
  网络中断与重新下载导入，以及「采样 → 打包 → 回传 → 导入 → 时间线」的完整采集链路。
- `backend/tests/test_acceptance_real_machine_lightdm.py`：验证机上的 LightDM 自动登录与配置恢复。

实际结果：

```
# cd backend && uv run pytest -q
775 passed, 1 skipped, 129 deselected

# uv run pytest -m integration -q
129 passed, 776 deselected

# uv run pytest -m acceptance -q
10 passed, 895 deselected
```

`1 skipped` 是没有设置验证机口令时的真机用例；设置口令后单独执行结果为
`1 passed, 879 deselected`。

前端：

```
# cd frontend
npm run lint / typecheck / format:check   通过
npm run test                              115 passed（9 个测试文件）
npm run build                             成功
npm run test:e2e                          15 passed
```

### AC-2 数据格式与时间线关联符合 0.2、0.3 的规范

`test_acceptance_bootrec_chain.py` 对四份样例逐份断言：

- 时间线条目按 `monotonic_ns` 升序返回；
- 条目引用的 `service:` / `metric:` 实体都能在 `entities` 表读到；
- 资源状态 `available` / `unavailable` / `no_data` 都能用仓库样例复现，缺失没有写成 0；
- Journal-only 样例没有 metric 轨道，整次启动 `resources.available = false` 并给出原因。

样例由 `python -m system_analyzer.tools.make_bootrec_samples` 生成，`--check` 在仓库中通过，
说明四份样例与生成器一致。

### AC-3 异常任务不遗留未恢复的远程配置，以验证机的配置 diff 为证

Mock SSH 用例（`test_acceptance_remote_chain.py`）：

- 正常流程执行了远程命令、上传与下载，恢复后远端配置文件逐字节回到原值；
- 取消与超时都执行 Post 阶段，临时 LightDM 片段被删除，主配置不变；
- 回传失败时先恢复环境，再抛出原始错误。

验证机实测（`uv run pytest -m real_machine -s -q`，`1 passed`）：

```
before:
  config_sha256 = 7ae60d8fa4fe8e35e915155120e0b6179c26074c2d6507466abb2785acc39f0b
  config_mode   = 644 0 0
  fragments     = []
during:
  fragments     = ["99-system-analyzer-autologin.conf"]
  fragment_sha256 = fe4500337c53a8bf517c9d659ccb3d244027c922d377c151a492657514ab7db6
  session       = {id: 1278, user: uos, seat: seat0, type: x11, class: user,
                   active: true, state: active, remote: false}
after:
  config_sha256 = 7ae60d8fa4fe8e35e915155120e0b6179c26074c2d6507466abb2785acc39f0b
  config_mode   = 644 0 0
  fragments     = []
staging: staging_exists=1
```

`staging_exists=1` 是 `test -d <暂存目录>` 的退出码，表示目录已删除。验证机的
`/etc/lightdm/lightdm.conf.d` 原本是空目录，采集期间只多出临时片段，结束后恢复为空。

### AC-4 网络中断不丢失已采集数据，有记录为证

`test_network_interruption_keeps_data_and_redownloads_before_import` 用会中途断开的假下载器：

1. 第一次下载只写入 64 字节就断开；本地已有的旧包保持原内容，没有被半截文件覆盖；
2. 远端文件没有删除；
3. 第二次下载完整成功，随后导入写入记录；
4. 导入成功后删除远端文件。

断言结果：`[attempt.succeeded] == [False, True]`，导入后 `import_status == "imported"`，
`records_written > 0`。

### AC-5 Journal-only 与 SSH 采集两条闭环都能在界面回放

**Journal-only：通过。**

- `test_acceptance_journal_chain.py` 用真实 Journal 归档走完导入 → 分析任务 → 结论入库 →
  诊断视图读回；
- Playwright 用例「Journal-only 数据可以回放，资源轨道显示不可用与原因」验证界面渲染；
- 干净部署后，时间线与诊断页面在真实数据下返回 200，并包含 `nginx` 文本。

**SSH 采集：通过。**

- `backend/src/system_analyzer/collection/sampling.py` 在 In Progress 阶段调用同步插件
  `resource-sampler` 与 `journal.remote`：用 `asyncio.to_thread` 放到 Worker 工作线程执行，
  避免阻塞事件循环。两个采集器拿到同一套 `machine_id`、`boot_id`、`collection_id`。
- 采集记录写在本次采集临时目录的 `sampling-records.jsonl`，Journal 导出文件写在同目录的
  `journal/` 下。Post 阶段的 `_load_source` 直接读取这两个产物，不要求先向存储层写一条
  「已导入」的采集，因此不需要修改 `DiagnosticStore.append_records` 的写入契约。
- 任务没有配置 `options.delivery` 时只记录「跳过」，不占用目标机器。
- 采集任务导入成功后自动入队 `analysis.run`（`collection/jobs.py`），与 `POST /api/imports`
  的导入路径一致。入队按 `collection_id` 去重：同一份采集重试时复用已有分析任务，不会重复
  分析。入队失败只写进任务结果的 `analysis_enqueue_error`，不把已经成功的采集判成失败。
- `delivery` 只给 `remote_path` 时默认先上传本地产出的包再下载。显式传
  `stage_local_pack=false` 才表示远端已经有包；这修掉了「下载一个从未上传的文件」的失败。
- 自动化用例：`test_ssh_collection_produces_bootrec_with_resources_and_journal` 用假采集插件
  与真 PostgreSQL 走完 `default_plan()`，断言任务成功、远端包被下载并导入、存储里有 Metric
  与 Journal 事件记录、时间线有资源轨道、包内 `journal_entries > 0`。
- 真机复验（`uos@10.20.50.152`，不启用桌面自动登录、不修改配置）：三阶段全部 `succeeded`，
  `delivery.status = imported`，`records_written = 72495`，落库记录为
  `event = 59787`、`metric = 12707`、`artifact = 1`；实体 2871 条，其中 service 实体 2279 条。
  远端包在导入成功后删除。
- 真实数据规模下的接口与页面实测（库 `sas_validator_784`，机器 `machine:uos-PC`，启动
  `abc7451f-65c8-4be8-ab01-4b9a1e49a8b9`）：

  | 项目 | 修复前 | 修复后 |
  | --- | --- | --- |
  | `/diagnostics/services` | 300 秒未返回，API 进程 CPU 99.5% | 0.32 秒返回 2279 个服务 |
  | 诊断请求期间 `/api/health` | 5 秒超时 | 最长 0.012 秒 |
  | `/timeline/tracks`（末尾 60 秒） | 9.84 秒，270,555,527 字节 | 1.41 秒，15,685,496 字节 |
  | `/timeline` 服务端渲染 | 30.97 秒，351,643,482 字节 | 8.98 秒，18,297,109 字节 |
  | 页面首次可交互 | 137.4 秒 | 3.79 秒 |
  | DOM 节点数 | 563,424 | 12,999 |

  时间线页面先读游标拿轴范围，再只请求末尾 60 秒且 `include_attributes=false`；每个分组默认
  渲染 20 条轨道，「关键事件」最多列出 200 条。服务列表只读 `span` 与 `annotation` 记录，
  记录汇总在 Worker 线程执行。
- 该次真机采集的 Journal 里没有 systemd 的 `Started` / `Stopped` 生命周期消息（只有
  `Failed with result` 与依赖失败三类消息），分析器按现有规则写回 256 条结论，没有可配对的
  生命周期区间。这是采集到的 Journal 内容决定的，不是分析任务没有执行：`jobs` 表里
  `analysis.run` 状态为 `succeeded`，结果记录为
  `{"analyzers": {"oom.analyzer": 0, "systemd.lifecycle": 256}}`。区间重建规则本身由
  `test_systemd_lifecycle_analysis.py` 与验收用例中的固定 Journal 覆盖。

### AC-6 按部署文档能在干净环境启动服务并完成一次导入

按 `docs/deployment.md` 在一个新建的空库 `sas_acceptance` 上执行：

```
alembic upgrade head
  0001_jobs -> 0002_diagnostic_data -> 0003_ssh_targets

GET /api/health
  {"status":"ok","version":"0.1.0"}

POST /api/imports  (full.bootrec)
  job.status = succeeded
  result.records_written = 17
  result.import_status = imported
  collection_id = collection:full-0001

GET /api/machines/machine:uos-PC/boots/3f1c9a52-.../timeline/tracks
  items = 14, tracks = 7, resources.available = true

GET /api/machines/machine:uos-PC/boots/3f1c9a52-.../diagnostics/services
  services = 2

上传目录
  只有 1 个实际文件（3425 字节），没有残留临时文件
```

前端按部署文档构建并用 `next start` 启动后，`/`、`/collections`、`/imports`、`/timeline`、
`/diagnostics` 都返回 200；带本次启动参数的时间线与诊断页面都能读到 `nginx` 文本。

## 4. 七条已知缺口结论

| # | 缺口 | 结论 | 证据 |
| --- | --- | --- | --- |
| 1 | `import_error` 可能透出存储层异常原文 | 已修 | 新增固定文案 `INTERNAL_IMPORT_FAILURE_MESSAGE`，异常详情只进日志；集成测试断言错误原文不再出现 |
| 2 | Worker 运行循环遇数据库抖动直接退出 | 已修 | `run_forever` 捕获单轮异常后等待下一轮；新增测试注入一次连接错误，主循环继续领取任务 |
| 3 | 分析链路没接线 | 已复验 | 导入后自动入队分析任务，Worker 执行成功，OOM 与生命周期结论入库并在诊断视图可读 |
| 4 | 内核 OOM 依赖 `_TRANSPORT` / `SYSLOG_IDENTIFIER` | 已复验 | 打包 → 解包 → 导入后六个原始字段仍在，`detect_kill()` 仍识别为内核 OOM |
| 5 | `analyzers/oom.py` 重复赋值 | 已清 | 删除重复的 `_KERNEL_KILL.search` 赋值 |
| 6 | 墙钟文字使用 boot 级锚点 | 按 lxz 决定接受 | 1.0 不改 1.5；per-collection 锚点留后续版本，限制写进 `docs/timeline-query.md` |
| 7 | `no_data` 状态没有真实样例 | 已补 | 新增 `docs/samples/bootrec-v1/no-data.bootrec`；导入后 `metric:load.1min` 轨道状态为 `no_data` |

补充说明：

- 第 3 条的四段链路都有实际输出：导入任务 `succeeded`、分析任务 `succeeded`、结论记录能从
  `records` 表读到、诊断视图返回带证据的结论。
- 第 4 条的断言在 `backend/tests/test_collection_packaging.py`，覆盖六个原始字段与
  `detect_kill()` 的完整返回值。
- 第 5 条的改动只有删除重复行，没有改变检测逻辑。

## 5. 新发现的缺口

### 5.1 SSH 采集的 In Progress 步骤没有接上采集器（已修复）

初版只交付了编排骨架，In Progress 的三步是空函数，采集任务因此得到 `delivery.status = no_data`。
本次返工新增 `collection/sampling.py`，在 In Progress 调用资源与 Journal 采集器，把记录与
Journal 导出文件交给 Post 打包；没有修改存储层的写入契约。验证证据见 AC-5。

### 5.2 采集管理界面不发送采集选项

`frontend/src/lib/api/client.ts:201` 的 `createCollectionTask(targetId)` 只发送 `target_id`。
本任务补上了后端接口的 `options` 字段并透传给 Worker（`backend/src/system_analyzer/api/schemas.py:541`、
`backend/src/system_analyzer/api/routers/collection_tasks.py`），但界面还没有填写 LightDM 用户、
备份路径与回传开关的入口。即使 5.1 修好，界面创建的任务仍然不会打开这些选项。

这一项需要改采集管理界面，超出「只修验收暴露的问题」的范围，建议另开后续 issue。

### 5.3 SSH 采集导入后没有排上分析任务（已修复）

`POST /api/imports` 的导入任务成功后会把 `analysis.run` 写进队列，采集任务内部的导入路径没有。
真实采集因此只有 event 与 metric 记录，没有生命周期区间与 OOM 结论。

修复在 `backend/src/system_analyzer/collection/jobs.py`：采集尝试结束后，如果
`delivery.status` 是 `imported` 或 `already_imported`，按采集记录里的 `hostname` 与 `boot_id`
入队一次 `analysis.run`；`enqueue_analysis_job_once()` 先按 `collection_id` 查已有任务，有就复用。
入队异常只写 `analysis_enqueue_error`，不改变采集任务的成功状态。单元测试
`test_collection_jobs.py` 覆盖成功入队、失败不入队与异常记录；集成测试
`test_collection_jobs_db.py` 覆盖同一采集只产生一个分析任务。

### 5.4 服务诊断列表按服务数乘记录数扫描（已修复）

`diagnostics/engine.py` 原先为每个服务调用一次 `_summary(entity_id, records)`，每次都全量扫描
全部记录。真实数据 2279 个服务、72495 条记录时请求超过 300 秒，并占满 API 进程。

改为一次遍历记录并按实体累计起止与结论数；服务列表只读 `span` 与 `annotation` 记录，不再读
事件与采样点；汇总放到 Worker 线程执行。实测 0.32 秒，期间 `/api/health` 最长 0.012 秒。
覆盖用例为 `test_services_list_accumulates_spans_and_annotations_in_one_pass` 与两个
`_runs_off_the_event_loop` 用例。

### 5.5 时间线响应体与页面节点随采样时长膨胀（已修复）

`/timeline/tracks` 返回整条轴，事件条目还带整份 Journal 原始字段；界面把每条轨道与每条事件都
渲染成 DOM，137.4 秒才能交互。

修复分三层：后端增加 `include_attributes=false` 与 `(machine_id, boot_id)` 的 10 秒轴缓存；
页面首屏只请求轴末尾 60 秒；界面每个分组最多渲染 20 条有数据的轨道，「关键事件」最多 200 条。
实测页面首次可交互 3.79 秒，DOM 节点 12,999 个。覆盖用例在 `test_timeline.py`、
`scale.test.ts` 与 `timeline-replay.test.tsx`。

### 5.6 `delivery` 只给 `remote_path` 时下载从未上传的文件（已修复）

`stage_local_pack` 原来默认 `false`，只给 `remote_path` 时 Post 阶段直接下载，三次尝试都是
`SFTPNoSuchFile`。现在默认先上传本地产出的包，显式传 `false` 才表示远端已有包。单元测试
`test_collection_delivery.py` 覆盖默认上传与显式跳过上传两条路径。

## 6. 环境限制

Node.js 27.0.0-alpha 下 `next start` 在服务端渲染时报 `e.hasOwnProperty is not a function`，
所有页面返回 500。Node.js 25.8.2 与 CI 使用的 Node.js 22 正常。这不是产品缺陷，已写进
`docs/deployment.md` 的前置条件与故障排查表。

## 7. 偏差清单

- `POST /api/collection-tasks` 新增可选字段 `options`，只接受白名单字段，其余键在接口处丢弃；
  旧客户端不传时按空对象处理，不改变原行为。
- 新增第四份样例 `no-data.bootrec`；`docs/bootrec-v1.md`、`docs/timeline-query.md`、
  `docs/timeline-ui.md` 从三份样例更新为四份。
- 新增 `scripts/acceptance.sh`；`backend/pyproject.toml` 增加 `acceptance` 与 `real_machine` 两个
  测试标记。
- `frontend/e2e/timeline.spec.ts` 的启动链接选择器限定为 `/timeline?` 开头，避免同时匹配诊断
  页面的链接；只改测试。
- `INTERNAL_IMPORT_FAILURE_MESSAGE` 从 `storage` 包导出，供集成测试断言。

其他没有偏差。

## 8. 复现命令

```bash
# 全部验收（含 Playwright）
./scripts/acceptance.sh

# 只跑后端验收用例
cd backend
SAS_TEST_DATABASE_URL='postgresql+asyncpg://sas:sas@127.0.0.1:55432/system_analyzer' \
  uv run pytest -m acceptance -q

# 验证机
SAS_REAL_MACHINE_HOST=10.20.50.152 \
SAS_REAL_MACHINE_USER=uos \
SAS_REAL_MACHINE_PASSWORD='<验证机口令>' \
  uv run pytest -m real_machine -s -q
```
