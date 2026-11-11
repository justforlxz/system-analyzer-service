# 统一诊断数据模型

本文档是系统分析服务的领域数据契约，由任务 0.2（MKACG-763）定稿。

- 服务对象：Importer（1.3、1.4）、存储（1.1）、时间线（1.5）、分析插件（2.x）、采集插件（3.x）、Web 界面（4.x）。
- 可执行版本：`backend/src/system_analyzer/domain/bootrec.py`。字段名、必填项、缺失语义以本文档为准；
  文档与代码冲突时先改文档，再改代码。
- 时间字段的语义（`monotonic_ns`、`realtime_ns`、`clock_domain` 的取值规则与跨数据源换算）
  由任务 0.3 定稿。本文档只固定字段名、类型与单位，语义标注为「此项待 0.3 定稿」，不提前定稿。
- 本文档只定义模型，不定义存储表结构与导入流程。

目录：

1. 设计约束
2. 数据对象总览
3. 统一记录字段
4. 六个数据对象的字段表
5. 实体定义
6. 实体之间的关联关系
7. 数据缺失的表达方式
8. 幂等判据
9. 待 0.3 定稿的项

## 1. 设计约束

来自父 issue MKACG-761 的交付原则，约束本文档的每个决定：

- 所有采集数据进入统一时间模型，以 `CLOCK_MONOTONIC` 为主要基准，用 Boot ID 区分启动过程。
- Journal-only 导入与 SSH 采集共用同一套数据模型，不出现第二套结构。
- 数据缺失必须明确标识，不得把缺失的资源指标写成 0，也不得用空值冒充。
- 数据来源、时间、实体关联和分析结果必须可追溯。

由此得出的三条模型规则：

1. **一条物理事实只对应一条记录。** 不把「同一条信息」既写成事件又写成指标。
2. **记录是不可变的事实，分析结果也是记录。** 分析结论（Annotation）不得回改原始记录，只追加。
3. **每条记录都带齐来源与作用域。** 不依赖文件位置或外部约定推断来源。

## 2. 数据对象总览

统一记录有六种 `kind`，对应六个数据对象：

| kind | 对象 | 一句话定义 | 时间形态 |
| --- | --- | --- | --- |
| `event` | Event | 在某个时刻发生、没有持续时间的事实 | 一个时刻 |
| `span` | Span | 持续一段时间的状态或过程 | 起止两个时刻 |
| `metric` | Metric | 某条指标流在某个时刻的一个采样值 | 一个时刻 |
| `profile` | Profile | 一次性能采样的索引，原始数据是 Artifact | 一个时刻（采样窗口起点） |
| `annotation` | Annotation | 分析引擎对事实的结论，必须指向证据 | 一个时刻或一个范围 |
| `artifact` | Artifact | 原始文件的索引，文件本体在容器里 | 一个时刻（文件生成时刻） |

六种对象共用同一组「统一记录字段」（第 3 节），差异全部落在 `payload` 里。这样存储只需一张
记录表，时间线只需按 `monotonic_ns` 排序，不需要为每种对象各写一条查询路径。

## 3. 统一记录字段

每条记录都带下面这 11 个字段（其中 `record_id` 是为幂等新增的第 11 项，其余 10 项来自父 issue 的
约定）。判为必填的字段缺失时记录不合法，读入方应拒绝整条记录并报告位置，不得静默补默认值。

| 名称 | 类型 | 单位 | 必填 | 缺失语义 |
| --- | --- | --- | --- | --- |
| `record_id` | string（32 位小写十六进制） | — | 是 | 没有「缺失」这一说：必填，且必须等于按内容算出的值（见第 8 节） |
| `kind` | enum(`event`、`span`、`metric`、`profile`、`annotation`、`artifact`) | — | 是 | 必填，无法推断 |
| `machine_id` | string | — | 是 | 必填，且必须等于所属 collection 的 `machine_id` |
| `boot_id` | string | — | 是 | 必填，且必须等于所属 collection 的 `boot_id`；跨启动的数据必须拆成多份 `.bootrec` |
| `collection_id` | string | — | 是 | 必填，且必须等于所属 collection 的 `collection_id` |
| `source_plugin` | string | — | 是 | 必填，表示产生这条记录的插件 id；不允许空串，也不允许用 `unknown` 占位 |
| `stream_id` | string | — | 是 | 必填，表示同一来源下的一条逻辑流（如 `systemd.units`、`cpu`）；缺失即无法把采样串成序列 |
| `monotonic_ns` | int64 | 纳秒 | 条件必填：与 `realtime_ns` 至少有一个非空 | `null` 表示这次采集没有可用的单调时钟读数，读取方按 0.3 的换算规则处理，不得当作 0 |
| `realtime_ns` | int64 | 纳秒（Unix 纪元） | 条件必填：与 `monotonic_ns` 至少有一个非空 | `null` 表示没有墙钟读数，不得当作 0 |
| `clock_domain` | enum(`boot_monotonic`、`wall_realtime`) | — | 是 | 必填，表示 `monotonic_ns` 所在的时间域（此项待 0.3 定稿） |
| `entity` | string 或 `null` | — | 否 | `null` 表示这条记录不绑定某个具体实体（如整机级 Artifact）；非空时必须能在 `entities.jsonl` 里解析到，且 `metric`、`span` 不得为 `null` |
| `payload` | object | — | 是 | 结构由 `kind` 决定（第 4 节）；`payload` 内各字段的必填性按对象定义，不允许用空对象顶替 |

字段为什么这样分工：

- `machine_id`、`boot_id`、`collection_id` 三个作用域字段逐条重复，而不是只写在文件头：
  记录被复制、聚合、跨文件比对时，作用域不会因为「脱离文件上下文」而丢失。
- `entity` 只放实体 id，不放实体对象：实体定义只有一份（`entities.jsonl`），记录里重复实体属性
  会制造两份真值。
- `stream_id` 与 `entity` 不是二选一：`entity` 回答「这条数据属于谁」，`stream_id` 回答
  「这条数据属于哪条序列」。一条 CPU 指标流属于机器（`entity` 指向 `metric_stream` 实体），
  同时所有采样同属 `cpu` 流（`stream_id`）。

## 4. 六个数据对象的字段表

下表的字段都位于 `payload` 内。`attributes` 是自由键值对，仅用于补充展示信息，不参与任何关联键
或判重规则 —— 需要参与判断的信息必须提升为正式字段。

### 4.1 Event（瞬时事件）

| 名称 | 类型 | 单位 | 必填 | 缺失语义 |
| --- | --- | --- | --- | --- |
| `event_type` | string | — | 是 | 必填，形如 `systemd.unit.started`，由产生方定义命名空间 |
| `severity` | enum(`info`、`warning`、`error`) | — | 否 | 缺省视为 `info`；无法判断时不写，不写 `unknown` |
| `message` | string 或 `null` | — | 否 | `null` 表示没有可读描述，仅凭 `event_type` 与 `attributes` 表达事实 |
| `attributes` | object | — | 否 | 缺省为空对象；不承载必填事实 |

### 4.2 Span（持续过程）

记录的 `monotonic_ns` 是**开始**时刻，结束时刻在 payload 里。

| 名称 | 类型 | 单位 | 必填 | 缺失语义 |
| --- | --- | --- | --- | --- |
| `span_type` | string | — | 是 | 必填，形如 `service.active`、`boot.phase` |
| `end_monotonic_ns` | int64 或 `null` | 纳秒 | 否 | `null` 表示采集结束时该过程仍未结束（开放区间），时间线把它画到采集结束时刻并标注未闭合 |
| `end_realtime_ns` | int64 或 `null` | 纳秒（Unix 纪元） | 否 | 同上，与 `end_monotonic_ns` 各自独立，缺失不代表另一个也缺失 |
| `state` | string 或 `null` | — | 否 | `null` 表示没有状态信息；有状态时用来源的原值（如 `active`、`failed`） |
| `attributes` | object | — | 否 | 缺省为空对象 |

### 4.3 Metric（指标采样）

一条记录 = 一条指标流的一个采样点。序列由 `(source_plugin, stream_id, entity)` 三者共同确定。

| 名称 | 类型 | 单位 | 必填 | 缺失语义 |
| --- | --- | --- | --- | --- |
| `name` | string | — | 是 | 必填，指标名，如 `cpu.total.utilization`；必须与所指向的 `metric_stream` 实体的 `name` 一致 |
| `value` | float | 由 `unit` 决定 | 是 | 必填。**没有数据时不写这条记录**，不写 0、不写 `null`、不写 `NaN` |
| `unit` | string | — | 是 | 必填，取值见下表；必须与 `metric_stream` 实体的 `unit` 一致 |

`unit` 的取值集合：

| unit | 含义 | 例子 |
| --- | --- | --- |
| `percent` | 0–100 的百分比 | CPU 使用率 |
| `bytes` | 字节数 | 已用内存 |
| `bytes_per_second` | 每秒字节数 | `disk.read_bytes_per_second` |
| `count` | 无量纲计数 | 进程数、打开的文件数 |
| `seconds` | 秒 | 启动耗时 |

新增单位需要在本文档里追加一行，代码里的校验不限制取值集合，但同一指标名必须始终用同一个单位。

### 4.4 Profile（性能采样）

原始采样数据是二进制，进 `artifacts/`；Profile 只记录索引与参数。

| 名称 | 类型 | 单位 | 必填 | 缺失语义 |
| --- | --- | --- | --- | --- |
| `profile_type` | string | — | 是 | 必填，如 `perf.cpu` |
| `artifact_path` | string | — | 是 | 必填，指向容器内 `artifacts/` 下的文件；文件必须存在 |
| `sample_count` | int 或 `null` | 个 | 否 | `null` 表示产生方没有统计样本数，不得填 0 |
| `attributes` | object | — | 否 | 采集参数，如 `{"tool": "perf", "event": "cycles"}` |

### 4.5 Annotation（分析结论）

| 名称 | 类型 | 单位 | 必填 | 缺失语义 |
| --- | --- | --- | --- | --- |
| `annotation_type` | string | — | 是 | 必填，如 `oom.detected`、`systemd.service_failed` |
| `analyzer` | string | — | 是 | 必填，产生结论的分析器 id；与 `source_plugin` 一致时仍要写，便于单独统计分析结果 |
| `severity` | enum(`info`、`warning`、`error`) | — | 否 | 缺省视为 `info` |
| `message` | string 或 `null` | — | 否 | `null` 表示结论没有可读描述 |
| `confidence` | float 或 `null` | 0–1 的比例 | 否 | `null` 表示分析器不给置信度；不得填 0 表示「没有置信度」 |
| `evidence_refs` | array\<string\> | — | 否 | 缺省为空数组。元素是 `record_id` 或 `artifacts/` 下的路径；空数组表示结论只依赖整体统计，但分析器应在文档里说明依据 |

`evidence_refs` 是「分析结果必须能够追溯原始证据」的落地字段：分析器的输出必须能回指到原始记录或
原始文件，不能只有一句结论。

### 4.6 Artifact（原始文件）

| 名称 | 类型 | 单位 | 必填 | 缺失语义 |
| --- | --- | --- | --- | --- |
| `path` | string | — | 是 | 必填，必须是 `artifacts/` 开头的相对路径，文件必须存在于容器内 |
| `media_type` | string | — | 是 | 必填，如 `application/octet-stream`、`application/x-systemd-journal-export` |
| `size_bytes` | int64 | 字节 | 是 | 必填；与容器内实际文件大小一致 |
| `sha256` | string（64 位小写十六进制） | — | 是 | 必填；与实际文件内容一致，用于确认原始文件未被改动 |
| `description` | string 或 `null` | — | 否 | `null` 表示没有补充说明 |

## 5. 实体定义

实体是「数据属于谁」的统一表达。一个 `.bootrec` 只描述一台机器、一次启动、一次采集，所以三者的
主记录在 `manifest.json` 的 `collection` 段；`entities.jsonl` 持有机器与启动的详细信息，以及所有
有生命周期或需要被记录引用的实体。

### 5.1 Machine（机器）

| 名称 | 类型 | 单位 | 必填 | 缺失语义 |
| --- | --- | --- | --- | --- |
| `entity_type` | `"machine"` | — | 是 | 固定值 |
| `machine_id` | string | — | 是 | 必填，由部署方生成并在该机器上保持稳定（如 `machine:<hostname>`） |
| `hostname` | string | — | 是 | 必填；改名会让 `machine_id` 与 `hostname` 不再对应，故两者都记录 |
| `os_release` | string 或 `null` | — | 否 | `null` 表示未采集到（如 `/etc/os-release` 不可读） |
| `kernel_version` | string 或 `null` | — | 否 | 同上 |
| `architecture` | string 或 `null` | — | 否 | 同上（如 `x86_64`） |

### 5.2 Boot（启动）

| 名称 | 类型 | 单位 | 必填 | 缺失语义 |
| --- | --- | --- | --- | --- |
| `entity_type` | `"boot"` | — | 是 | 固定值 |
| `boot_id` | string | — | 是 | 必填，取自 `/proc/sys/kernel/random/boot_id` 的原值 |
| `machine_id` | string | — | 是 | 必填，指向 Machine |
| `started_realtime_ns` | int64 或 `null` | 纳秒（Unix 纪元） | 否 | `null` 表示没有采集到启动时刻（Journal-only 导入常见）；不得用 0 或估算值代替 |
| `kernel_version` | string 或 `null` | — | 否 | `null` 表示未采集到 |
| `boot_index` | int 或 `null` | 次 | 否 | `null` 表示不知道是第几次启动 |

### 5.3 Collection（采集）

一次采集，由 `manifest.json` 的 `collection` 段描述，不在 `entities.jsonl` 里重复。

| 名称 | 类型 | 单位 | 必填 | 缺失语义 |
| --- | --- | --- | --- | --- |
| `collection_id` | string | — | 是 | 必填，由产生方生成；同一份数据重复导出必须保持同一个值（幂等靠它） |
| `machine_id` | string | — | 是 | 必填，指向 Machine |
| `boot_id` | string | — | 是 | 必填，指向 Boot |
| `mode` | enum(`ssh_collection`、`journal_import`、`bootrec_import`) | — | 是 | 必填，表示数据怎么来的 |
| `created_realtime_ns` | int64 或 `null` | 纳秒（Unix 纪元） | 否 | `null` 表示产生方没有记录生成时刻 |

### 5.4 Entity（通用实体）

服务、进程、用户会话、cgroup、指标流共用一张结构，用 `entity_type` 区分。

| 名称 | 类型 | 单位 | 必填 | 缺失语义 |
| --- | --- | --- | --- | --- |
| `entity_type` | enum(`service`、`process`、`user_session`、`cgroup`、`metric_stream`) | — | 是 | 必填，取值之外的类型属于格式变更，需要升版本 |
| `entity_id` | string | — | 是 | 必填，在同一个 boot 内唯一，推荐 `<entity_type>:<name>` |
| `machine_id` | string | — | 是 | 必填，指向 Machine |
| `boot_id` | string 或 `null` | — | 否 | 指向 Boot；`null` 表示实体不绑定某次启动（如跨启动的进程模板） |
| `name` | string 或 `null` | — | 否 | 可读名称，如 `nginx.service` |
| `attributes` | object | — | 否 | 补充信息，如 `{"unit": "nginx.service"}` |

`metric_stream` 类型额外要求：

| 名称 | 类型 | 单位 | 必填 | 缺失语义 |
| --- | --- | --- | --- | --- |
| `name` | string | — | 是 | 指标名，必须与 `metric` 记录的 `payload.name` 一致 |
| `unit` | string | — | 是 | 指标单位，必须与 `metric` 记录的 `payload.unit` 一致 |
| `available` | bool | — | 是 | 必填。`false` 表示这条流没有数据 |
| `unavailable_reason` | string 或 `null` | — | 条件必填：`available=false` 时必填 | `available=true` 时必须为空；`available=false` 时不得为空，必须写明具体原因 |

## 6. 实体之间的关联关系

```text
Machine 1 ──< Boot 1 ──< Collection 1 ──< Record
                   │                        │
                   └──< Entity ─────────────┘（Record.entity）
                          └── MetricStream ──< Metric 记录的 stream 序列
```

| 关联 | 关联键 | 基数 | 该关联键在什么情况下失效 |
| --- | --- | --- | --- |
| Machine → Boot | `boot_id.machine_id = machine_id.machine_id` | 1:N | 机器重装系统或改主机名后，`machine_id` 若由主机名派生就会变，旧的 Boot 找不到 Machine。因此 `machine_id` 必须由部署方生成并在重装前保持稳定；无法保证时按「新机器」处理 |
| Boot → Collection | `collection.boot_id = boot.boot_id` | 1:N | 同一次启动可以采集多次（例如先采 30 秒、再采 5 分钟）。两次采集的 `collection_id` 不同，不能合并成一次；跨采集的对比由时间线按 `monotonic_ns` 对齐，不靠合并记录 |
| Collection → Record | `record.collection_id` | 1:N | 记录被单独复制出文件时，作用域字段仍在记录上，所以这个关联不因脱离文件而失效；但若有人手工改写 `collection_id`，就与原始采集脱钩，校验会报 `E_RECORD_SCOPE` |
| Boot → Entity | `entity.boot_id = boot.boot_id` | 1:N | `boot_id` 在每次重启后由内核重新生成，不能跨启动关联同一个服务。跨启动的「同一服务」只能用 `machine_id + name` 做逻辑匹配，且不保证是同一次运行 |
| Entity → Record | `record.entity = entity.entity_id` | 1:N | `entity_id` 只在同一个 boot 内唯一。把不同 boot 的实体合并到一张表时必须带 `boot_id` 作为联合键，否则同名服务会互相覆盖 |
| MetricStream → Metric 记录 | `(record.source_plugin, record.stream_id, record.entity)` + `payload.name` | 1:N | 若同一指标名在两条不同的 `stream_id` 下出现，说明产生方把一条流拆成了两条，序列会被拆断；若 `entity` 指向的不是 `metric_stream`，校验报 `E_ENTITY_TYPE_MISMATCH` |
| Record → Artifact | `payload.path`（Artifact）与 `payload.artifact_path`（Profile） | 1:N | 原始文件只在容器内有效。`.bootrec` 被解包、只导入数据库时，文件必须随采集目录一起保留；文件被删除后该 Artifact 只有索引没有内容，展示方要显示「原始文件缺失」而不是空图 |
| Annotation → 证据 | `payload.evidence_refs` | N:M | 指向 `record_id` 与 `artifacts/` 路径。若原始记录因为重新导入被替换，`record_id` 由内容算出，内容一致时仍能命中；内容不同则视为不同事实，旧结论的证据链会断开，必须重新分析 |

三条使用规则：

1. 只用上表的键做连接，不新增隐式约定（如「文件名里带服务名」「同一行即同一实体」）。
2. 连接失败时按「缺失」处理并报告，不猜测、不填充。
3. 跨启动的对比走时间线的对齐规则（0.3 定稿），不通过合并实体实现。

## 7. 数据缺失的表达方式

| 情况 | 表达方式 | 禁止的写法 |
| --- | --- | --- |
| 整份文件没有任何资源指标（Journal-only 导入） | `manifest.resources.available = false`，`resources.reason` 写明原因；文件里没有 `metric` 记录 | 写一条 `value: 0` 的占位 metric；写一个 `available=false` 的流却仍带采样 |
| 某条指标流没有数据 | `metric_stream` 实体 `available = false` + `unavailable_reason`；这条流没有任何 `metric` 记录 | `value: 0`、`value: null`、`NaN`、补一条「无数据」事件代替 |
| 某个实体属性没有采集到 | 字段写 `null`（或省略可选字段） | 写空串、写 `"unknown"` |
| 某个时间戳没有读数 | `monotonic_ns` 或 `realtime_ns` 写 `null`（两者不能同时为 `null`） | 写 0；用另一个时钟复制一份 |
| 某项分析结论没有依据 | 不产生这条 Annotation | 产生一条 `evidence_refs` 为空、消息里也不说明依据的结论 |

区分「没有数据」与「数据是 0」是硬性要求：CPU 使用率 0% 是一个有效测量值，采集失败是另一件事。
下游画图时，缺失区间必须断开曲线，不能画成 0。

## 8. 幂等判据

同一份 `.bootrec` 被重复导入时，靠两级键判断重复。

**第一级：文件内容摘要。** 导入方在导入开始时对整个文件的 ZIP 条目按名字排序计算摘要
（`sha256` over 每个条目的 `名字长度 + 名字 + 内容长度 + 内容`）。摘要相同的文件已经在库里，
直接跳过，不读记录。这一级处理「同一个文件被上传两次」这个最常见的情况。

**第二级：记录主键。** 每条记录的 `record_id` 是内容摘要：

```text
record_id = blake2b_128( 规范化 JSON( 除 record_id 之外的全部字段 ) )
规范化 JSON = 键排序、无多余空白、保留非 ASCII 字符
```

导入方用 `(collection_id, record_id)` 作为记录的唯一键写入：

- 同一个键再次出现 → 内容必然相同（内容不同则 `record_id` 不同），视为重复，跳过即可；
- 同一个 `collection_id` 下出现新增的 `record_id` → 是同一次采集的补充数据（例如采集重试后补上
  了最后几秒），追加写入；
- `collection_id` 相同但记录集合整体不同 → 说明产生方把两次不同的采集用了同一个
  `collection_id`，此时导入方应保留两批记录并在任务结果里报「collection_id 被重复使用」。

为什么用内容摘要而不是 UUID：

- 产生方从同一份原始数据重复生成 `.bootrec` 时，得到的是同一个 `record_id`，天然幂等，不需要
  产生方保存「已生成过哪些 id」的状态。
- 记录被改动一个字节，`record_id` 就变，校验能发现「文件被改过但没更新 id」（`E_RECORD_ID_MISMATCH`）。
- 代价是内容相同的两条独立事实（如同一毫秒同一个单元的两条一样的事件）会被合并成一条。当前六类
  对象里每条记录都带时间与来源，真正完全相同意味着「同一个事实被采了两次」，合并是正确的。

## 9. 待 0.3 定稿的项

以下内容由任务 0.3（时间基准与跨数据源关联规范）定稿，本文档只给出字段与单位：

1. `clock_domain` 的完整取值集合，以及 `boot_monotonic` 与 `wall_realtime` 的换算与漂移处理规则。
2. `monotonic_ns` 的零点定义（内核启动时刻还是采集开始时刻）以及跨 boot 比较的允许方式。
3. `Span` 的开放区间（`end_*_ns` 为 `null`）在时间线上的收尾规则。
4. 多份采集（不同 `collection_id`、同一次启动）在统一时间轴上的对齐与去重规则。
5. Journal 时间戳（`__REALTIME_TIMESTAMP`、`__MONOTONIC_TIMESTAMP`）到模型时间字段的换算细则。

在上述规则定稿前，读取方必须把时间字段当作「同一 boot 内可排序的纳秒读数」，不得自行做跨 boot
的挂钟比较。
