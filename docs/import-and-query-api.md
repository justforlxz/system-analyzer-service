# 导入与查询 API

本文说明任务 1.6 交付的对外接口：前端能独立完成「上传 Journal 压缩包 → 导入 → 查询时间线」三步，
并用同一套接口查询机器、启动记录、采集记录与时间线数据。

- 代码位置：
  - `backend/src/system_analyzer/api/`：路由、请求与响应模型、统一错误结构、同步数据入口。
  - `backend/src/system_analyzer/worker/import_jobs.py`：导入任务的 Worker 处理器。
- 接口契约的机器可读版本是 FastAPI 生成的 OpenAPI：
  `cd backend && uv run python -m system_analyzer.tools.export_openapi`，前端用
  `cd frontend && npm run api:generate` 生成 `src/lib/api/schema.d.ts`。
- 时间语义、轨道与三种轨道状态以 `docs/timeline-query.md` 为准；记录与实体字段以
  `docs/data-model.md` 为准；字段的换算与排序见 `docs/time-base.md`。本文不重复定义它们。

目录：

1. 接口清单
2. 三步流程
3. 统一错误结构
4. 分页与时间范围
5. 时间线接口
6. 导入任务与采集状态
7. 上传文件的存放
8. 复跑验证
9. 已知限制与偏差

## 1. 接口清单

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/imports` | 上传文件并提交导入任务（multipart：`file`，可选 `plugin_id`） |
| GET | `/api/imports/{job_id}` | 导入任务状态，以及它写回的采集 |
| GET | `/api/machines/{machine_id}` | 机器详情 |
| GET | `/api/machines/{machine_id}/boots` | 一台机器的启动记录（分页） |
| GET | `/api/machines/{machine_id}/boots/{boot_id}` | 启动详情 |
| GET | `/api/collections` | 采集记录列表（按机器、启动、导入状态筛选，分页） |
| GET | `/api/collections/{collection_id}` | 采集详情 |
| GET | `/api/collections/{collection_id}/records` | 一次采集里的记录（分页、时间范围、种类筛选） |
| GET | `/api/machines/{m}/boots/{b}/timeline/tracks` | 时间范围里的轨道与条目 |
| GET | `/api/machines/{m}/boots/{b}/timeline/state` | 指定时刻各轨道的状态 |
| GET | `/api/machines/{m}/boots/{b}/timeline/cursor` | 统一时间游标；带 `forward` 时前进或后退一帧 |
| GET | `/api/health` | 进程存活与版本 |

导入接口按内容自动选插件：`.bootrec` 容器由 `bootrec.importer` 处理，Journal 原生文件与
gzip / xz / bzip2 / tar 压缩包由 `journal.archive` 处理。也可以显式传 `plugin_id`，此时插件不认
这个文件会直接报格式错误。

## 2. 三步流程

1. **上传**：`POST /api/imports` 把文件写到上传目录、算出整份文件的 `sha256`、确认有插件认得它，
   把导入任务写进 `jobs` 表，返回 `202` 与 `job_id`。请求线程里**不解析**文件内容。
2. **导入**：Worker 领取任务后，在同步边界内调用插件的 `import_source`：插件解析文件，经
   `PluginContext.data`（`api/sync_store.py` 的 `SyncStoreAccess`）把整份采集写进 PostgreSQL。
   写库由存储层的 `import_collection` 在**一个事务**里完成，失败整批回滚并把采集标成 `failed`。
3. **查询**：`GET /api/imports/{job_id}` 看任务与采集状态；采集状态到 `imported` 之后，
   用 `timeline/*` 或 `/api/collections/{id}/records` 取数据。

导入任务类型只有一个：`import.source`。它在 `worker/main.py` 的 `default_handlers()` 里注册，
处理器实现在 `worker/import_jobs.py`。

## 3. 统一错误结构

所有失败响应都是同一个信封：

```json
{"error": {"type": "invalid_parameter", "message": "...", "details": ["..."]}}
```

| type | HTTP | 含义 |
| --- | --- | --- |
| `invalid_parameter` | 422 | 查询参数、路径参数、分页与时间范围不合法；插件 id 不存在或不是导入插件 |
| `invalid_format` | 400 | 没有插件认得这个文件；插件判定输入格式不对；送进来的数据不符合数据模型 |
| `invalid_format` | 413 | 上传文件超过 `SAS_MAX_UPLOAD_BYTES`（默认 512 MiB）；已写入的部分已被删除 |
| `not_found` | 404 | 机器、启动、采集、任务或接口路径不存在 |
| `internal_error` | 500 | 服务端内部错误，包含插件抛出的未处理异常 |

`details` 是逐条可读的说明（例如 FastAPI 的字段级校验错误），可以为空。

`internal_error` 的 `message` 是固定文案加一个能在服务端日志里检索的请求 id，**不含异常类型与
原文**：异常消息可能带文件路径、SQL 片段或库内部细节，只写日志。插件主动拒绝输入
（`PluginInputError`）与数据校验失败（`StorageValidationError`）的说明本来就是给调用方看的，
原样透出。

插件抛出的异常在这里被转换成上面的分类，插件自己的异常类型不会漏到 HTTP 层：

| 异常 | 结果 |
| --- | --- |
| `PluginInputError` | `invalid_format` |
| `PluginExecutionError`（插件抛出的其它异常） | `internal_error`，原始异常记进日志 |
| `PluginNotRegisteredError` | `invalid_parameter` |
| `StorageValidationError` | `invalid_format`，`details` 是逐条原因 |
| `TimelineBootNotFoundError` | `not_found` |
| `TimelineRangeError`、`TimelineKindError` | `invalid_parameter` |

实际响应（本机复跑得到）：

```text
$ curl -s "$API/api/collections?limit=0"
{"error":{"type":"invalid_parameter","message":"请求参数不合法","details":["query.limit: Input should be greater than or equal to 1"]}}   [HTTP 422]

$ curl -s "$API/api/collections/collection:x/records?start_ns=10&end_ns=5"
{"error":{"type":"invalid_parameter","message":"时间范围起点 10 晚于终点 5，空区间不查询","details":[]}}   [HTTP 422]

$ curl -s -F "file=@notes.txt" "$API/api/imports"
{"error":{"type":"invalid_format","message":"没有导入插件认得这个文件：notes.txt","details":["已注册的导入插件：bootrec.importer、journal.archive"]}}   [HTTP 400]

$ curl -s "$API/api/machines/machine:nope"
{"error":{"type":"not_found","message":"机器 machine:nope 不在库里","details":[]}}   [HTTP 404]

$ curl -s "$API/api/machines/machine:uos-PC/boots/nope/timeline/tracks"
{"error":{"type":"not_found","message":"启动 machine:uos-PC/nope 不在库里，没有可回放的时间轴","details":[]}}   [HTTP 404]
```

插件崩溃的表现：`tests/test_api_imports.py::test_plugin_crash_is_internal_error` 用一个
`detect` 直接抛 `RuntimeError` 的插件走完整个 HTTP 请求，响应是 `500` + `internal_error` 信封，
不是没有响应的裸崩，也不会把任务排进队列；响应里没有 `RuntimeError` 字样与插件的异常原文。

`500` 响应的形状（固定文案 + 请求 id、不含异常原文）由
`tests/test_api_errors.py::test_unexpected_exception_becomes_internal_error_without_details` 断言。

## 4. 分页与时间范围

分页参数（机器启动记录、采集记录、采集内记录）：

| 参数 | 取值 | 说明 |
| --- | --- | --- |
| `limit` | 1..200，默认 50 | 每页条数 |
| `offset` | 0..100000，默认 0 | 跳过的条数 |

`limit` 与 `offset` 任一边界越界（`limit` 为 0 或超过 200、`offset` 为负或超过 100000）都是
`invalid_parameter`。`offset` 超出总条数时返回空 `items`，`total` 仍是筛选后的真实条数。

采集内记录的页码要靠 `LIMIT offset+limit` 模拟（存储层没有 `OFFSET` 参数），所以 `offset` 必须有
上界：上界让最坏情况最多取回 100200 行。越界的页码（`offset` 不小于这次采集的记录总数）连那条
`LIMIT` 查询都不会发出，直接返回空页。

时间范围参数（采集内记录、时间线）：

| 写法 | 含义 |
| --- | --- |
| 都不给 | 全部 |
| 只给 `start_ns` | `[start_ns, +∞)` |
| 只给 `end_ns` | `(-∞, end_ns]` |
| 都给 | 闭区间 `[start_ns, end_ns]`，两端时刻的记录都包含 |
| `start_ns == end_ns` | 单个瞬间 |
| `start_ns > end_ns` | `invalid_parameter`，不静默交换 |
| 范围完全在数据之外 | `items` 为空；时间线的轨道与 `axis_*` 仍然返回 |

`start_ns`、`end_ns` 是纳秒整数，负数算参数错误。

`kinds` 是逗号分隔的记录种类：采集内记录接受 `event`、`span`、`metric`、`profile`、`annotation`、
`artifact`；时间线只接受 `event`、`span`、`metric`（其余种类的展示属于 2.x 与 4.x）。写了不认识的
种类报 `invalid_parameter`，不静默忽略。

采集内记录的 `total` 口径：响应字段叫 `collection_total`，是**这次采集的记录总数**，不随
`kinds` 与时间范围变化。存储层没有「按筛选条件计数」的接口，与其给一个算不准的「筛选后总数」，
不如把口径写清楚。`items` 仍然是当前页。

## 5. 时间线接口

三个接口全部直接调用任务 1.5 的 `TimelineEngine`，本层只做参数校验与序列化：

- `tracks`：一次启动在给定范围里的轨道与条目，附整条轴的范围、启动锚点与漂移、整次启动的资源声明。
- `state`：某个 boot 内时刻每条轨道的状态（最近采样点、正在进行的区间、最近事件）。
- `cursor`：统一时间游标。带 `forward=true|false` 时先把游标放到 `at_ns`（缺省为轴起点），再挪到
  后一个或前一个可停靠时刻。

条目按 `type` 区分形状（`event` / `span` / `metric`），OpenAPI 里是带判别字段的联合类型。
轨道状态原样透出，不在序列化时抹平：

| `status` | 含义 | 响应里有什么 |
| --- | --- | --- |
| `available` | 这条轨道有记录 | `items` 里有数据 |
| `unavailable` | 没有记录，且指标流被标成不可用 | `items` 为空，`unavailable_reason` 写明原因 |
| `no_data` | 没有记录，也没有任何采集声明它不可用 | `items` 为空，`unavailable_reason` 为 `null` |

「整次启动没有资源数据」（例如 Journal-only 导入）不是轨道状态，而是响应里的
`resources.available = false` 与 `resources.reasons`；这种情况根本不会出现资源轨道，也不会把缺失
写成 0。真实 PostgreSQL 上的用例：`tests/integration/test_api_import_timeline.py` 的
`test_timeline_distinguishes_unavailable_from_no_data`，同一条启动里同时存在三种状态。

## 6. 导入任务与采集状态

`GET /api/imports/{job_id}` 返回两块：

- `job`：`jobs` 表里的任务状态（`queued` / `running` / `succeeded` / `failed`）、执行次数、失败分类，
  以及处理器写回的 `result`（写进了哪次采集、写入与跳过多少条、跳过原因）。
- `job.failure`：失败时给出 `kind` 与一句说明。`kind` 只有两种：
  - `invalid_input`：插件判定这份输入不能用，`message` 是插件的拒绝原因（例如「压缩流不完整」）；
  - `internal`：任务执行出错，`message` 是固定文案，具体异常只写进 Worker 日志。
  任务行的 `last_error` 带异常类型与原文，**不对外返回**。
- `collection`：`result.collection_id` 指向的采集，含 `import_status`、`import_error`、
  `resources_available` 与原因。任务还没写出结果时为 `null`。

一次成功导入的实际响应：

```json
{
  "job": {
    "id": "34aa1c1e-c0fd-4833-adfc-fd0941937c52",
    "kind": "import.source",
    "status": "succeeded",
    "attempts": 1,
    "max_attempts": 3,
    "failure": null,
    "plugin_id": "journal.archive",
    "upload_id": "8354032bae1645f7ad92e1c4bb488983",
    "filename": "boot.journal.gz",
    "result": {
      "collection_id": "journal-import:machine:uos-PC:abc7451f-65c8-4be8-ab01-4b9a1e49a8b9",
      "import_status": "imported",
      "records_written": 4,
      "records_skipped": 0,
      "skipped_reason": null
    }
  },
  "collection": {
    "collection_id": "journal-import:machine:uos-PC:abc7451f-65c8-4be8-ab01-4b9a1e49a8b9",
    "import_status": "imported",
    "resources_available": false,
    "resources_reason": "Journal 归档只含日志，没有 CPU、内存、磁盘采样"
  }
}
```

任务结果写在 `jobs.payload.result` 里，状态仍然由 `jobs.status` 决定：结果与成败分开记录，
失败的任务不会带着一个「成功」的结果。状态查询只暴露约定字段，执行方内部用的键不外露。

同一个文件重复上传仍然幂等：第二次的 `records_written` 为 0，`skipped_reason` 说明「同一份文件已经
导入过」，`collection_id` 与第一次相同。

## 7. 上传文件的存放

- 目录由 `SAS_UPLOAD_DIR` 配置，默认 `var/uploads`（相对进程工作目录）。**API 与 Worker 必须指向
  同一个路径**：API 写文件，Worker 读文件。
- 文件名是「随机 id + 安全化后的原名」：同名文件多次上传不互相覆盖，原名里的路径分隔符会被替换，
  不会写出上传目录。
- 写文件与算 `sha256` 在工作线程里做，不占住处理请求的事件循环。
- 单个文件的上限由 `SAS_MAX_UPLOAD_BYTES` 配置，默认 512 MiB。写入过程中累计字节数，一超过上限
  就中止、删掉已写入的部分并返回 `413`：不会先写满磁盘再判断，也不留半份文件。
- 格式不被接受、插件在识别时崩溃或任务入队失败时，本次写入的文件会被删除，被拒绝的上传不留垃圾。
- 导入成功后的原文件**不自动删除**：它是这次导入的原始证据（`source_sha256` 也记在采集上）。
  清理属于部署运维的事，本任务不做自动清理。

## 8. 复跑验证

```bash
scripts/dev-db.sh start
cd backend
cp -n .env.example .env
uv run alembic upgrade head
```

起两个进程（`SAS_UPLOAD_DIR` 两边一致）：

```bash
cd backend
SAS_API_PORT=8010 SAS_UPLOAD_DIR=var/uploads uv run sas-api
SAS_UPLOAD_DIR=var/uploads uv run sas-worker
```

三步走通（用的是仓库里的固定样例 `backend/tests/fixtures/journal_archive/boot.journal.gz`）：

```bash
API=http://127.0.0.1:8010

# 1. 上传
curl -s -F "file=@tests/fixtures/journal_archive/boot.journal.gz;type=application/gzip" "$API/api/imports"
# {"job_id":"34aa1c1e-...","upload_id":"8354032b...","plugin_id":"journal.archive",
#  "filename":"boot.journal.gz","source_sha256":"e4c6a962...","size_bytes":10551}

# 2. 轮询任务状态（Worker 领取后执行）
curl -s "$API/api/imports/34aa1c1e-c0fd-4833-adfc-fd0941937c52"
# job.status = succeeded，collection.import_status = imported，records_written = 4

# 3. 查询时间线
curl -s "$API/api/machines/machine:uos-PC/boots/abc7451f-65c8-4be8-ab01-4b9a1e49a8b9/timeline/tracks"
```

时间线的实际输出（摘要）：轴覆盖 `175830828563000 → 175831281654000`，共 4 条轨道、4 个条目，
`resources.available` 为 `false` 且带原因，`clock.drift_ns` 超过 1 秒因此 `unreliable` 为 `true`。

自动化测试：

```bash
cd backend && uv run ruff check . && uv run ruff format --check . && uv run pytest
uv run pytest -m integration
```

## 9. 已知限制与偏差

1. **上传接口不建采集记录**。任务交接里写的是「上传接口只落文件与建采集记录并把任务写进队列」。
   实现改成：上传只写文件、算摘要、选插件、排任务；采集记录由 Worker 在导入开始时登记
   （存储层的 `import_collection` 先登记再写）。原因：登记采集需要 `machine_id`、`boot_id` 与
   `collection_id`，只能靠解析文件得到；在请求线程里解析一遍、Worker 导入时再解析一遍，对一份上限
   512 MiB 的归档是双倍开销，也与「请求线程里不做导入」冲突。代价是「已上传、未开始导入」这段时间
   里没有采集记录，任务状态接口返回 `collection: null`。要做到交接里的形态，需要导入模块提供
   「只读采集标识」的轻量入口（属于 1.3 / 1.4 的范围），本任务不自行新增。
2. **分页在 API 层切片**。存储层（1.1）的查询接口没有 `limit` / `offset`，采集与启动记录都是先取
   全量再切页。数据量大时应在存储层加分页参数；本任务不改存储层，按交接要求向 1.1 提需求。
   采集内记录的分页把 `limit` 下推给存储层（多取 `offset` 条再切），只有 `collection_total` 走
   `count_records`，口径见第 4 节。
3. **任务状态需要 `jobs` 表的两个只读入口**。`jobs/queue.py` 新增 `get(job_id)`（按 id 读一条）与
   `record_result(job_id, result)`（把导入结果写回 `payload.result`）。交接没有点名要改这个文件，
   但不加这两个方法，任务状态查询要么写 SQL 越过分层，要么只能回报采集状态。两处都只读写
   `jobs` 表，不改变任务状态机。
4. **新增配置项 `SAS_UPLOAD_DIR`**（`core/config.py`）。上传目录必须由两个进程共用，放进
   `Settings` 是唯一不把路径写死在代码里的做法。
5. **新增依赖 `python-multipart`**（`backend/pyproject.toml`）。FastAPI 的 multipart 上传需要它，
   不引入就无法接收文件；这是唯一新增的运行时依赖。
6. **`ruff` 配置增加 `flake8-bugbear.extend-immutable-calls`**。FastAPI 的依赖注入就是把 `Depends`、
   `Query`、`File`、`Form` 写在默认参数上，`B008` 会把它们全部报成缺陷；按 FastAPI 官方建议把它们
   声明为可接受的调用。
7. **上传时只做格式识别，不做完整校验**。`detect` 只读文件头，所以「文件是 gzip 但内容被截断」
   这类问题在上传到完成导入之间才暴露，表现为任务 `failed` 与可读的失败原因（插件把整份拒绝），
   而不是上传时直接 `400`。这符合「导入在 Worker 里跑」的划分。
8. **未改动父 issue 的 23 项拆分与 stage 分组**，也没有改动 `storage/`、`importers/`、`timeline/`、
   `plugins/`、`migrations/`、`domain/`、`tools/`。
