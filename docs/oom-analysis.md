# OOM 与进程异常退出分析

本文档是任务 2.2（issue MKACG-773）的交付物，说明 `analyzers/oom.py` 里那个分析插件认什么、
怎么关联、怎么区分「直接证据」与「只靠时间接近的疑似关联」。

- 任务依据：issue MKACG-773 正文与 Task Handoff。
- 上游契约：`docs/plugin-contracts.md` 第 4 节（`AnalyzerPlugin.analyze` 是**同步**方法）、
  第 5 节（插件只通过 `PluginDataAccess` 读写数据）、第 7 节（新增插件必须做的三件事）；
  `docs/data-model.md` 第 4.1 节（Event）、第 4.5 节（Annotation）；
  `docs/time-base.md` 第 3 节（`monotonic_ns` 零点与跨启动禁止项）、第 7 节（排序键）。
- 代码位置：`backend/src/system_analyzer/analyzers/oom.py`（规则与插件）、
  `backend/tests/test_oom_analyzer.py`（规则与契约）、
  `backend/tests/integration/test_oom_analysis_flow.py`（真实 PostgreSQL 上的端到端）、
  `backend/tests/fixtures/oom/journal.export`（两类 OOM 的固定 fixture）。
- 代码与本文档不一致时先查代码，再立刻修正本文档。

目录：

1. 输入：读哪些记录
2. 识别：两类 OOM 的原文规则
3. 提取：PID、进程名、Unit、时间
4. 关联：进程、Unit、服务实体
5. 直接证据与疑似关联
6. 数据入口与调用链
7. 配置
8. 幂等与可重复
9. 验证方式
10. 与其它任务的边界
11. 偏差清单

## 1. 输入：读哪些记录

插件只读 `event` 记录：`context.data.query_records(record_kinds=("event",))`。这些记录由 1.4 的
Journal 导入写好，`payload.attributes.derived_from = "journal"`、
`payload.event_type = "journal.entry"`，原始 Journal 字段留在 `payload.attributes.fields`。

本插件**不重新解析 Journal 压缩包**，也不改 1.4 的解析逻辑：它只从已经归一化的记录推导结论。

读字段的顺序写死两条：

1. 先取 1.4 归一化后的短名字段 `payload.attributes.unit` / `payload.attributes.identifier` /
   `payload.attributes.pid`；
2. 取不到再退回原始字段 `payload.attributes.fields._SYSTEMD_UNIT` /
   `SYSLOG_IDENTIFIER` / `_PID`。

`boot_id` 为空表示分析 Core 交给它的全部记录；非空时只分析这次启动。`record_ids` 非空时只把列出的
记录当作 OOM 证据，但找 Unit 线索时仍看整个 boot。跨 boot 的关联按 `docs/time-base.md` 第 3.2 节
禁止，插件按 `(machine_id, boot_id)` 分组后各自计算，不把一个 boot 的结论套到另一个上。

## 2. 识别：两类 OOM 的原文规则

规则是确定的文本匹配，不引入机器学习推断。

### 2.1 Kernel OOM Killer

`payload.message` 匹配下面这条正则（大小写敏感，与内核 `mm/oom_kill.c` 的输出一致）：

```text
(?:Out of memory|Memory cgroup out of memory):\s*(?:Killed|Kill) process\s+(?P<pid>\d+)\s*\((?P<process>[^)]+)\)
```

认得的原文示例（前两条来自内核 4.x/5.x/6.x，第三条是旧版写法）：

```text
Out of memory: Killed process 4310 (firefox) total-vm:2846920kB, anon-rss:1804808kB, ...
Memory cgroup out of memory: Killed process 77 (java) total-vm:1024kB
Out of memory: Kill process 1234 (postgres) score 900 or sacrifice child
```

### 2.2 systemd-oomd

两个条件同时成立才算：

1. `payload.attributes.identifier`（原始字段 `SYSLOG_IDENTIFIER`）等于 `systemd-oomd`；
2. `payload.message` 匹配 `\bKilled\s+(?P<cgroup>/\S+)`。

原文示例：

```text
Killed /user.slice/user-1000.slice/session-2.scope due to memory pressure 92.00% > 80.00% for > 10s
Killed /system.slice/tracker-extract-3.service due to memory pressure 91.50% > 80.00% for > 20s
```

oomd 的其它日志（`Monitoring ...`、`Considered 3 cgroups ...`）不含 `Killed /`，不会被当成 OOM 事实。

## 3. 提取：PID、进程名、Unit、时间

| 字段 | Kernel OOM Killer | systemd-oomd |
| --- | --- | --- |
| PID | 同一行 `MESSAGE` 里的 `<pid>` | **没有**：`Killed` 行不写 PID，结论里如实说明「不给出进程归属」 |
| 进程名 | 同一行 `MESSAGE` 里的 `(<comm>)` | **没有**，同上 |
| cgroup | 同一行的 `payload.attributes.fields._SYSTEMD_CGROUP`（有就写进结论） | 同一行 `MESSAGE` 里的 `/<cgroup>` 路径 |
| Unit | 见第 5 节：内核那一行通常没有 Unit 字段，只能按时间接近推断，属于疑似 | cgroup 路径最后一段，例如 `/system.slice/nginx.service` → `nginx.service` |
| 时间 | 该 `event` 记录的 `monotonic_ns` / `realtime_ns` / `clock_domain` 原样带到结论上 | 同左 |

时间字段不重算、不平移：结论的 `monotonic_ns`、`realtime_ns`、`clock_domain` 与证据记录逐字段相同，
断言见集成测试 `test_annotations_follow_the_documented_time_axis`。

## 4. 关联：进程、Unit、服务实体

关联只有一条线索：记录自己。插件不查数据库表、不读别的插件的运行状态，只用
`context.data.query_records` 返回的记录。

| 关联对象 | 怎么得出 | 写在结论的哪里 |
| --- | --- | --- |
| 进程 | 同一行 `MESSAGE` 的 PID 与进程名 | `payload.message`（写明「PID x 与进程名 y 由 payload.message 同一行给出」） |
| Unit | 同一行日志里的 cgroup / `_SYSTEMD_UNIT`，或时间窗口内结果为 `oom-kill` 的 Unit 失败日志 | `payload.message`（写明字段名或「跨日志，仅时间接近」） |
| 服务实体 | Unit 解析出来后，若同一 boot 的记录里有记录引用 `service:<unit>`，就把结论的 `entity` 指向它 | 记录的 `entity` |

为什么用「有记录引用 `service:<unit>`」判断实体存在：`PluginDataAccess` 只给记录，不给实体表；
而记录上的 `entity` 就是实体存在过的证据（1.4 把 `_SYSTEMD_UNIT` 落成 `service` 实体后，记录会引用它）。
实体不存在时结论的 `entity` 是 `null`，并在 `payload.message` 里说明「库里没有 `service:<unit>` 实体」，
不硬造一个指向空实体的引用——那样写入会被存储层的实体校验拒绝。

`payload` 里没有 `attributes` 字段可用（`docs/data-model.md` 第 4.5 节只定义了
`annotation_type`、`analyzer`、`severity`、`message`、`confidence`、`evidence_refs`），所以
「哪个字段、哪条日志」全部写进 `message`，机器可读的部分放在 `annotation_type`、
`evidence_refs`、`confidence`、`entity` 上。这样不用给数据模型加字段。

## 5. 直接证据与疑似关联

这是本任务的核心要求：**只靠时间接近的关联必须标成疑似，不得写成因果。**

| 情形 | 判定 | `confidence` | `message` 的写法 |
| --- | --- | --- | --- |
| 关联对象写在同一行日志里（systemd-oomd 的 cgroup → Unit；内核 OOM 的 PID/进程名） | 直接证据 | `0.95` | 写出字段名，例如「Unit nginx.service 由payload.message给出」 |
| 内核 OOM 那一行没有任何 Unit 线索，靠同一 boot 内时间接近的失败日志补出来 | **疑似** | `0.3` | 「疑似归属 Unit …」「只凭时间接近」「不能当作因果结论」，并写出两者相差多少纳秒、窗口是多少 |
| 内核 OOM 那一行没有 Unit 线索，窗口内也没有失败日志 | 只有 OOM 事实本身 | `0.9` | 「不给出 Unit 归属」 |

补 Unit 的辅助条件（三条同时成立才用）：

1. 该记录在同一个 `(machine_id, boot_id)` 内；
2. `clock_domain` 是 `boot_monotonic` 且 `monotonic_ns` 落在 `[OOM 时刻, OOM 时刻 + 窗口]`；
3. 该记录的 `payload.message` 匹配 `Failed with result 'oom-kill'`，并且它带 `unit` 字段。

取窗口内**最近**的一条。窗口默认 5 秒，可通过第 7 节的配置改。除 `oom-kill` 以外的
`Failed with result '<其它>'` 不参与关联：普通退出码失败不是 OOM 的线索，集成测试
`test_unit_failure_outside_oom_kill_result_is_not_used_as_evidence` 盯着这条。

疑似关联的 `evidence_refs` 里放下**两条** `record_id`：OOM 那一行与被推断出的失败日志。这样人工复核时
两个时间点都能直接跳到原文。

## 6. 数据入口与调用链

插件入口按契约写成同步方法：

```python
def analyze(self, request: AnalysisRequest, context: PluginContext) -> AnalysisResult
```

它只依赖 `docs/plugin-contracts.md` 第 5 节的 `PluginDataAccess`：

- `query_records(record_kinds=("event",))` 读事件；
- `append_records(annotations, plugin_id=..., source=...)` 追加结论。

两条保护，都是为了不出现「调用成功、实际什么都没写」：

1. `context.data` 不满足 `PluginDataAccess`（没有同名的同步方法）时抛 `PluginInputError`，错误信息
   写明缺什么；
2. 数据入口返回协程时同样抛 `PluginInputError`——插件不开事件循环，不同步的入口一律当配置错误。

Core 侧要提供的是**同步**数据入口。生产上存储层 `DiagnosticStore` 的方法是协程，Worker 必须像
1.3/1.4 那样把它包成同步入口：`query_records` 换成带启动范围的只读查询
（`list_records_in_boot`），`append_records` 按记录上的 `collection_id` 分组后逐组写入
（`store.append_records(collection_id, records)`）。`PluginDataAccess.append_records` 的签名里没有采集号，
所以「写到哪个采集」这个决定必须落在 Core 的数据入口实现里，插件不碰。

集成测试里的 `SyncStoreAccess` 就是这份包装的样例，链路与 Worker 将来的接法一致：
`PluginRegistry.invoke` → 插件 → 数据入口 → 存储层，结论回读库里的实际内容再断言。

## 7. 配置

| 名称 | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `proximity_window_ns` | integer ≥ 0 | `5000000000`（5 秒） | 跨日志补 Unit 时允许的时间窗口，单位纳秒。它只决定「拿哪一条日志补 Unit」，不改变「这是疑似」这个结论 |

配置由 Core 按 manifest 的 `config_schema` 校验后放进 `context.config`，插件不读环境变量与配置文件。

## 8. 幂等与可重复

- `record_id` 由记录内容算出（`docs/data-model.md` 第 8 节）：同一批输入、同一份规则，重复分析得到
  同一批 `record_id`。
- 存储层按 `(collection_id, record_id)` 做幂等写入，所以第二次分析返回
  `AnalysisResult(annotations_written=0)`，库里仍是同样那几条结论，不会叠加互相矛盾的结果
  （集成测试 `test_rerunning_the_analysis_does_not_duplicate_annotations`）。
- 结论按 `(monotonic_ns, 证据 record_id)` 排序后写出，顺序也是确定的。

## 9. 验证方式

```bash
cd backend && uv run ruff check . && uv run ruff format --check . && uv run pytest

scripts/dev-db.sh start
cd backend && cp -n .env.example .env
uv run alembic upgrade head
uv run pytest -m integration
cd .. && scripts/dev-db.sh stop
```

`backend/tests/fixtures/oom/journal.export` 是两类 OOM 的固定 fixture（`journalctl -o export` 文本）：

| 序号 | 时间（`monotonic_ns`） | 内容 | 期望的结论 |
| --- | --- | --- | --- |
| 1 | 240000000000 | `Starting Tracker metadata extractor...`，`_SYSTEMD_UNIT=tracker-extract-3.service` | 不产生结论，但提供 `service:tracker-extract-3.service` 实体 |
| 2 | 240100000000 | `Started Firefox Web Browser.`，`_SYSTEMD_UNIT=firefox.service` | 不产生结论，但提供 `service:firefox.service` 实体 |
| 3 | 240500000000 | `Out of memory: Killed process 4310 (firefox) ...`，`_TRANSPORT=kernel` | 一条 `oom.kernel.killed_process`：PID 4310、进程名 firefox、时间 240500000000 |
| 4 | 240530000000 | `firefox.service: Failed with result 'oom-kill'.` | 作为第 3 条的疑似 Unit 线索（相差 30 毫秒），不单独出结论 |
| 5 | 241400000000 | `Killed /system.slice/tracker-extract-3.service due to memory pressure ...`，`SYSLOG_IDENTIFIER=systemd-oomd` | 一条 `oom.systemd_oomd.killed_cgroup`：Unit `tracker-extract-3.service` |

期望结果（集成测试逐条断言）：

- 共 2 条结论，两类各一条，`severity` 都是 `error`；
- 内核那一条：`confidence = 0.3`，`entity = service:firefox.service`，
  `evidence_refs` 是第 3、4 条记录的 `record_id`；
- oomd 那一条：`confidence = 0.95`，`entity = service:tracker-extract-3.service`，
  `evidence_refs` 只有第 5 条记录的 `record_id`；
- 两条结论的 `monotonic_ns`、`realtime_ns`、`clock_domain` 与各自的第一条证据相同；
- 采集的 `check_collection_integrity()` 返回空列表。

## 10. 与其它任务的边界

| 任务 | 关系 |
| --- | --- |
| 1.4 Journal 压缩包导入 | 本插件只读它写好的 `event` 记录，不改它的解析与归一化逻辑。 |
| 1.5 时间线查询 | 本插件不实现查询；结论按 `docs/time-base.md` 第 7 节的排序键落库，时间线直接能查出来。 |
| 2.1 生命周期分析 | 同目录下 2.1 自己的模块，本任务不改它的文件；两者都能写 `annotation`，靠 `analyzer` 字段区分。 |
| 4.x 可视化 | 前端按 `annotation_type`、`severity`、`confidence` 展示；`confidence` 低的那条要能看出来是疑似。 |

## 11. 偏差清单

- **不实现 bootrec 快照里的进程实体关联。** 本插件只从 Journal 事件推导结论；`process` 类型的实体
  目前没有采集来源，所以关联对象是 Unit 衍生的 `service` 实体。等采集侧提供进程实体后再补字段级关联。
- **systemd-oomd 的结论没有 PID 与进程名。** `Killed /<cgroup>` 行本身不写 PID，按项目规范
  「缺失不得伪造」，这里如实留空并在 `message` 里说明，没有用时间接近去猜进程。
- **除本文档与 `plugins/builtins.py` 的一行注册外，未改动其它既有文件。**
