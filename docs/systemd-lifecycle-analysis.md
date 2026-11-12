# Systemd 服务生命周期分析

本文档是任务 2.1（issue MKACG-772）的交付物之一：说明怎样从已入库的 Journal 事件重建 systemd
Unit 的生命周期区间，识别启动耗时、启动失败与异常退出，并把每条结论指回原始记录。

- 可执行版本：`backend/src/system_analyzer/analyzers/systemd_lifecycle.py`（推导）、
  `backend/src/system_analyzer/analyzers/systemd_lifecycle_analyzer.py`（插件）。
- 数据字段全部来自 0.2（`docs/data-model.md`），时间语义来自 0.3（`docs/time-base.md`），插件接口来自
  0.4（`docs/plugin-contracts.md`）。本文档不新增任何数据对象或字段，只规定「怎样从日志推导语义」。
- 固定 fixture：
  - `backend/tests/fixtures/systemd_lifecycle/boot.export`：手工拼的样本，覆盖各种生命周期句式与边界；
  - `backend/tests/fixtures/systemd_lifecycle/real-boot.journal.gz`：由本机 `journalctl -o export`
    原样截取的 36 条生命周期消息（11 个 Unit）。**真实数据的字段布局与手工样本不同**，见第 3 节。
- 预期结果由 `backend/tests/test_systemd_lifecycle.py` 逐条断言（时刻容差 1 毫秒）。

目录：

1. 目标与不做什么
2. 输入：已入库的 Journal 事件
3. 认哪些日志消息，说的是哪个 Unit
4. 分段规则：一次激活一段
5. 输出之一：生命周期区间（span）
6. 输出之二：分析结论（annotation）
7. 数据不足：输出「生命周期不完整」，不补全区间
8. 启动耗时
9. 按指定时间点查询服务状态
10. 采集边界与多次采集
11. 证据链与可重复性
12. 插件数据入口
13. 运行与验证
14. 偏差清单

## 1. 目标与不做什么

**目标**：从 Journal 事件里推导出 Unit 的启动、运行、停止、失败四类过程，落成可以画在时间轴上的
区间，以及可以追溯证据的结论。

**不做什么**：

- 不重新解析 Journal 压缩包。解析与字段归一化是任务 1.4 的事，本分析只读它写进库的 `event` 记录。
- 不修改原始记录。分析只追加新的 `span` 与 `annotation` 记录（0.2 第 1 节：记录是不可变的事实）。
- 不改 1.4 的解析逻辑、不改 1.5 的排序规则、不改 `storage/`、`domain/`、`plugins/contracts.py`。
- 不推断日志里没有的事实。缺证据就写「生命周期不完整」，不补一个看起来合理的时间。

**为什么输出用 `span` + `annotation`**：0.2 定稿的六种对象里没有专门的「Unit 生命周期」对象，
而有生命周期含义的正是 `span`（起止两个时刻加 `state`），有结论含义的正是 `annotation`
（类型、严重级别、可读说明、证据）。用这两个既有对象表达，数据模型不需要改，1.5 的时间线也不用
为它加一条查询路径。命名沿用 0.2 样例文件 `docs/samples/bootrec-v1/full.bootrec` 里已有的
`service.active`、`systemd.service_failed`、`systemd.units`、`analysis.systemd`。

## 2. 输入：已入库的 Journal 事件

任务 1.4 把每条 Journal 条目写成一条 `event` 记录：

| 记录里的位置 | 内容 | 本分析怎么用 |
| --- | --- | --- |
| `monotonic_ns` / `realtime_ns` | boot 内位置与墙钟时刻（微秒 ×1000） | 区间的起止时刻；只有带 `monotonic_ns` 的记录才进入重建（`clock_domain = boot_monotonic`） |
| `record_id` | 记录内容摘要 | 结论的 `evidence_refs` 指向它 |
| `entity` | 导入时为 `service:<_SYSTEMD_UNIT>` | 不直接用：真实数据上它是日志发送方的 Unit，见第 3 节 |
| `payload.event_type` | `journal.entry` | 只分析这类事件 |
| `payload.message` | `MESSAGE` 原文 | 识别生命周期句式 |
| `payload.attributes.fields` | 整条 Journal 原始字段 | 取 `SYSLOG_IDENTIFIER`、`UNIT`、`USER_UNIT`、`INVOCATION_ID`、`_SYSTEMD_UNIT` 等 |

只有墙钟的记录（`monotonic_ns` 为 `null`）不参与重建：`docs/time-base.md` 第 2、4 节规定只有墙钟的
读数没有启动锚点时不可换算，硬放进 boot 时间轴会得到一条没有物理含义的轴。这些记录在原始事件里
原样保留，本分析不删除也不改写它们。

## 3. 认哪些日志消息，说的是哪个 Unit

链路：

```text
记录 → 是 event 吗？→ event_type = journal.entry（或 attributes.derived_from = journal）？
     → 有 monotonic_ns 吗？→ SYSLOG_IDENTIFIER = systemd 吗？
     → Unit 取自 UNIT= / USER_UNIT=（拿不到才退回 _SYSTEMD_UNIT）→ 把 message 归到句式
```

### 3.1 只认 systemd 自己打印的消息

只认 `SYSLOG_IDENTIFIER = systemd`：systemd 的生命周期消息由 PID 1（或 user manager）自己打印，
标识符固定是 `systemd`；业务程序自己的日志也带 `_SYSTEMD_UNIT`（只要它的进程在 Unit 的 cgroup
里），一句 `Started worker pool` 会被误判成「服务启动完成」。判错一个启动时刻，整段区间就是编造的。

### 3.2 Unit 取自 `UNIT=` / `USER_UNIT=`，不是 `_SYSTEMD_UNIT`

这是真实 Journal 与手工样本最大的差别，也是本分析第一次交付时最严重的缺陷：

| 字段 | 真实含义 | 真实数据里的取值 |
| --- | --- | --- |
| `UNIT=` | 消息说的是哪个 Unit（系统实例） | `NetworkManager-dispatcher.service`、`systemd-journald.service` 等 |
| `USER_UNIT=` | 消息说的是哪个 Unit（用户实例） | `podman-3623221.scope` 等 |
| `_SYSTEMD_UNIT` | **写这条日志的进程**所属的 Unit | PID 1 的日志恒为 `init.scope`；user manager 的日志是 `user@1000.service` |

只看 `_SYSTEMD_UNIT` 的后果：一个 boot 里 3000 多个服务被合并进 `init.scope` 与
`user@1000.service` 两个伪 Unit，产出的区间与结论全部挂在错误的对象上。

取值顺序：`UNIT=` → `USER_UNIT=` → `_SYSTEMD_UNIT`（后者只在拿不到结构化字段时使用，那时日志的
发送方就是该 Unit）。

同理，「这次激活的编号」也取对象作用域的字段：`INVOCATION_ID=` → `USER_INVOCATION_ID=`；
只有当日志的发送方就是该 Unit 时才退回 `_SYSTEMD_INVOCATION_ID`。

### 3.3 PID 与 cgroup 只在发送方就是该 Unit 时才算在它头上

`_PID` 与 `_SYSTEMD_CGROUP` 描述的也是「谁写了这条日志」。PID 1 打印的消息上它们是 `1` 与
`/init.scope`，算在某个服务头上就是错误关联。因此这两个字段只在
`_SYSTEMD_UNIT` / `_SYSTEMD_USER_UNIT` 等于被分析的 Unit 时才写入区间属性，否则留空——宁可不写，
也不写错。真实 Journal 的生命周期消息因此没有 `pid` / `cgroup`，手工样本（发送方就是服务自己）
两者都有。

### 3.4 识别到的信号

句式取自 systemd 源码 `job.c` / `unit.c` / `service.c` 打印的固定文案（不翻译）：

| 句式 | 信号 | 含义 |
| --- | --- | --- |
| `Starting <描述>...` | `starting` | 激活开始 |
| `Started <描述>.` | `started` | 激活完成，进入运行 |
| `Stopping <描述>...` | `stopping` | 开始停止 |
| `Stopped <描述>.`、`<unit>: Deactivated successfully.`、`<unit>: Succeeded.` | `stopped` | 已停止 |
| `Failed to start <描述>.` | `activation_failed` | 启动失败 |
| `<unit>: Failed with result '<结果>'.`、`Unit entered failed state.` | `failed_result` | Unit 进入 failed |
| `<unit>: Main process exited, code=<方式>, status=<状态>` | `main_exit` | 主进程退出（只保留非正常退出，见下） |

两条判定细节：

- **去掉 Unit 前缀再匹配**。systemd 的消息有两种写法：`Started PostgreSQL RDBMS.`（不带单元名）与
  `NetworkManager-dispatcher.service: Deactivated successfully.`（带单元名）。前缀本身不含空格且带
  `: ` 时才算前缀，避免把正文里的冒号当分隔符。
- **主进程正常退出不算异常退出**。`code=exited` 且 `status` 的数字部分是 0（如 `0/SUCCESS`）时不产生
  信号：它不是「异常退出」，也不是停止证据（停止由 `Stopped` / `Deactivated successfully` 表达）。
  `code=killed` / `dumped` / `timeout` 一律算非正常退出；`status` 写法认不出来时不加「异常」的结论。

认不出来的消息一律跳过，不按关键字模糊匹配、不猜。

## 4. 分段规则：一次激活一段

同一个 `(collection_id, unit)` 的信号按 `(monotonic_ns, record_id)` 排序后切成一轮轮激活：

1. **编号变了就换段**。`INVOCATION_ID` 是判定「是不是同一次激活」最硬的依据，这一条对所有信号
   成立：只拿到失败记录的 Unit 也会连着失败好几次（真实数据里就有），只按开始证据分段会把几次
   失败合并成一段。
2. 上一段已经有终态（`stopped` / `activation_failed` / `failed_result`），又出现 `starting` /
   `started`，也开启新的一段。
3. 出现第二条 `started` 而编号不足以证明是同一次激活（缺失或无法比较）时，同样另起一段：拆开只会
   多出一段「不完整」，合并却会产出一个偏长的错误区间。

编号相同、时间相同的重复 `Started` 视为重复日志，不拆段，也不改动第一次的开始时刻。

从一段的信号推导五个时刻：

| 时刻 | 取自 | 缺失时 |
| --- | --- | --- |
| 激活开始 | 第一条 `starting` | `null` |
| 激活结束 | 第一条 `started`；没有 `started` 时取该段的终态信号 | `null` |
| 运行开始 | 第一条 `started` | `null` |
| 运行结束 | 该段的终态信号（`stopped` / `activation_failed` / `failed_result`），且不早于运行开始 | `null` |
| 终态 | `stopped` → `inactive`；`activation_failed` / `failed_result` → `failed` | `null` |

`INVOCATION_ID`、`PID`、`cgroup` 按「激活开始 → 激活结束 → 终态 → 其余」的顺序取第一个有值的
证据记录（PID 与 cgroup 还受第 3.3 节的限制），四个字段与 Unit 一起落在同一段区间上。

## 5. 输出之一：生命周期区间（span）

两段区间，都是 `span` 记录，`source_plugin = systemd.lifecycle`，`stream_id = systemd.units`，
`entity = service:<unit>`，`clock_domain = boot_monotonic`：

| `span_type` | 开始 | 结束 | `state` 取值 |
| --- | --- | --- | --- |
| `service.activating` | 激活开始 | 激活结束（`Started` 或激活期间的失败） | `activating`（未闭合）／`active`（正常完成）／`failed`／`inactive` |
| `service.active` | 运行开始（`Started`） | 运行结束（停止或失败），未观测到则为 `null` | `active`（未闭合）／`inactive`／`failed` |

`state` 用 systemd 自己的 ActiveState 取值（`activating` / `active` / `inactive` / `failed`），
不做中文翻译，也不自造取值。

区间里能查到的关联信息放在 `payload.attributes`：

| 键 | 含义 |
| --- | --- |
| `unit` | Unit 名，如 `NetworkManager-dispatcher.service` |
| `invocation_id` | 这次激活的编号（`INVOCATION_ID=` / `USER_INVOCATION_ID=`） |
| `pid` | 证据记录上的 `_PID`，**仅当发送方就是该 Unit**（见第 3.3 节） |
| `cgroup` | 证据记录上的 `_SYSTEMD_CGROUP`，同样仅当发送方就是该 Unit |
| `startup_duration_ns` | 仅 `service.activating` 且激活完成时有：启动耗时（纳秒） |

## 6. 输出之二：分析结论（annotation）

四条结论类型，`source_plugin` 与 `payload.analyzer` 都是 `systemd.lifecycle`，
`stream_id = analysis.systemd`，`entity = service:<unit>`，`evidence_refs` 非空且只指向本次分析
用到的 Journal 事件记录（`record_id`）：

| `annotation_type` | `severity` | 什么时候产生 | 证据 |
| --- | --- | --- | --- |
| `systemd.service_failed` | `error` | 该段出现 `Failed to start` 或 `Failed with result` | 失败记录 + 该段的开始证据 |
| `systemd.startup_failure` | `error` | 失败发生在运行开始之前，且确有启动尝试的证据（观测到 `Starting`，或失败本身就是 `Failed to start`） | `Starting` 记录 + 失败记录 |
| `systemd.abnormal_exit` | `error` | 主进程非正常退出 | 该 `Main process exited` 记录（另有失败记录时一并写上） |
| `systemd.lifecycle_incomplete` | `warning` | 该段证据不足（见第 7 节） | 该段全部证据记录 |

消息是给人读的中文句子，例如：

```text
postgresql.service 启动失败，激活阶段用时 640.000 毫秒：Failed to start PostgreSQL RDBMS.
sshd.service 的主进程非正常退出（code=exited, status=1/FAILURE）：sshd.service: Main process exited, code=exited, status=1/FAILURE
```

**`confidence` 写 `null`**：这四条结论都是固定规则从原文直接得出的二值判断，没有概率成分；写一个
0.9 之类的数字会让读者以为存在统计意义上的把握程度。`docs/data-model.md` 第 4.5 节允许
「分析器不给置信度」，这里就用这个表达。

只有 `Failed with result`、没有 `Starting` / `Failed to start` 时，只产生 `systemd.service_failed`，
不产生 `systemd.startup_failure`：那时只能说「这个 Unit 失败了」，说「启动失败」就是替日志下结论。

## 7. 数据不足：输出「生命周期不完整」，不补全区间

一段区间的证据缺口按原因码记录，并写进一条 `systemd.lifecycle_incomplete` 结论的 `message`，
消息里固定包含「生命周期不完整」四个字：

| 原因码 | 判定 | 消息里的说明 |
| --- | --- | --- |
| `missing_activation_start` | 没有 `Starting` 记录 | 缺少启动（Starting）记录，激活开始时刻未知 |
| `missing_started` | 有停止记录，却没有 `Started` | 缺少启动完成（Started）记录，运行开始时刻未知 |
| `missing_termination` | 没有任何终态记录 | 缺少停止或失败记录，区间结束时刻未知，按未闭合处理 |
| `out_of_order_evidence` | 失败或停止证据早于 `Started` | 失败或停止证据早于 Started，顺序异常，没有按时间补全 |

具体做法：

- 缺结束证据时 `end_monotonic_ns` / `end_realtime_ns` 写 `null`（0.2 第 4.2 节、0.3 第 6 节的开放
  区间），时间线按所属 collection 的采集窗口终点收尾并标注未闭合。
- **不用「下一次激活的开始时刻」补上一段的结束时刻**。日志里没有停止记录，就不能假设服务在那时停了。
- **不因为缺 `Starting` 就丢掉整段**：只补上能确定的时刻（例如只有 `Started` 时仍给出
  `service.active` 的开始时刻），缺的那一阶段不生成区间。
- 完全没有日志证据的时刻不返回任何状态（见第 9 节），不写「没在运行」。

## 8. 启动耗时

启动耗时 = 激活结束时刻 − 激活开始时刻，两个时刻都来自证据：

- `service.activating` 区间的 `payload.attributes.startup_duration_ns`；
- `LifecycleReconstruction.startup_durations()`：每一段激活一条，缺证据的那一段也在列表里，
  `duration_ns` 写 `None`（把「没有测到」显式表示出来，而不是让这一项凭空消失）。

只有 `Started` 没有 `Starting` 时没有启动耗时：拿「运行开始时刻」减任意别的东西都不是启动耗时。

## 9. 按指定时间点查询服务状态

`LifecycleReconstruction.state_at(monotonic_ns)`（boot 内纳秒）返回该时刻每个 Unit 的状态。

同一个 boot 的所有采集共用一条单调时钟轴（`docs/time-base.md` 第 7 节），所以查询把该 Unit 在
**所有采集**里的状态变化时刻合到一起，再取不晚于查询时刻的最后一个：

| 证据 | 状态 |
| --- | --- |
| 激活开始 | `activating` |
| 运行开始（`Started`） | `active` |
| 终态信号 | `inactive` / `failed` |

只看其中某一次采集会给出与其它采集的证据矛盾的答案：例如第一次采集里有完整的
`Stopped`（3000ms），第二次采集只有 `Started`（2500ms），若按「开始最晚的那一段」判断，
`t = 5000` 会返回 `active`，而实际上 3000ms 就已经停了。

| 字段 | 含义 |
| --- | --- |
| `state` | `activating` / `active` / `inactive` / `failed` / `unknown` |
| `invocation_id` | 得出该状态的那次激活的编号 |
| `state_since_ns` | 当前状态从哪个证据时刻开始 |
| `stale` | `true` 表示该 Unit 的证据在查询时刻之前就用尽了：这是「最后一次观测到的状态」，不是实时读数 |
| `complete` | 得出该状态的那段区间是否完整 |

`stale` 的判据是「该 Unit 最后一次证据的时刻不晚于查询时刻」，因此开放区间在日志用尽之后一定是
`true`——包括只比最后一条证据晚一点点的时候。调用方据此区分「现在仍在运行」与「日志到这里就
没有了」。

查询时刻之前该 Unit 没有任何证据时，结果里不出现它：缺证据时不说「没在运行」，那会把
「没观测到」说成「不存在」。

## 10. 采集边界与多次采集

生命周期**区间**按 `(collection_id, unit)` 分段，区间不跨采集：

- 一条派生记录只能追加到它自己所属的采集（记录的作用域字段必须与采集一致，1.1 会校验）。
- `docs/time-base.md` 第 6 节规定开放区间只在其所属 collection 的窗口内有效。下一次采集又观测到
  同一个 Unit 时，按新记录新开一段，不去延长上一段（记录不可变）。
- 因此「启动在第一次采集、停止在第二次采集」会得到两段都不完整的区间，两段各有一条
  `systemd.lifecycle_incomplete`。这比把两段拼成一段更接近事实：中间那段空档确实没有观测到。
- 只有**按时间点查询状态**会把多次采集的证据合起来判断（第 9 节），因为那是一次读取，
  不是在改写记录。

## 11. 证据链与可重复性

- 每条派生的 `span` / `annotation` 记录都按 0.2 第 8 节算 `record_id`（内容是除 `record_id` 外的全部
  字段的 blake2b_128 摘要）。同样的输入重复分析得到同样的记录与同样的 `record_id`。
- 写入走存储层的追加路径，按 `(collection_id, record_id)` 幂等：重复分析不新增行，也就不会叠加出
  互相矛盾的结论。`AnalysisResult.annotations_written` 报的是这一次真正写进去的结论条数，重复分析时
  是 0。
- 结论的 `evidence_refs` 指向原始 Journal 事件的 `record_id`，不指向本次产出的区间：证据链始终
  落在原始事实上，删掉分析结果重新分析仍然接得上。

## 12. 插件数据入口

`analyze` 是同步方法（`docs/plugin-contracts.md` 第 3.4 节），签名 `analyze(request, context)`，
返回 `AnalysisResult`。插件不 import 存储层，它声明自己需要的同步入口形状
`analyzers.systemd_lifecycle_analyzer.LifecycleDataAccess`：

| 方法 | 对应存储层 | 说明 |
| --- | --- | --- |
| `list_records_in_boot(machine_id, boot_id, *, kinds=None, limit=None)` | `DiagnosticStore.list_records_in_boot` | 读一次启动的记录，只读 |
| `ensure_entities(collection_id, entities) -> int` | 目前没有同名的公开方法 | 把本次分析引用的 service 实体补进这次启动的实体集合，必须幂等 |
| `append_records(collection_id, records) -> int` | `DiagnosticStore.append_records` | 追加记录，返回写入条数 |

**为什么需要 `ensure_entities`**：记录里的 `entity` 必须是已存在的实体（0.2 第 6 节），而任务 1.4
只按 `_SYSTEMD_UNIT` 建过实体——真实 Journal 上那就是 `init.scope` / `user@1000.service`。被分析的
Unit 大多没有实体，不补就写不进去。1.1 目前只有整份采集写入 `import_collection` 能写实体，
Core 接这个入口时可以复用它（集成测试的同步桥接就是这么做的，见
`tests/integration/test_systemd_lifecycle_analysis.py` 的 `_ensure_entities`），也可以补一个专用的
实体追加方法。这是本分析对 Core 提出的唯一新增入口，与 1.3 / 1.4 要求同步的整份写入入口同一性质。

Core 负责把异步存储层包成这个同步形状（与 1.3 / 1.4 对导入插件做的事完全一样：Worker 线程里
每次调用开一条短命事件循环，连接池用 `NullPool`）。插件拿到的是返回协程的入口时，抛
`PluginInputError` 说明「分析需要同步入口」，绝不自己在插件里 `asyncio.run`。

写库顺序：先 `ensure_entities`（记录要引用它们），再写区间、写结论。

**机器作用域**：`AnalysisRequest` 只有 `boot_id`（0.4 定稿，本任务不改契约），而存储层按
`(machine_id, boot_id)` 取数——同一个 `boot_id` 可能出现在两台机器上（虚拟机从快照恢复，见
`docs/data-model.md` 第 5.2 节）。因此 Core 必须通过 `request.options["machine_id"]` 给出机器；
缺失时插件抛 `PluginInputError` 并说明原因，不猜机器。`boot_id` 为空（表示「全部分析」）时同样拒绝：
生命周期要落在一条 boot 时间轴上，空作用域无法定位。`request.record_ids` 非空时只分析这些记录。

## 13. 运行与验证

```bash
cd backend && uv run ruff check . && uv run ruff format --check . && uv run pytest
```

```bash
scripts/dev-db.sh start
cd backend && cp -n .env.example .env
uv run alembic upgrade head
uv run pytest -m integration
cd .. && scripts/dev-db.sh stop
```

### 13.1 手工样本 `boot.export` 的预期结果

逐条断言在 `tests/test_systemd_lifecycle.py`，时刻容差 1 毫秒：

| Unit | 证据 | 期望结果 |
| --- | --- | --- |
| `nginx.service` | `Starting` 1000ms、`Started` 1480ms、`Stopping` 9000ms、`Stopped` 9120ms | 激活 [1000, 1480]、运行 [1480, 9120]、启动耗时 480ms、状态 `inactive`、区间完整、无结论 |
| `postgresql.service` | `Starting` 2000ms、`Failed to start` 2640ms、`Failed with result 'exit-code'` 2650ms | 激活 [2000, 2640] 状态 `failed`、无运行区间、启动耗时 640ms；结论 `systemd.startup_failure` + `systemd.service_failed` |
| `redis.service`（第一次激活） | 只有 `Started` 3000ms | 运行区间 [3000, null]、结束时刻保持 `null`；结论 `systemd.lifecycle_incomplete` |
| `redis.service`（第二次激活） | `Started` 7000ms（另一个 `INVOCATION_ID`）、`Deactivated successfully` 8000ms | 与第一次激活分开的另一段、运行 [7000, 8000] 状态 `inactive` |
| `sshd.service` | `Main process exited, code=exited, status=1/FAILURE` 4000ms、`Failed with result 'exit-code'` 4010ms | 无区间（没有 `Starting` / `Started`）；结论 `systemd.abnormal_exit` + `systemd.service_failed` + `systemd.lifecycle_incomplete` |
| 业务日志 | `SYSLOG_IDENTIFIER=redis-server`、`Started serving requests.` | 忽略，不产生任何区间或结论 |

### 13.2 真实样本 `real-boot.journal.gz` 的预期结果

这份 fixture 是 `journalctl -o export` 的原样截取，用来检验真实字段布局（第 3 节）：

| 断言 | 期望 |
| --- | --- |
| Unit 集合 | 11 个，全部来自 `UNIT=` / `USER_UNIT=`；`init.scope` 与 `user@1000.service` 都不出现 |
| `NetworkManager-dispatcher.service` | 4 段完整激活，4 个互不相同的 `INVOCATION_ID`，每段都有正的启动耗时 |
| user manager 的 Unit | 各段带自己的 `USER_INVOCATION_ID`，终态 `failed` |
| 区间属性 | 只有 `unit` / `invocation_id` / `startup_duration_ns`，没有 `pid` / `cgroup`（发送方是 PID 1） |

## 14. 偏差清单

1. **不新增数据对象**：按 PL 在任务交接里的要求，生命周期区间落在 `span`、结论落在 `annotation`，
   没有新增模型；理由见第 1 节。`span_type` 用了 `service.activating` / `service.active`，其中
   `service.active` 沿用 0.2 样例里的写法，`service.activating` 是新增的取值（`span_type` 是自由
   字符串，0.2 只要求「形如 `service.active`」，不需要改模型）。
2. **`analyzer` 字段取插件 id `systemd.lifecycle`**：0.2 样例里的 `analyzer` 是
   `systemd-lifecycle`；`docs/data-model.md` 第 4.5 节要求它是「产生结论的分析器 id」，这里按插件
   id 写，和 `source_plugin` 保持一致。
3. **`AnalysisRequest` 需要 Core 给 `options["machine_id"]`**：契约里 `AnalysisRequest` 没有机器
   字段，而存储层按 `(machine_id, boot_id)` 取数。本任务不改 0.4 的契约，改用 `options` 里的一个
   键，并在缺失时明确报错（第 12 节）。
4. **需要 `ensure_entities` 数据入口**：分析产出的记录引用被分析的 Unit，而 1.4 只为
   `_SYSTEMD_UNIT` 建过实体。1.1 还没有公开的实体追加入口，Core 需要提供它（可复用
   `import_collection`）。见第 12 节。
5. **只认 `SYSLOG_IDENTIFIER = systemd`**：结果是手工拼的样例
   `docs/samples/bootrec-v1/journal-only.bootrec`（缺该字段）不会产生任何生命周期结论。真实
   Journal 数据都带这个字段。
6. **PID 与 cgroup 只在发送方就是该 Unit 时写入**（第 3.3 节）。要拿到真正的主进程 PID，需要
   采集侧记录 `MainPID` 一类的字段，属于 3.x 的范围。
7. **`docs/samples/bootrec-v1/journal-only.bootrec` 与 `tests/fixtures/journal_archive/boot.export`
   都是手工拼的样本，不是真实 Journal**。第一版本文档曾把后者称作「真实 Journal 数据」，是错的；
   本次新增 `tests/fixtures/systemd_lifecycle/real-boot.journal.gz`（本机 `journalctl -o export`
   原样截取），真实字段布局才算被测试覆盖。
