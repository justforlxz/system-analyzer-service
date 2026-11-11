# bootrec v1 导入

本文说明任务 1.3 交付的 `.bootrec` 导入实现：怎么解析、怎么校验、写库走哪个入口、幂等与
`collection_id` 复用怎么判断，以及实现与既有契约之间的偏差。格式本身见 `docs/bootrec-v1.md`，
数据对象见 `docs/data-model.md`，插件接口见 `docs/plugin-contracts.md`。

代码位置：

| 模块 | 职责 |
| --- | --- |
| `backend/src/system_analyzer/importers/bootrec.py` | 解析、校验、第一级幂等键的计算，不碰数据库 |
| `backend/src/system_analyzer/importers/bootrec_importer.py` | 导入编排与 `bootrec.importer` 插件 |
| `backend/tests/bootrec_fixtures.py` | 四类夹具（正常包、缺字段包、坏校验和包、版本过高包） |
| `backend/tests/test_bootrec_importer.py` | 单元测试，不需要数据库 |
| `backend/tests/integration/test_bootrec_import_store.py` | 真实 PostgreSQL 上的导入与读回测试 |

## 1. 导入顺序

导入方按固定顺序处理，前三步都不写库，因此坏包不会在存储里留下任何痕迹：

1. **校验**：调用 `domain.bootrec.validate_bootrec`（任务 0.2 的实现）。容器、清单、数据文件、
   交叉引用四层检查与 `bootrec-v1.md` 第 11 节的错误码全部复用它，本任务不另写一套解析与校验。
   任何一个 `error` 都让整份包被拒绝，拒绝原因里带错误码、位置与说明；`warning`（次版本更高、
   容器里有没被引用的原始文件）随导入结果返回，不阻断导入。
2. **解析**：把 `manifest.json`、`entities.jsonl`、`records.jsonl`、`journal.jsonl` 读成领域对象。
   记录保留文件里的**原始字典**，因为 `record_id` 是按原始内容算的，写库时要用同一份内容核对。
3. **采集号复用检查**：见第 4 节。
4. **写入**：调 `DiagnosticStore.import_collection`，登记采集、写实体、写记录、推进导入状态在
   同一个事务里完成，失败整批回滚（这段由任务 1.1 实现，导入方不重复实现）。

## 2. 版本规则

沿用 `bootrec-v1.md` 第 7 节，不在导入方另加规则：

| 情况 | 导入行为 |
| --- | --- |
| 主版本相同、次版本不高于 1.0 | 正常导入 |
| 主版本相同、次版本更高（如 1.9） | 按 1.0 读取，导入结果里带 `E_NEWER_MINOR_VERSION` 警告 |
| 主版本不同（如 2.0） | 拒绝，报 `E_UNSUPPORTED_VERSION`，不做降级猜测 |

## 3. 两级幂等

| 级别 | 键 | 导入方做什么 |
| --- | --- | --- |
| 第一级 | 容器摘要 `source_sha256` | 每次导入都把摘要传给 `import_collection`；同一份文件再导入一次，存储层直接整批跳过（`records_written = 0`，`skipped_reason` 说明原因） |
| 第二级 | `(collection_id, record_id)` | 同一采集换一份文件导出（摘要不同、记录相同）时，逐条按记录唯一键去重，重复记录只计 `records_skipped` |

第一级摘要的算法是 `data-model.md` 第 8 节的规定：容器内每个文件条目按名字排序，依次喂入
「名字长度 + 名字 + 内容长度 + 内容」的 sha256。规范只写了「长度」没写编码，本实现统一用
**8 字节大端字节数**；目录条目（名字以 `/` 结尾）没有内容，不参与计算。用条目而不是文件字节，
是因为 ZIP 自身的时间戳、压缩等级、权限位不参与校验和，同一份数据重新打包会得到同一个摘要。

## 4. `collection_id` 复用的判定

`data-model.md` 第 8 节把这条判断交给导入方：存储层只会在完整性检查里报不一致，不会拒绝写入。
导入方在写库之前读一次 `get_collection(collection_id)`：

| 情况 | 导入行为 |
| --- | --- |
| 库里没有这个采集号 | 正常导入 |
| 已有采集号，机器与启动都相同，摘要也相同 | 正常导入，存储层按第一级幂等整批跳过 |
| 已有采集号，机器与启动都相同，摘要是另一份文件 | 按「同一采集的补充数据」导入，逐条按记录唯一键去重，并在导入结果里说明采集号被另一份文件用过 |
| 已有采集号，机器或启动不同 | **整份拒绝**，报出两个作用域与这份文件声称的作用域；否则采集会与它的记录、启动互相对不上 |

## 5. Journal 与 `artifacts/` 怎么处置

两者都**读出并统计，但不落库**：

- 1.1 的五张表（`machines`、`boots`、`collections`、`entities`、`records`）里没有 Journal 表，
  也没有原始文件表。写入对象是实体与记录，`artifact` 记录是原始文件的索引。
- `journal.jsonl` 的条目已经在 0.2 交付时按 `time-base.md` 第 5 节换算成纳秒，导入方照原值读出，
  只用来统计条目数；由日志推导出来的事件本来就已经在 `records.jsonl` 里，不重复生成。
- `artifacts/` 里的字节随 `.bootrec` 文件本身保存，与 `data-model.md` 第 6 节「原始文件只在容器内
  有效」一致；导入结果里列出文件名，供任务结果说明这份包带了哪些原始文件。

## 6. 插件怎么拿到写库入口

插件的整条导入路径都是**同步**的：`docs/plugin-contracts.md` 第 3.3 节把 `import_source` 声明为
同步方法、返回 `ImportResult`，第 4 节规定 Core 用 `PluginRegistry.invoke` 调它，第 3.6、4.4 节
规定插件抛出的非 `PluginError` 异常在那里被包成 `PluginExecutionError`。契约第 5 节的
`PluginDataAccess` 同样是同步接口（`append_records` 返回 `int`），所以数据入口也必须是同步的。

`PluginDataAccess` 只有逐条追加与只读查询，而导入需要「一整份采集一次写入」的事务。因此
`importers/bootrec_importer.py` 声明了 `CollectionImportAccess` 协议：

```python
def get_collection(self, collection_id: str) -> ...: ...
def import_collection(self, collection, entities, records, *,
                      source_sha256=None, resources=None) -> ...: ...
```

语义与 `DiagnosticStore` 的同名方法一致，区别只在于**同步**。Core 需要把存储层包成同步入口后放进
`context.data`（任务 1.1 交付的 `DiagnosticStore` 是协程，这张包装还没有人实现，属于 Core 的数据
入口实现）。插件不 import `system_analyzer.storage`，`test_plugin_implementations_never_import_storage`
守着这条边界。

如果 Core 错把异步存储层直接交给插件，`import_bootrec` 用 `inspect.isawaitable` 发现返回的是协程，
就抛 `PluginInputError` 并说明「导入需要同步入口」，不会出现「调用成功、实际什么都没导入」。

## 7. 怎样跑验证

```bash
cd backend && uv run ruff check . && uv run ruff format --check . && uv run pytest
```

真实数据库上的集成测试：

```bash
scripts/dev-db.sh start
cd backend && cp -n .env.example .env
uv run alembic upgrade head
uv run pytest -m integration
scripts/dev-db.sh stop
```

## 8. 偏差清单

1. **写库入口不是 `PluginDataAccess` 的两个方法**。1.1 交付的存储层没有实现 `PluginDataAccess`
   （它的 `append_records` 需要先有采集、签名也不同），而 `plugin-contracts.md` 第 9 节把
   「实现 `PluginDataAccess`」列为 1.1 的边界。本任务按 handoff 的要求改用 `import_collection`
   的整份采集事务入口，并以 `CollectionImportAccess` 协议描述它。
2. **Journal 条目与 `artifacts/` 文件不落库**，原因见第 5 节；1.1 没有对应的表，本任务也不允许
   改存储层。
3. 未改动父 issue 的 23 项拆分与 stage 分组。

## 9. 交给后续任务的接口

- **Core 的数据入口**（1.1 的补充，或 1.6 / 3.2 建导入任务时一起做）：把 `DiagnosticStore` 包成
  `CollectionImportAccess` 的同步实现，`get_collection` 与 `import_collection` 都是同步方法；
  证据用例见 `tests/integration/test_bootrec_import_store.py` 的 `SyncStoreAccess`（每次调用一条
  短命事件循环 + `NullPool`，只是测试里的桥接方式，不是生产建议）。
- **调用方**（1.6 导入 API、3.2 Worker 导入处理器）：按契约用
  `registry.invoke(plugin_id, lambda p, ctx: p.import_source(source, ctx), data=..., run_id=...)`
  调用，直接拿到 `ImportResult`，不需要 `await` 插件的返回值。
- **1.4 Journal Archive Importer**：`BUILTIN_PLUGINS` 里在 1.3 那一行之后追加自己的插件，清单
  保持可追加，不要重排已有内容。
