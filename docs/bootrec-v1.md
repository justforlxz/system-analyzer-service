# bootrec v1 容器格式

本文档定义 `.bootrec` 文件的结构，由任务 0.2（MKACG-763）定稿。数据对象与字段语义见
`docs/data-model.md`，本文档只讲容器。

- 一个 `.bootrec` = 一次采集（一个 Collection）+ 一次启动（一个 Boot）+ 一台机器（一个 Machine）。
  一次启动可以产生多份 `.bootrec`（多次采集），一次采集只能装进一份 `.bootrec`。
- 可执行版本：`backend/src/system_analyzer/domain/bootrec.py`；校验命令：
  `cd backend && uv run python -m system_analyzer.tools.validate_bootrec <文件>`。
- 只定义格式与样例，不定义存储与导入流程。导入方（1.3、1.4）与存储（1.1）按本文档解析。

目录：

1. 容器结构
2. `manifest.json`
3. `entities.jsonl`
4. `records.jsonl`
5. `journal.jsonl`
6. `artifacts/`
7. 格式版本与向后兼容
8. 完整性校验
9. 三份样例
10. 与下游任务的接口约定
11. 校验错误码

## 1. 容器结构

`.bootrec` 是一个 ZIP 文件（`application/zip`），条目全部在根目录，不建子目录（`artifacts/` 是唯一
的路径前缀）。必须存在的条目：

```text
manifest.json
entities.jsonl
records.jsonl
journal.jsonl
artifacts/              （可选：没有原始文件时不出现）
artifacts/<文件名>       （可选，可以有多层子路径）
```

规则：

| 条目 | 必备 | 内容 | 格式 |
| --- | --- | --- | --- |
| `manifest.json` | 是 | 文件自描述：格式版本、采集标识、资源与 Journal 的有无、各数据文件校验和与行数 | 单个 UTF-8 JSON 对象 |
| `entities.jsonl` | 是 | 机器、启动与所有被记录引用的实体 | UTF-8，每行一个 JSON 对象 |
| `records.jsonl` | 是 | 全部统一记录（六种 `kind` 混在一起） | UTF-8，每行一个 JSON 对象 |
| `journal.jsonl` | 是 | 归一化后的 Journal 日志条目 | UTF-8，每行一个 JSON 对象 |
| `artifacts/` | 否 | 原始文件（perf 采样、导出的日志包等） | 任意字节 |

三个 `.jsonl` 即便没有数据也必须存在，写成空文件（0 行）。这样「文件不存在」只表示文件损坏，
不表示「这类数据没有」，读取方不用为两种语义各写一条分支。

其它约定：

- 不允许同名条目（`zipfile` 读出同一名字两份时校验报 `E_DUPLICATE_ENTRY`），因为读取方无法
  判断用哪一份。
- 条目顺序不参与语义。九成情况下 `manifest.json` 在第一位便于人肉排查，但读取方不得依赖顺序。
- 校验和不计入 ZIP 自身的元数据（时间戳、压缩等级、权限位），所以同样的数据重新打包得到同一次
  校验结果。
- 一个 `.bootrec` 里的所有记录必须属于同一个 `boot_id`。跨启动的 Journal 压缩包在导入时必须按
  `boot_id` 拆成多份 `.bootrec`（1.4 的职责），不得把多次启动塞进一份文件。
- `.jsonl` 的行分隔符只有 `\n`（行尾允许有一个 `\r`）。JSON 字符串里可以出现任意 Unicode 字符，
  包括 U+2028、U+2029、U+0085 与 `\x0b`、`\x0c` 这类 `str.splitlines()` 会当作换行的字符
  —— Journal 的 `message` 是任意程序与内核写进去的原文，这些字符真实存在。读取方必须只按 `\n`
  切行（本仓库的 `jsonl_lines()`），否则会把一条合法记录切成两段。

## 2. `manifest.json`

```json
{
  "format": "bootrec",
  "format_version": "1.0",
  "collection": {
    "collection_id": "collection:full-0001",
    "machine_id": "machine:uos-PC",
    "boot_id": "3f1c9a52-8f0e-4a10-9b1c-2b7d5c3a6e11",
    "mode": "ssh_collection",
    "created_realtime_ns": 1794009600000000000
  },
  "producer": {"name": "system-analyzer-service", "version": "0.2.0"},
  "resources": {"available": true, "reason": null},
  "journal": {"included": true},
  "counts": {"entities": 7, "records": 17, "journal_entries": 3},
  "entries": {
    "entities.jsonl": {"sha256": "…", "size_bytes": 1234},
    "records.jsonl": {"sha256": "…", "size_bytes": 5678},
    "journal.jsonl": {"sha256": "…", "size_bytes": 90}
  }
}
```

字段表：

| 名称 | 类型 | 单位 | 必填 | 缺失语义 |
| --- | --- | --- | --- | --- |
| `format` | `"bootrec"` | — | 是 | 固定值；不是它就说明这不是 `bootrec` 文件 |
| `format_version` | string，`主版本.次版本` | — | 是 | 必填；读到不认识的格式即为损坏（第 7 节） |
| `collection.collection_id` | string | — | 是 | 必填，采集的唯一标识；重复导入的幂等键之一 |
| `collection.machine_id` | string | — | 是 | 必填，与 `entities.jsonl` 的 machine 行一致 |
| `collection.boot_id` | string | — | 是 | 必填，与 `entities.jsonl` 的 boot 行一致 |
| `collection.mode` | enum(`ssh_collection`、`journal_import`、`bootrec_import`) | — | 是 | 必填，表示数据怎么来的 |
| `collection.created_realtime_ns` | int64 或 `null` | 纳秒（Unix 纪元） | 否 | `null` 表示产生方没有记录生成时刻 |
| `producer.name` | string | — | 是 | 必填，产生这份文件的程序名 |
| `producer.version` | string | — | 是 | 必填，程序版本；排查格式问题时先看这个 |
| `resources.available` | bool | — | 是 | 必填。`false` 表示整份文件没有任何资源指标（Journal-only 导入） |
| `resources.reason` | string 或 `null` | — | 否 | `available=false` 时应写明原因；`available=true` 时写 `null` |
| `journal.included` | bool | — | 是 | 必填。`false` 时 `journal.jsonl` 必须是空文件 |
| `counts.entities` | int | 行 | 是 | 必填，`entities.jsonl` 的非空行数 |
| `counts.records` | int | 行 | 是 | 必填，`records.jsonl` 的非空行数 |
| `counts.journal_entries` | int | 行 | 是 | 必填，`journal.jsonl` 的非空行数 |
| `entries` | object | — | 是 | 必填，键是三个 `.jsonl` 的名字，值是该文件的 `sha256` 与 `size_bytes` |

`resources.available` 与单条指标流的 `available` 分工不同：前者回答「整份文件有没有资源指标」，
后者回答「这一条指标流有没有数据」。一份文件可以 `resources.available = true` 却仍有若干流
`available = false`（部分采集失败）。

`counts` 存在的意义是防止文件被截断：写入方先写完 `.jsonl` 再写 manifest，读取方核对行数就能
发现「写到一半掉电」这类损坏。

## 3. `entities.jsonl`

每行一个实体对象，`entity_type` 决定结构（定义见 `docs/data-model.md` 第 5 节）：

| `entity_type` | 说明 | 关键字段 |
| --- | --- | --- |
| `machine` | 被诊断的机器，每个文件恰好一行 | `machine_id`、`hostname` |
| `boot` | 这次启动，每个文件恰好一行 | `boot_id`、`machine_id`、`started_realtime_ns` |
| `service` | systemd 单元 | `entity_id`、`name` |
| `process` | 进程 | `entity_id`、`attributes.pid` |
| `user_session` | 桌面会话 / 登录会话 | `entity_id`、`name` |
| `cgroup` | cgroup 路径 | `entity_id`、`name` |
| `metric_stream` | 一条指标流 | `entity_id`、`name`、`unit`、`available`、`unavailable_reason` |

Collection 不出现在本文件里，它由 `manifest.json` 的 `collection` 段描述（每个文件恰好一次采集）。

校验会核对：machine 行与 boot 行各恰好一行、`machine_id`/`boot_id` 与 manifest 一致；每个
`entity.boot_id`、`entity.machine_id` 都能解析；`metric_stream` 的 `available` 与
`unavailable_reason` 互相匹配。

## 4. `records.jsonl`

每行一条统一记录，字段定义见 `docs/data-model.md` 第 3、4 节。六种 `kind` 混在同一个文件里，
按 `kind` 决定 `payload` 结构：

```json
{"record_id":"2fb95339a5d06813da89126d404dd12c","kind":"metric","machine_id":"machine:uos-PC","boot_id":"3f1c9a52-…","collection_id":"collection:full-0001","source_plugin":"resource-sampler","stream_id":"cpu","monotonic_ns":1000000000,"realtime_ns":1794009601000000000,"clock_domain":"boot_monotonic","entity":"metric:cpu.total.utilization","payload":{"name":"cpu.total.utilization","unit":"percent","value":12.5}}
```

记录层面校验的内容：

| 检查 | 不通过时的错误码 |
| --- | --- |
| 一行必须是 JSON 对象 | `E_RECORD_SYNTAX` |
| 统一字段齐全且类型正确 | `E_RECORD_FIELD` |
| `payload` 符合对应 `kind` 的字段表 | `E_PAYLOAD_FIELD` |
| `record_id` 是 32 位小写十六进制，且等于按内容算出的值 | `E_RECORD_ID_FORMAT`、`E_RECORD_ID_MISMATCH` |
| `monotonic_ns` 与 `realtime_ns` 至少一个非空 | `E_NO_TIMESTAMP` |
| `machine_id`/`boot_id`/`collection_id` 与 manifest 一致 | `E_RECORD_SCOPE` |
| `entity` 能解析到实体；`metric`、`span` 必须有 `entity` | `E_DANGLING_REF`、`E_MISSING_ENTITY_REF` |
| `metric` 记录指向 `metric_stream` 实体 | `E_ENTITY_TYPE_MISMATCH` |
| 同一个 `record_id` 不出现两次 | `E_DUPLICATE_RECORD` |
| Artifact / Profile 引用的文件在容器内存在 | `E_ARTIFACT_MISSING`、`E_ARTIFACT_PATH` |

## 5. `journal.jsonl`

每行一条 Journal 日志。字段名沿用 journald 的导出字段，不做重命名，减少导入时的映射错误：

```json
{"boot_id":"3f1c9a52-…","realtime_ns":1794009601200000000,"monotonic_ns":1200000000,"priority":6,"message":"Started A high performance web server…","cursor":"s=1b2c3d4e5f6; i=1","unit":"nginx.service","identifier":"systemd","fields":{"_SYSTEMD_UNIT":"nginx.service"}}
```

字段表：

| 名称 | 类型 | 单位 | 必填 | 缺失语义 |
| --- | --- | --- | --- | --- |
| `boot_id` | string | — | 是 | 必填，必须等于 manifest 的 `boot_id` |
| `realtime_ns` | int64 | 纳秒（Unix 纪元，源自 `__REALTIME_TIMESTAMP`） | 是 | 必填。Journal 条目一定带墙钟时间，缺失说明导出过程有问题 |
| `monotonic_ns` | int64 或 `null` | 纳秒（源自 `__MONOTONIC_TIMESTAMP`） | 否 | `null` 表示该条目没有单调时钟读数（部分内核与外部来源的日志没有） |
| `priority` | int 0–7 | — | 是 | 必填，syslog 级别；导出缺失时用 6（info）并在 `fields` 里标注来源 |
| `message` | string | — | 是 | 必填，允许空串（Journal 里确有 message 为空的结构化条目） |
| `cursor` | string 或 `null` | — | 否 | `null` 表示导出时没有保留游标 |
| `unit` | string 或 `null` | — | 否 | `null` 表示这条日志不属于某个 systemd 单元 |
| `identifier` | string 或 `null` | — | 否 | `SYSLOG_IDENTIFIER` 原值（如 `systemd`、`kernel`） |
| `fields` | object（字符串键值） | — | 否 | 缺省为空对象；只保留导入方需要的原始字段，值统一为字符串 |

Journal 与记录的关系：Journal 条目本身是原始日志，独立成文件；由日志推导出来的事件（如
`systemd.unit.started`）写成 `records.jsonl` 里的 `event` 记录，`attributes.derived_from = "journal"`
标明来源。两处都保留，分析结论可以用 `evidence_refs` 指向事件记录，展示则直接读 Journal 原文。

校验额外核对：

- `journal.included = false` 却有条目 → `E_JOURNAL_FLAG`；
- 条目的 `boot_id` 与 manifest 不一致 → `E_JOURNAL_SCOPE`（多启动压缩包必须拆分）；
- 条目提到的单元没有对应 `service` 实体 → `E_JOURNAL_UNIT`（警告，不影响通过）。

## 6. `artifacts/`

存放原始文件，例如 perf 采样、第三方导出的日志包。要求：

- 所有文件必须放在 `artifacts/` 前缀下，允许子目录（`artifacts/perf/cpu.data`）。
- 每个文件都必须有一条 `artifact` 记录索引它（`payload.path` = 容器内路径），或者被某条
  `profile` 记录引用（`payload.artifact_path`）。没有被引用的文件报 `E_ORPHAN_ARTIFACT`（警告）。
- `artifact` 记录的 `size_bytes`、`sha256` 必须与容器内实际文件一致，否则导入方无法确认原始数据
  没被改动。`artifact` 记录的 `payload.sha256` 与 `manifest.entries` 里登记的是两回事：前者管
  `artifacts/` 里的原始文件，后者管三个 `.jsonl` 数据文件。
- 没有原始文件时，`artifacts/` 与 `artifact`、`profile` 记录都可以整体不出现。

## 7. 格式版本与向后兼容

版本号形如 `主版本.次版本`，写在 `manifest.format_version`。当前实现是 `1.0`。

| 情况 | 读取方行为 |
| --- | --- |
| 主版本相同、次版本 ≤ 自己支持的 | 按本规范读取 |
| 主版本相同、次版本更大（如读到 `1.9`，实现是 `1.0`） | 按 `1.0` 读取，忽略不认识的字段，报 `E_NEWER_MINOR_VERSION`（警告，不阻断导入） |
| 主版本不同（如读到 `2.0`） | 直接拒绝，报 `E_UNSUPPORTED_VERSION`，不做降级猜测 |

同一主版本内的兼容规则：

1. **允许新增**：新加可选字段、新加 `attributes` 里的键、新加错误码、新加 `unit` 的取值。
   老读取方忽略它们即可。
2. **不允许改动**：字段改名、字段类型变化、必填性收紧或放宽、枚举取值改语义、`record_id`
   计算方式变化。这些一律升主版本。
3. **不允许删除必填字段**。删除可选字段等于改动语义，升主版本。
4. **不认识的 `kind` 或 `entity_type`**：v1 视为错误（`E_RECORD_FIELD` / `E_ENTITY_TYPE`），
   因为它们是闭集合。新增类别必须升主版本。
5. **不认识的顶部条目**：例如 `1.1` 加了 `metrics.idx`。老读取方可以忽略，不会报错。

为什么主版本不匹配不尝试读取：`bootrec` 是存储与导入的输入，猜错字段语义会把错数据写进数据库，
事后很难区分「数据本身错」还是「解析错」。宁可让任务失败并提示升级，也不静默降级。

## 8. 完整性校验

校验分四层，任何一层不通过都让校验整体失败：

| 层 | 检查 | 说明 |
| --- | --- | --- |
| 容器 | 能作为 ZIP 打开、没有同名条目、`manifest.json` 存在 | 结构层面的坏文件 |
| 清单 | manifest 字段齐全、`format` 正确、版本受支持 | 语义层面的坏文件 |
| 数据文件 | `entities.jsonl`、`records.jsonl`、`journal.jsonl` 都存在，`size_bytes` 与 `sha256` 与 manifest 一致，行数与 `counts` 一致 | 防止截断与改动 |
| 交叉引用 | 引用能解析到实体、作用域字段一致、指标流可用性与记录一致、`record_id` 与内容一致 | 防止「结构正确但内容自相矛盾」 |

实现细节：

- `sha256` 与 `size_bytes` 覆盖 `.jsonl` 的完整字节（含行尾换行），不算 ZIP 自身元数据。
- `record_id` 用 `blake2b`（128 位）对「除 `record_id` 之外的全部字段」做规范化 JSON 摘要，
  规范化规则是键排序、无多余空白、保留非 ASCII 字符。校验会重算一遍，与记录里的值不一致就报
  `E_RECORD_ID_MISMATCH`。
- 引用的原始文件同样核对 `artifact` 记录里登记的 `size_bytes` 与 `sha256`，不一致分别报
  `E_ARTIFACT_SIZE`、`E_ARTIFACT_CHECKSUM`。
- `.jsonl` 里的字节不是合法 UTF-8（文件损坏、传输截断、非 UTF-8 生产者）时，按行报
  `E_JSONL_ENCODING` 并跳过该行，不抛异常。
- 校验器对「读不了的文件」与「坏文件」一视同仁：文件不存在报 `E_FILE_NOT_FOUND`，路径是目录、
  没有读权限或其它 `OSError` 报 `E_FILE_UNREADABLE`，ZIP 条目读取失败报 `E_ENTRY_UNREADABLE`，
  三者都返回校验报告，不向上抛异常。

## 9. 三份样例

样例在 `docs/samples/bootrec-v1/`，由
`cd backend && uv run python -m system_analyzer.tools.make_bootrec_samples` 生成，生成结果固定
（时间戳、校验和、ZIP 时间都是常量或按内容算出），所以可以直接进仓库并逐字节比对。

| 文件 | 覆盖的场景 | 校验结论 |
| --- | --- | --- |
| `full.bootrec` | 六种对象都有：Systemd 事件与 Span、CPU/内存/磁盘指标、perf Profile 与 Artifact、分析结论 Annotation、Journal | 通过；`资源数据: 有（metric 记录 9 条）` |
| `journal-only.bootrec` | Journal 导入：只有从日志推导出来的事件，没有任何资源指标，没有 `metric_stream` 实体 | 通过；`资源数据: 无`（`resources.available=false` 且 0 条 metric） |
| `resources-missing.bootrec` | 部分资源缺失：CPU 有采样；内存与磁盘两条流 `available=false` 并写明原因，且**没有**对应记录 | 通过；`不可用指标流: metric:disk.read_bytes_per_second, metric:memory.used` |

三份样例的共同点：一台机器（`machine:uos-PC`）、一次启动（`3f1c9a52-…`）、一次采集；时间是固定
的示例值（`1794009600000000000` 纳秒 = 2026-11-07T00:00:00Z），不是真实采集时间，不要用它做业务判断。

看样例内容：

```bash
cd backend && uv run python -m system_analyzer.tools.validate_bootrec ../docs/samples/bootrec-v1/full.bootrec
cd .. && unzip -p docs/samples/bootrec-v1/full.bootrec records.jsonl | head -3
```

## 10. 与下游任务的接口约定

| 任务 | 用本文档的哪部分 |
| --- | --- |
| 1.1 存储与核心领域模型 | 第 2～5 节的字段表与第 8 节的幂等键；表结构按统一记录字段 + `payload` 建 |
| 1.3 Bootrec Importer | 整份文档：解析顺序、校验、版本拒绝规则、`record_id` 重算 |
| 1.4 Journal Archive Importer | 第 5 节字段表与多启动拆分规则；输出必须是符合本文档的 `.bootrec` |
| 1.5 统一时间线查询与回放 | 第 3、4 节的 `kind` 与时间字段；`stream_id`/`entity` 决定轨道归属 |
| 2.x 分析插件 | `annotation` 记录与 `evidence_refs`；结论只能追加，不改原始记录 |
| 3.x 采集插件 | 第 2～4 节的必填字段、缺失语义（尤其 `resources.available` 与 `metric_stream.available`） |
| 4.2 时间线界面 | 「数据缺失」在界面上的表达方式由 `data-model.md` 第 7 节决定 |

导入方必须做到：遇到 `error` 就整份拒绝并报错，不要跳过坏记录继续导入 —— 跳过会让「文件损坏」
和「这台机器当时没采到这条数据」变得无法区分。

## 11. 校验错误码

`error` 让校验不通过；`warning` 只提示，不影响通过。

| 错误码 | 级别 | 含义 |
| --- | --- | --- |
| `E_FILE_NOT_FOUND` | error | 文件不存在 |
| `E_FILE_UNREADABLE` | error | 文件读不了（路径是目录、没有读权限或其它 `OSError`） |
| `E_ENTRY_UNREADABLE` | error | 容器内某个条目读取失败（损坏、加密或用了不支持的压缩方法） |
| `E_NOT_A_ZIP` | error | 不是 ZIP 容器 |
| `E_DUPLICATE_ENTRY` | error | 容器内有同名条目 |
| `E_MISSING_ENTRY` | error | 缺少 `manifest.json` 或三个 `.jsonl` 中的某个 |
| `E_MANIFEST_JSON` | error | `manifest.json` 不是合法 UTF-8 JSON |
| `E_MANIFEST_FIELD` | error | manifest 字段缺失或类型错误 |
| `E_FORMAT_NAME` | error | `format` 不是 `bootrec` |
| `E_UNSUPPORTED_VERSION` | error | 主版本不受支持 |
| `E_NEWER_MINOR_VERSION` | warning | 次版本更高，按当前版本读取 |
| `E_MISSING_CHECKSUM` | error | manifest 没有登记某个数据文件的校验和 |
| `E_UNKNOWN_CHECKSUM` | warning | manifest 登记了规范之外的文件 |
| `E_SIZE_MISMATCH` | error | 数据文件大小与 manifest 不一致 |
| `E_SHA256_MISMATCH` | error | 数据文件校验和与 manifest 不一致 |
| `E_COUNT_MISMATCH` | error | 行数与 manifest 的 `counts` 不一致 |
| `E_JSONL_SYNTAX` | error | `.jsonl` 某行不是合法 JSON |
| `E_JSONL_ENCODING` | error | `.jsonl` 某行的字节不是合法 UTF-8 |
| `E_ENTITY_SYNTAX` | error | 实体行不是 JSON 对象 |
| `E_ENTITY_TYPE` | error | `entity_type` 不在闭集合内 |
| `E_ENTITY_FIELD` | error | 实体字段缺失或类型错误 |
| `E_ENTITY_MISMATCH` | error | 实体 id 与 manifest 的 `collection` 段不一致 |
| `E_MISSING_ENTITY` | error | 缺少 manifest 声明的 machine 或 boot 行 |
| `E_DUPLICATE_ENTITY` | error | 同一个实体 id 出现两行 |
| `E_STREAM_REASON` | error | 指标流的 `available` 与 `unavailable_reason` 自相矛盾 |
| `E_DANGLING_REF` | error | 引用的实体不存在 |
| `E_RECORD_SYNTAX` | error | 记录行不是 JSON 对象 |
| `E_RECORD_FIELD` | error | 统一字段缺失或类型错误 |
| `E_PAYLOAD_FIELD` | error | `payload` 不符合对应 `kind` 的字段表 |
| `E_RECORD_ID_FORMAT` | error | `record_id` 不是 32 位小写十六进制 |
| `E_RECORD_ID_MISMATCH` | error | `record_id` 与内容算出的值不一致 |
| `E_NO_TIMESTAMP` | error | `monotonic_ns` 与 `realtime_ns` 都为空 |
| `E_RECORD_SCOPE` | error | 记录的作用域字段与 manifest 不一致 |
| `E_MISSING_ENTITY_REF` | error | `metric` 或 `span` 记录没有 `entity` |
| `E_ENTITY_TYPE_MISMATCH` | error | `metric` 记录指向的实体不是 `metric_stream` |
| `E_DUPLICATE_RECORD` | error | 同一个 `record_id` 出现多次 |
| `E_ARTIFACT_PATH` | error | 原始文件路径不在 `artifacts/` 下 |
| `E_ARTIFACT_SHA256` | error | `artifact` 记录的 `sha256` 格式错误 |
| `E_ARTIFACT_SIZE` | error | 原始文件大小与 `artifact` 记录不一致 |
| `E_ARTIFACT_CHECKSUM` | error | 原始文件内容与 `artifact` 记录的 `sha256` 不一致 |
| `E_ARTIFACT_MISSING` | error | 引用的原始文件在容器内不存在 |
| `E_ORPHAN_ARTIFACT` | warning | `artifacts/` 下有文件但没有任何记录引用 |
| `E_RESOURCES_FLAG` | error | manifest 声明无资源数据，却有 metric 记录 |
| `E_UNAVAILABLE_STREAM_HAS_DATA` | error | 不可用的指标流下写了数据 |
| `E_JOURNAL_SYNTAX` | error | Journal 行不是 JSON 对象 |
| `E_JOURNAL_FIELD` | error | Journal 字段缺失或类型错误 |
| `E_JOURNAL_FLAG` | error | manifest 声明不含 Journal，却有 Journal 条目 |
| `E_JOURNAL_SCOPE` | error | Journal 条目的 `boot_id` 与 manifest 不一致 |
| `E_JOURNAL_UNIT` | warning | Journal 提到的单元没有对应 `service` 实体 |
