# Journal 压缩包导入

本文说明如何把一份 Journal 归档变成统一存储里的诊断数据：不用 SSH，只有「上传文件 → 导入 → 查询」
三步。实现是 `backend/src/system_analyzer/importers/journal_archive.py`（解析与归一化）与
`backend/src/system_analyzer/importers/journal_archive_importer.py`（插件入口，id
`journal.archive`）。

本文只描述**导入**。从日志推导分析结论属于 2.x；时间轴的排序与回放属于 1.5；上传任务的 HTTP 接口
与任务状态属于 1.6。时间字段的语义以 `docs/time-base.md` 为准，记录与实体的字段以
`docs/data-model.md` 为准，本文不重复定义它们。

## 1. 支持哪些输入

| 输入 | 判定依据 | 说明 |
| --- | --- | --- |
| `journalctl -o export` 文本 | 首行是 `字段=值` | 二进制字段（如 `SYSLOG_RAW`）整段跳过，不解码 |
| Journal 原生文件 | 文件头 `LPKSHHRH` | 直接读 journald 的二进制对象，不调用 `journalctl` |
| gzip / xz / bzip2 单文件 | 魔数 | 例如 `journalctl -o export … \| gzip > boot.export.gz` |
| tar 包（可再压缩） | tar 头 `ustar` | 打包 `*.journal` 或导出文本；目录与其它文件忽略并记录 |

zstd 压缩的外层容器与 zstd / lz4 压缩的字段**不支持**：仓库没有这两者的解码依赖，本任务也不允许
新增依赖。外层是 zstd 时整份拒绝并说明原因；字段级是 zstd / lz4 时跳过该字段，把跳过次数写进
`ParsedJournal.skipped_fields` 与每条记录的 `payload.attributes.skipped_fields`，绝不悄悄当作
「当时就没有这个字段」。

## 1.1 体积上限与完整性

解压后的体积有上限，默认 **512 MiB**（`DEFAULT_MAX_DECOMPRESSED_BYTES`）。超限时整份拒绝，错误里
同时写出已占用字节数与上限。上限按**解压后**的字节数计，因为压缩前的体积说明不了内存开销：仓库里
的样例 `boot.journal.gz` 只有 10 KB，解开是 8 MiB 的预分配 Journal 文件。

预算同时管**事件条数**：解析出的每条事件按 6 KiB 折算计入（实测样例上字段字典 2176 B、记录字典
3788 B，合计约 6 KiB）。只按字节算的话，一份体积没超限、却拆成几百万条事件的导出文本仍然会把内存
吃光——512 MiB 的预算最多容得下约 8.7 万条纯事件，其余额度留给解压出的字节。

文件与 tar 成员都按 64 KiB 分块读入、分块解压，累计一超过上限就立即中止；tar 的成员逐个解析完就
释放，同一时刻只有整份 tar 加上当前这一份成员。注意 tar 的预算同时累计「外层 tar 字节」与「成员
解压字节」，所以同样的内容打成 tar 会比单个文件更早触到上限。

**输入必须完整。** 被截断的归档（上传中断、拷贝不完整、`head` 截取）一律整份拒绝，不会被当成
「少了几条日志但导入成功」——否则最后一条记录里会留着被切断的半句话，分析结论就建在缺失数据上，
平台还留不下任何痕迹。两条输入各自有判据：

- **压缩成员**都要读到结束标记，否则报「压缩流不完整」；`cat a.gz b.gz` 这种多成员拼接会逐个成员
  读完，不丢后续数据。
- **未压缩的 `-o export` 文本**要求每条事件都以空行结尾。`journalctl -o export` 对每个条目都会多打
  一个空行，真实文件因此以空行结束；末尾没有空行就说明这份导出被截断了。整份拒绝并提示重新导出。
- **每个条目必须有 `__REALTIME_TIMESTAMP`**。`docs/bootrec-v1.md` 第 5 节把 journal 条目的
  `realtime_ns` 定为必填，`docs/time-base.md` 5.1 也写明缺失时「该条目不能进 `journal.jsonl`」；
  只有单调读数的条目整份拒绝，而不是写一条 `realtime_ns` 为 `null` 的记录（`data-model.md` 第 3 节
  的「两个时间字段至少一个非空」是统一记录的下限，这里按 journal 的专项规定收紧）。

部署需要导入更大的归档时，用插件配置调高：

```json
{"max_decompressed_bytes": 2147483648}
```

`config_schema` 会先按 JSON Schema 校验这个值（必须是正整数）；直接构造 `PluginContext` 的调用路径
（测试、将来的 Worker 接线）不过 Core 的校验，插件自己也会挡住非正整数。

## 2. 导入步骤

导入是两次调用，与「上传」和「解析」分开这一步一致：

1. **上传**：文件落到 Worker 能读到的路径，算出整份文件的 `sha256`。1.6 的接口只负责收文件、
   建任务、回状态；本任务不提供 HTTP 接口。
2. **解析与写入**：`parse_journal_archive()` 得到 Journal 原始字段，`build_bundle()` 归一化成
   一条采集、若干实体和若干 `event` 记录，`DiagnosticStore.import_collection()` 在**一个事务**里
   写入并推进导入状态。插件入口 `JournalArchiveImporter.import_source()` 就是第 2 步的插件形态：
   它是 `async` 方法，通过 `context.data` 调同一个整份写入入口，自己不碰数据库；体积上限取自
   `context.config` 的 `max_decompressed_bytes`（不填就是 512 MiB，见第 1.1 节）。

下面的命令可直接复跑，用的是仓库里的样例 `backend/tests/fixtures/journal_archive/boot.journal.gz`
（由 `systemd-journal-remote --compress=no` 从同目录的 `boot.export` 生成，是一份真实的 Journal
原生文件，内容固定）。先起本地库并建表：

```bash
scripts/dev-db.sh start
cd backend
cp -n .env.example .env
uv run alembic upgrade head
```

```bash
# 清空本地开发库里的诊断数据（demo 需要从空表开始；别对真实环境执行）
uv run python - <<'PY'
import asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from system_analyzer.core.config import get_settings

async def main() -> None:
    engine = create_async_engine(get_settings().database_url)
    async with engine.begin() as conn:
        for table in ("records", "entities", "collections", "boots", "machines"):
            await conn.execute(text(f"DELETE FROM {table}"))
    await engine.dispose()

asyncio.run(main())
PY

# 上传 → 导入 → 查询
uv run python - <<'PY'
import asyncio
from pathlib import Path

from sqlalchemy.ext.asyncio import create_async_engine

from system_analyzer.core.config import get_settings
from system_analyzer.domain.bootrec import RecordKind
from system_analyzer.domain.time_base import format_boot_relative
from system_analyzer.importers.journal_archive import (
    archive_digest,
    build_bundle,
    parse_journal_archive,
    summarise,
)
from system_analyzer.storage import DiagnosticStore

archive = Path("tests/fixtures/journal_archive/boot.journal.gz")
parsed = parse_journal_archive(archive)
bundle = build_bundle(parsed, source_sha256=archive_digest(archive))
print("解析:", len(parsed.entries), "条事件;", summarise(bundle))


async def main() -> None:
    engine = create_async_engine(get_settings().database_url)
    store = DiagnosticStore(engine)
    try:
        out = await store.import_collection(
            bundle.collection, bundle.entities, bundle.records,
            source_sha256=bundle.source_sha256, resources=bundle.resources,
        )
        print("导入:", out.status.value, "写入记录", out.records_written, "写入实体", out.entities_written)
        again = await store.import_collection(
            bundle.collection, bundle.entities, bundle.records,
            source_sha256=bundle.source_sha256, resources=bundle.resources,
        )
        print("再上传同一份:", again.records_written, again.skipped_reason)
        state = await store.get_collection(bundle.collection.collection_id)
        print("采集:", state.import_status.value, "资源数据:", state.resources_available, state.resources_reason)
        cid = bundle.collection.collection_id
        records = await store.list_records_in_collection(cid)
        print("记录:", len(records), sorted({r.kind.value for r in records}))
        metrics = await store.list_records_in_collection(cid, kinds=[RecordKind.METRIC, RecordKind.PROFILE])
        print("metric+profile 记录:", len(metrics))
        first = records[0]
        print("第一条:", first.monotonic_ns, "→ boot 内", format_boot_relative(first.monotonic_ns))
        print("        ", first.realtime_ns, first.clock_domain.value, first.entity)
        print("完整性复核:", await store.check_collection_integrity(cid) or "无问题")
    finally:
        await engine.dispose()


asyncio.run(main())
PY
```

实际输出：

```text
解析: 4 条事件; JournalArchiveSummary(machine_id='machine:uos-PC', boot_id='abc7451f-65c8-4be8-ab01-4b9a1e49a8b9', collection_id='journal-import:machine:uos-PC:abc7451f-65c8-4be8-ab01-4b9a1e49a8b9', hostname='uos-PC', kernel_version='6.6.0-amd64-desktop', entry_count=4, service_units=('service:nginx.service', 'service:session-674.scope', 'service:sshd.service'))
导入: imported 写入记录 4 写入实体 3
再上传同一份: 0 同一份文件已经导入过（source_sha256 相同）
采集: imported 资源数据: False Journal 归档只含日志，没有 CPU、内存、磁盘采样
记录: 4 ['event']
metric+profile 记录: 0
第一条: 175830828563000 → boot 内 2d 00:50:30.828563000
         1794367420210127000 boot_monotonic service:session-674.scope
完整性复核: 无问题
```

集成测试走的是真实链路（插件 `import_source` → 适配器 → `import_collection` → 查询），跑法：

```bash
cd backend && uv run pytest -m integration tests/integration/test_journal_archive_import.py -v
```

## 3. 数据落到哪里

1.0 不新增表，一份归档写到现有五张表里：

| 表 | 这份归档写什么 |
| --- | --- |
| `machines` | 一行：`machine_id = machine:<hostname>`，`hostname` 取自 `_HOSTNAME` |
| `boots` | 一行：`boot_id` 取自 `_BOOT_ID`（归一化成 8-4-4-4-12） |
| `collections` | 一行：`mode = journal_import`，`source_sha256` = 整份归档的摘要，`resources_available = false` 并写明原因 |
| `entities` | 日志里出现过的 `_SYSTEMD_UNIT`，每个一条 `service` 实体 |
| `records` | 每条 Journal 条目一条 `event` 记录 |

没有独立的 Journal 表：Journal 条目本身就是「瞬时事件」，与 `event` 记录一一对应；由日志推导出来的
语义事件（例如 `systemd.unit.started`）属于 2.x 的分析插件，它们用
`payload.evidence_refs` 指向这里的 `event` 的 `record_id`，分析结论因此能指回原文。

## 4. 字段映射

| Journal 原始字段 | 落到哪里 | 说明 |
| --- | --- | --- |
| `__MONOTONIC_TIMESTAMP` | `monotonic_ns`（微秒 × 1000） | 缺失 → `null`，且 `clock_domain = wall_realtime` |
| `__REALTIME_TIMESTAMP` | `realtime_ns`（微秒 × 1000） | 必填；缺失说明导出过程有问题，整份拒绝（`bootrec-v1` 第 5 节，有用例） |
| `_BOOT_ID` | `boot_id` | 归一化成小写 8-4-4-4-12 |
| `_HOSTNAME` | `machine_id`、Machine 实体的 `hostname` | 必填；一份归档只能有一台机器 |
| `_KERNEL_VERSION` | Machine / Boot 的 `kernel_version` | 没有则 `null` |
| `MESSAGE` | `payload.message` | 允许空串（Journal 里确有 message 为空的结构化条目） |
| `PRIORITY` | `payload.attributes.priority` + `payload.severity` | 缺失用 6；≤3 → `error`，4 → `warning`，≥5 → `info` |
| `_SYSTEMD_UNIT` | `payload.attributes.unit`，以及记录的 `entity = service:<unit>` | 同时生成对应的 `service` 实体 |
| `SYSLOG_IDENTIFIER` | `payload.attributes.identifier` | |
| `__CURSOR` | `payload.attributes.cursor` | 原生文件不存游标，此时没有这一项 |
| `_PID` | `payload.attributes.pid`（整数） | |
| `_SYSTEMD_INVOCATION_ID` | `payload.attributes.invocation_id` | |
| 其余全部字段 | `payload.attributes.fields`（原样字符串） | 「指回原文」的落点 |

`payload.event_type` 固定为 `journal.entry`，`source_plugin` 固定为 `journal.archive`，
`stream_id` 固定为 `journal`，`payload.attributes.derived_from` 固定为 `journal`。

关联信息怎么落：Boot ID 落成 Boot 实体；Unit 落成 `service` 实体，记录用 `entity` 指向它；PID 与
Invocation ID 随每条日志变化，做成实体只会产生大量一次性实体（同一个 PID 会复用，同一个单元在一个
boot 里也会有多个 Invocation ID），所以留在 `payload.attributes` 里。没有 `_SYSTEMD_UNIT` 的日志
（例如内核日志）`entity` 为 `null`。

## 5. 时间换算与可复核示例

换算不在本模块实现：`domain.time_base.journal_event_position()` 是唯一一处，规则见
`docs/time-base.md` 第 5 节。本模块只负责把两种输入都变成「Journal 原始字段字典」——原生文件的
时间戳不在字段里而在 ENTRY 对象上，这里补成与 `-o export` 相同的字段名，两种输入因此走同一条换算
路径。

样例里第一条事件同时出现在 `docs/time-base.md` 第 5.3 节，可以对照复核：

| 步骤 | 计算 | 结果 |
| --- | --- | --- |
| Boot ID 归一化 | `abc7451f65c84be8ab014b9a1e49a8b9` | `abc7451f-65c8-4be8-ab01-4b9a1e49a8b9` |
| 单调时钟 | `175830828563 µs × 1000` | `monotonic_ns = 175830828563000` |
| 墙钟 | `1794367420210127 µs × 1000` | `realtime_ns = 1794367420210127000` |
| boot 内位置 | `format_boot_relative(175830828563000)` | `2d 00:50:30.828563000` |

`clock_domain = boot_monotonic`（有单调读数），排序键按 `docs/time-base.md` 第 7.1 节落在
`(machine_id, boot_id)` 之内。换算后的纳秒低三位恒为 0，不代表真有纳秒精度。

## 6. 没有资源数据时怎么表达

Journal 归档里没有 CPU、内存、磁盘采样，按 `docs/data-model.md` 第 7 节第一行处理：

- 采集上写 `resources_available = false`，`resources_reason = "Journal 归档只含日志，没有 CPU、内存、磁盘采样"`；
- 不写任何 `metric` 记录，也不造 `metric_stream` 实体（缺失不得用 0 或占位值代替）；
- Boot 的 `started_realtime_ns` 写 `null`：Journal-only 导入没有采集到启动时刻，data-model 第 5.2
  节明确禁止用估算值代替。用某条事件的 `realtime_ns − monotonic_ns` 能估出启动锚点，但那是估计值，
  不是实测值；读取方要用它请自己按 `docs/time-base.md` 第 4 节算，并带上误差。

集成测试断言导入后 `metric` 与 `profile` 记录数都是 0、`metric_stream` 实体为 0。

## 7. 幂等

两级键，与 `docs/data-model.md` 第 8 节一致：

1. **整份文件的摘要** `sha256`（本模块算，存进 `collections.source_sha256`）。同一份文件再上传一次，
   直接跳过，不读记录。
2. **记录键** `(collection_id, record_id)`。`record_id` 是按内容算的摘要，所以「同一份日志换个打包
   方式再上传」（文件摘要变了，记录内容没变）也不会产生重复记录。

`collection_id` 由 **`(machine_id, boot_id)`** 算出：`journal-import:<machine_id>:<boot_id>`。不用文件
摘要派生，是因为那样「换个打包方式的同一份日志」会落到另一条采集上，第二级幂等就失效了。代价是：
同一个 boot 的两份**不同时间范围**导出会合并进同一条采集——记录是按时间可排序的事件，合并不丢信息；
要把它们分开，应按 boot 分次管理采集（属于 1.5/1.6 的范围）。

## 8. 多启动与多机器：整份拒绝

一份归档里出现两个以上的 `_BOOT_ID`，或者两台以上的机器，整份拒绝并说明原因，不做拆分。理由：

- 一条记录必须属于唯一一次启动（`docs/bootrec-v1.md` 第 5 节、`docs/data-model.md` 第 3 节）；
- 硬拆会把一次上传变成多次采集，`collection_id` 的幂等含义随之失效，重试还会再拆一遍；
- 拆出来的多条采集在任务结果里怎么表达、失败时哪一份回滚，都没有契约，属于超出本任务的设计。

要导入多启动，请按 boot 分别 `journalctl -b <id> -o export` 导出、分别上传。

## 9. 已知限制

| 限制 | 影响 | 处理 |
| --- | --- | --- |
| zstd / lz4 压缩的字段解不开 | 少数长字段（常见是长 `MESSAGE`）缺失 | 跳过并计数，记录的 `payload.attributes.skipped_fields` 写明 |
| 外层 zstd 容器（`.tar.zst` 等）不支持 | 无法导入 | 整份拒绝并提示改用 gzip / xz / bzip2 或先导出成文本 |
| 解压后超过上限（默认 512 MiB） | 整份拒绝 | 报出已占用字节数与上限；用插件配置 `max_decompressed_bytes` 调高，或按时间范围拆分导出 |
| 压缩流被截断（上传中断、拷贝不完整） | 整份拒绝 | 报「压缩流不完整」；重新上传完整文件 |
| 未压缩导出文本被截断（最后一条没有以空行结尾） | 整份拒绝 | 报「导出文本不完整」；重新导出或重新上传完整文件 |
| 条目缺 `__REALTIME_TIMESTAMP` | 整份拒绝 | 按 `docs/bootrec-v1.md` 第 5 节，`realtime_ns` 必填；缺失说明导出过程有问题 |
| `-o export` 里的二进制字段（`SYSLOG_RAW` 等） | 不保留 | 整段跳过；换算与检索不需要它们 |
| 原生文件没有游标 | `payload.attributes.cursor` 缺失 | 游标是 journald 按需生成的，文件里本来不存 |
| 同一份日志分别用原生文件和导出文本各上传一次 | 两边 `record_id` 不同（游标差异），会出现两份内容相同的记录 | 一种来源只上传一次；`collection_id` 相同，不会多出一条采集 |
| `_SYSTEMD_UNIT` 是 scope/slice 时 | 也建成 `service` 实体 | data-model 的实体类型里没有 scope/slice，最接近的是 service；`name` 保留原值 |

## 10. 偏差清单

| # | 偏差 | 原因 | 由谁闭环 |
| --- | --- | --- | --- |
| 1 | `import_source` 是 `async` 方法，契约第 3.3 节写的是同步签名 | 导入要等一次完整的 `import_collection` 事务，而 Worker 是异步进程。1.3 的 `BootrecImporter` 做了同样的改动 | 建议在 `docs/plugin-contracts.md` 第 3.3 节把 Importer 入口改成异步，交 PL 决定 |
| 2 | 数据入口在 `PluginDataAccess`（`append_records` / `query_records`）之外要求 `import_collection`（`importers/journal_archive.py:CollectionImportAccess`） | 存储层要求记录的 `entity` 能在同一份采集里解析到、Machine 与 Boot 两行是写入前置条件（`storage/validation.py` 的 `entity_problems`、`_reference_problems`），三者还必须同一个事务写入。只写记录的入口装不下这些 | 1.3 已在 `importers/bootrec_importer.py` 声明同形的 `CollectionImportAccess`；两处应合并到公共位置，交 PL 决定放哪 |
| 3 | `docs/bootrec-v1.md` 第 10 节说 1.4 的输出「必须是符合本文档的 `.bootrec`」，本实现直接把 `event` 记录写进存储，不生成 `.bootrec` | 按 PL 在 1.4 任务交接里定的口径：Journal 条目写成 `event` 记录、`source_sha256` 走现有列、不新增表 | PL 已在交接里确认，此处只做记录 |
| 4 | `-o export` 的逐条扫描在本模块另写了一份（`iter_export_events`），`tools/journal_time.py` 里的 `parse_event` 只取第一条 | `tools/journal_time.py` 属于 0.3 的交付且本任务不允许改动；时间换算仍然只用它的 `journal_event_position`，二进制字段的长度头常量也从那里取，只有「切分多条事件」这一层是新的 | 若后续要给 `journal_time` 加多事件接口，可合并成一处 |
| 5 | 逐条日志的 PID / Invocation ID 落在 `payload.attributes` 而不是实体 | 见第 4 节 | 无 |
| 6 | 多启动选择「拒绝」而不是「拆分」 | 见第 8 节 | 若 1.6/1.5 需要自动拆分，需先有「一次上传产出多次采集」的契约 |
