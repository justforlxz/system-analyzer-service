# Core 与 Plugin 接口契约

本文档是任务 0.4（issue MKACG-765）的交付物，定义 Core 与四类插件（Collector、
Importer、Analyzer、Visualization）之间的契约，使它们可以并行开发且互不侵入。

- 任务依据：issue MKACG-765 正文与附件 Task Handoff。
- 上游结论：`docs/implementation-baseline.md` 第 2.4 节「采集、导入、分析任务都跑在 Worker，
  插件注册只在 Worker 侧，`plugins/builtins.py` 是唯一的内置插件清单与注册入口，
  API 与 Worker 启动都调用同一入口」。本文档与这条结论一致，不另设第二套注册入口。
- 代码位置：`backend/src/system_analyzer/plugins/contracts.py`（契约）、`registry.py`（注册表）、
  `builtins.py`（唯一清单与注册入口）。
- 代码与本文档不一致时以 `contracts.py` 为准，并立刻修正本文档；本文档不重复定义任何数据字段，
  数据字段见任务 0.2 的 `docs/data-model.md`。

目录：

1. Core 与插件的职责划分
2. Plugin Manifest
3. 四类接口
4. 注册、发现、加载与错误隔离
5. 插件读写数据的唯一入口
6. 插件不得做的事
7. 契约测试方案
8. 假插件样例
9. 与其它任务的边界
10. 偏差清单

## 1. Core 与插件的职责划分

| 能力 | 谁负责 | 规则 |
| --- | --- | --- |
| 调度（决定哪个插件、什么时候跑） | Core | Worker 从 `jobs` 表取任务、按 capability 选插件、调用入口方法。插件不得调度任务、不得入队、不得起线程/进程/定时器。 |
| 写诊断数据 | Core | 只有 Core 的存储层实现 `PluginDataAccess`。插件只能通过 `context.data` 写，见第 5 节。 |
| 读诊断数据 | Core 提供、插件使用 | 插件可以只读全部统一数据（包括别的插件写入的记录）；同一份数据多方读取是允许的。 |
| 读别的插件的运行状态 | 不允许 | 插件之间不互相调用、不共享全局变量。跨插件关联只能通过统一数据模型里的记录完成（关联规范见任务 0.3）。 |
| 插件自身的状态 | 插件自己持有，且不跨调用 | 插件对象在模块加载时构造一次，必须无跨调用的可变状态；一次任务要用的上下文全部由 `PluginContext` 传入。 |
| 配置解析与校验 | Core | Core 按 manifest 的 `config_schema` 校验配置，再把结果放进 `context.config`；插件不读环境变量、不读配置文件。 |
| manifest 与插件清单 | 插件提供 manifest，Core 拥有清单与注册表 | 清单只有 `plugins/builtins.py` 的 `BUILTIN_PLUGINS`；注册表只有 `plugins/registry.py`。 |
| 失败判定、重试、隔离 | Core | 插件用 `PluginError` 家族表达失败，Core 决定记录、重试还是终止。规则见第 3.6 与第 4 节。 |
| 事务、建表、迁移 | Core | 插件不得建表、改表、开事务、写 SQL。 |
| 网络访问 | 只有采集插件 | 采集插件只能在 `CollectRequest.target_id` 指定的机器上访问网络；导入、分析、可视化插件不访问网络。 |
| 数据缺失的表示 | 插件按 0.2 的规则写 | 缺失的资源指标必须写成 0.2 定义的「不可用」，不得写成 0、空串或默认值。 |

一句话概括权限：**Core 拥有调度、存储、事务与清单；插件拥有解析与计算，只能通过
`context.data` 追加记录。**

## 2. Plugin Manifest

manifest 是注册与发现的唯一依据：Core 只看 `PluginSpec`，不看插件实现的内部结构。

| 字段 | 类型 | 必填 | 规则 |
| --- | --- | --- | --- |
| `id` | `str` | 是 | 稳定标识，形如 `systemd.lifecycle`（小写、点分、全局唯一）。不允许随版本改名，也不允许用显示名称充当标识。空串或纯空白视为缺失。 |
| `kind` | `PluginKind` | 是 | `collector` / `importer` / `analyzer` / `visualization` 之一（`PluginKind` 的成员），决定 Core 用哪一套接口调用。写成普通字符串（例如 `"analyzer"`）或拼错时 `validate()` 抛 `PluginManifestError`，因为 `by_kind()` 用 `is` 比较枚举成员，非成员值会让插件静默失效。 |
| `name` | `str` | 是 | 给人看的名称，可以随版本改进文案，不得用作标识。 |
| `version` | `str` | 是 | 插件自身版本，建议语义化写法（`1.2.0`）。 |
| `api_version` | `str` | 是 | 插件按哪一版插件 API 编写；必须与 `PLUGIN_API_VERSION` **完全相同**。 |
| `capabilities` | `tuple[str, ...]` | 是 | 插件能处理的对象，至少一项，按「领域.对象」书写（例如 `journal.archive`）。必须是 `tuple`（写成单个字符串会被逐字符当成 capability，`validate()` 拒绝），元素不能是空串。 |
| `config_schema` | `Mapping[str, Any]` | 否，默认 `{}` | 插件配置的 JSON Schema，顶层必须是对象（`type` 缺省或等于 `"object"`）。`{}` 表示不接受任何配置。 |

### 2.1 插件 API 版本规则（写死）

- 当前 Core 支持的插件 API 版本是 `PLUGIN_API_VERSION = "1.0"`。
- 比较方式是**字符串全等**：`1.0` 与 `1.0.0` 是两个不同的版本，后者会被拒绝。不做范围协商，
  不做「主版本相同就兼容」的放宽，也没有兼容层。
- 校验发生在注册时，不是调用时：写错版本的插件进不了注册表，不可能跑到任务里才发现。
- 版本不符时抛 `ApiVersionMismatchError`，注册表保持原样（不会写入半个条目）。错误信息同时给出
  插件 id、插件要求的版本、Core 支持的版本，并给出下一步建议，样例：

  ```text
  插件 sample.legacy-analyzer 要求插件 API 0.9，Core 支持 1.0；版本不匹配，拒绝加载。请以插件 API 1.0 重写该插件或升级 Core。
  ```

- 内置插件出现版本不符时，服务启动直接失败（fail fast）。`BUILTIN_PLUGINS` 是仓库代码，
  出错必须立刻暴露，不存在「跳过这个插件继续启动」的路径。
- manifest 其它不合格项同样抛 `PluginManifestError`，错误信息指出是哪个字段与不合格的原因：
  必填项为空或不是字符串、`kind` 不是 `PluginKind` 成员、`capabilities` 不是 `tuple` 或为空、
  `config_schema` 不是对象。

### 2.2 capabilities 的用法

- Core 用 `capabilities` 把任务分给插件，例如 `journal.archive` 交给 Journal 导入插件。
- 名字是自由字符串，但必须按「领域.对象」书写；同一个 capability 允许被多个插件声明（Core 按任务与配置选一个）。
- Core 不认识某个 capability 时忽略它，不拒绝插件：老 Core 不能因为新插件多声明一项就加载失败。

## 3. 四类接口

四类接口共用同一套公共部分（3.1），区别只在入口方法与输入输出（3.2 - 3.5），
错误约定统一在 3.6。

### 3.1 公共部分

#### PluginContext

Core 每次调用插件时传入的**全部外部能力**：

```python
@dataclass(frozen=True, slots=True)
class PluginContext:
    plugin_id: str
    data: PluginDataAccess          # 唯一的数据读写入口，见第 5 节
    config: Mapping[str, Any] = {}  # 已按 manifest 的 config_schema 校验过
    run_id: str = ""                # 本次任务 id，用于把写入的数据关联到任务记录
```

插件只能从这里拿配置与数据入口；不得自己读环境变量、配置文件、数据库连接串。

#### PluginBase 与生命周期回调

```python
class PluginBase:
    spec: ClassVar[PluginSpec]

    def on_load(self, context: PluginContext) -> None: ...
    def on_unload(self, context: PluginContext) -> None: ...
```

| 回调 | 何时调用 | 约定的行为 |
| --- | --- | --- |
| `on_load` | Core 第一次调用该插件之前，进程内调用一次 | 做一次性准备（例如读取远端能力）；抛异常表示加载失败，Core 记录原因并且不调用入口方法。 |
| `on_unload` | Core 停止使用该插件（进程退出或清单变化） | 释放外部资源；不得在此写业务数据。 |

两个回调都是**可选**的：Core 用 `getattr(plugin, "on_load", None)` 判断，插件可以不继承
`PluginBase`。继承基类只是为了省掉两个空方法。

四类接口都继承同一个 ``Plugin`` 协议，它只要求 `spec` 一个属性：

```python
@runtime_checkable
class Plugin(Protocol):
    spec: PluginSpec
```

`PluginRegistry` 只认这个结构契约（`register(plugin: Plugin)`），所以不继承 `PluginBase` 的插件
同样能注册、能被调用。

#### 入口方法的调用方式

Core 调用插件入口方法只有一种方式：

```python
def invoke_plugin[T](plugin_id: str, call: Callable[[], T]) -> T
```

`call` 里是插件的入口方法。规则见 3.6。

### 3.2 Collector（远程采集）

```python
@dataclass(frozen=True, slots=True)
class CollectRequest:
    target_id: str
    run_id: str
    options: Mapping[str, Any] = {}

@dataclass(frozen=True, slots=True)
class CollectResult:
    records_written: int
    artifacts: tuple[str, ...] = ()

class CollectorPlugin(Protocol):
    spec: PluginSpec
    def collect(self, request: CollectRequest, context: PluginContext) -> CollectResult: ...
```

输入输出示例（来自 `backend/tests/fake_plugins.py` 的 `FakeCollector`）：

```python
request = CollectRequest(target_id="uos-152", run_id="run-1")
result = plugin.collect(request, context)
# CollectResult(records_written=1, artifacts=())
```

| 约束 | 内容 |
| --- | --- |
| 访问范围 | 只能访问 `request.target_id` 指定的机器；不得顺带访问别的目标。 |
| 数据写入 | 采集到的记录通过 `context.data.append_records(..., plugin_id=spec.id, source=run_id)` 写入。 |
| 产物 | 原始文件（例如 `.bootrec`）由任务 3.7 的回传通道上传，`CollectResult.artifacts` 只放 Core 能识别的引用。 |
| 幂等 | 同一次 `run_id` 被重试时，不得产生重复记录。 |
| 生命周期 | 采集过程必须能在没有桌面会话的机器上运行；创建的任何临时配置必须在返回前恢复。 |

### 3.3 Importer（外部数据导入）

```python
@dataclass(frozen=True, slots=True)
class ImportSource:
    path: Path
    media_type: str | None = None
    size_bytes: int | None = None

@dataclass(frozen=True, slots=True)
class ImportResult:
    records_written: int
    artifacts: tuple[str, ...] = ()

class ImporterPlugin(Protocol):
    spec: PluginSpec
    def detect(self, source: ImportSource) -> bool: ...
    def import_source(self, source: ImportSource, context: PluginContext) -> ImportResult: ...
```

输入输出示例（来自 `FakeImporter`）：

```python
source = ImportSource(path=Path("/data/boot-1.journal.gz"), media_type="application/gzip")
if plugin.detect(source):
    result = plugin.import_source(source, context)
    # ImportResult(records_written=1)
```

| 约束 | 内容 |
| --- | --- |
| `detect` | 只读文件元信息（扩展名、大小、魔数），必须快；不解析全部内容、不写数据、不访问网络。 |
| `import_source` | 解析来源并把**标准化后**的记录写进 `context.data`；不认识的来源抛 `PluginInputError`。 |
| 幂等 | 同一个来源重复导入必须幂等（父 issue「数据导入必须幂等」）。 |
| 缺失数据 | 压缩包里没有的资源指标必须写成 0.2 定义的「不可用」，不得补 0 或估算。 |
| 网络 | 不访问 SSH 与网络；只处理本地文件。 |

### 3.4 Analyzer（分析）

```python
@dataclass(frozen=True, slots=True)
class AnalysisRequest:
    boot_id: str = ""                 # 空串表示分析 Core 选定的全部记录
    record_ids: tuple[str, ...] = ()
    options: Mapping[str, Any] = {}

@dataclass(frozen=True, slots=True)
class AnalysisResult:
    annotations_written: int

class AnalyzerPlugin(Protocol):
    spec: PluginSpec
    def analyze(self, request: AnalysisRequest, context: PluginContext) -> AnalysisResult: ...
```

输入输出示例（来自 `FakeAnalyzer`）：

```python
result = plugin.analyze(AnalysisRequest(boot_id="boot-1"), context)
# AnalysisResult(annotations_written=1)
```

| 约束 | 内容 |
| --- | --- |
| 只读原始数据 | 通过 `context.data.query_records(...)` 读；分析结论以新记录形式追加。 |
| 不改原始数据 | 数据入口没有 update / delete，插件无法修改或删除已有记录。 |
| 可重复 | 同样的输入必须得到同样结论；同一批输入重复分析不得叠加出互相矛盾的结论。 |
| 可追溯 | 每条结论必须能回溯到它依据的原始记录（关联方式按任务 0.3 的规范）。 |
| 网络 | 不访问网络。 |

### 3.5 Visualization（可视化）

```python
@dataclass(frozen=True, slots=True)
class VisualizationDescriptor:
    component: str                      # 前端组件标识，例如 "timeline.replay"
    required_inputs: tuple[str, ...]    # 需要的时间线输入种类，名字取自 0.2 的数据模型
    options_schema: Mapping[str, Any] = {}

class VisualizationPlugin(Protocol):
    spec: PluginSpec
    def describe(self) -> VisualizationDescriptor: ...
```

输入输出示例（来自 `FakeVisualization`）：

```python
descriptor = plugin.describe()
# VisualizationDescriptor(component="timeline.sample", required_inputs=("timeline",))
```

| 约束 | 内容 |
| --- | --- |
| 不执行 | 可视化插件没有任务入口，Core 不会在任务里调用它；渲染由前端（任务 4.x）完成。 |
| 纯声明 | `describe()` 必须是纯函数：不读写数据、不访问网络、不读环境变量。 |
| 数据需求 | `required_inputs` 里的名字必须来自 0.2 的数据模型，不得自造新名字。 |

### 3.6 错误约定

| 错误类型 | 谁抛 | 含义 | Core 的处理 |
| --- | --- | --- | --- |
| `PluginManifestError` | Core | manifest 不合格（必填为空、`capabilities` 为空、`config_schema` 不是对象） | 拒绝注册；服务启动失败并记录字段名。 |
| `ApiVersionMismatchError` | Core | 插件 API 版本不一致 | 拒绝注册；服务启动失败并记录双方版本。 |
| `DuplicatePluginError` | Core | 同一个 `id` 注册两次 | 拒绝注册；启动失败。 |
| `PluginInputError` | 插件 | 输入不可用（数据损坏、格式不支持） | 任务失败，**不重试**，记录原因。 |
| `PluginUnavailableError` | 插件 | 依赖暂时不可用（SSH 连不上、目标忙） | 任务失败，按 `jobs.max_attempts` 重试。 |
| `PluginExecutionError` | Core（包装） | 插件抛出了契约外的异常 | 任务失败，**不重试**，记录异常类型与原始 traceback（原始异常在 `__cause__`）。 |

规则：

1. 插件只能抛 `PluginError` 家族表达可预期失败；其它异常一律视为缺陷。
2. Core 只通过 `invoke_plugin` 调用插件入口方法。该方法把契约外的异常包成
   `PluginExecutionError`，因此插件自己的异常类型不会进入 Core 的其它代码。
3. 插件不得用「返回空结果」表示失败：空结果表示「确实没有数据」，失败必须抛错。
   否则 Core 无法区分「没有异常」和「采不到数据」。

## 4. 注册、发现、加载与错误隔离

### 4.1 发现

- 唯一清单：`backend/src/system_analyzer/plugins/builtins.py` 的 `BUILTIN_PLUGINS`，
  元素是插件对象（无状态，模块加载时构造）。
- 其它模块不得定义同名清单，测试 `test_builtin_plugin_list_lives_in_one_module_only` 会扫描整个包。
- 本版本不扫描文件系统、不加载第三方包、不发网络请求去发现插件。

### 4.2 注册

- API 启动（`api/app.py` 的 `create_app`）与 Worker 启动（`worker/main.py` 的 `main`）都调用
  `register_builtins()`，它把清单里的插件注册进进程内注册表。
- `register_builtins()` 可重复调用：已注册的 id 跳过，所以两个进程各启动一次也只有一份注册。
- `PluginRegistry.register(plugin)` 的校验顺序写死：
  1. 插件对象必须有 `spec`（`PluginBase.spec` 或等价属性）；
  2. `spec.validate()`：必填项非空 → `kind` 值域 → 插件 API 版本 → `capabilities`（必须是
     `tuple` 且非空）→ `config_schema`；
  3. `id` 未被注册过。
- 任何一步不通过都抛错，**不做「跳过继续」**：内置清单是仓库代码，出错必须立刻暴露。

### 4.3 加载

- 注册只做校验与登记，不调用插件入口方法。
- 加载发生在第一次真正使用该插件之前：Core 在 `invoke_plugin` 保护下调用 `on_load`（若存在）。
- `on_load` 抛异常表示加载失败，Core 记录原因并让本次任务失败，不调用入口方法。

### 4.4 错误隔离

「插件抛异常不得拖垮 Core」的具体含义与已验证的行为：

1. **边界收敛**：插件抛出的非 `PluginError` 异常在 `invoke_plugin` 处被包成
   `PluginExecutionError`。Core 的调用方只需要处理这一种错误，不需要 import 任何插件的异常。
2. **不影响其它插件**：一个插件在任务里崩溃，注册表与其它插件不受影响，下一个任务照常选到别的插件。
   测试 `test_plugin_crash_is_wrapped_and_registry_survives` 用一个「必定崩溃」的假插件与一个健康插件
   同处一个注册表来证明：崩溃被包成 `PluginExecutionError`，注册表仍有两条，健康插件随后仍能正常返回结果。
3. **不影响进程**：隔离靠「异常被包住」这一条边界，不靠进程隔离；本版本不引入插件子进程或沙箱
   （父 issue：首版用 Backend 进程内插件）。
4. **工程化留给任务 1.2**：任务超时、并发上限、失败计数、插件状态上报属于 1.2 的注册机制与
   生命周期管理。本契约只固定「调用必须经过 `invoke_plugin`、插件异常不得直传」这条边界。

## 5. 插件读写数据的唯一入口

```python
class PluginDataAccess(Protocol):
    def append_records(
        self,
        records: Sequence[UnifiedRecord],
        *,
        plugin_id: str,
        source: str,
    ) -> int: ...

    def query_records(
        self,
        *,
        record_kinds: Sequence[str] = (),
        limit: int | None = None,
    ) -> Sequence[UnifiedRecord]: ...
```

- 由 Core 的存储层（任务 1.1）实现，通过 `PluginContext.data` 交给插件。
- 插件拿到的是这个协议的实例，不是数据库连接、会话或仓储对象；因此插件无法改表结构、开事务、
  绕过校验，也拿不到连接串。
- 写入必须带 `plugin_id` 与 `source`，这是「数据来源、时间、实体关联和分析结果必须可追溯」
  的落点：任何一条记录都能回答「谁写的、来自哪次采集或导入」。
- 只有追加与只读查询，没有 update / delete：原始数据不会被插件改写。
- 记录的具体类型由任务 0.2 定稿。本契约为避免在 0.2 之前锁死名字，用
  `type UnifiedRecord = Any` 占位；0.2 定稿后由任务 1.1 把这一行换成真实类型，契约其余部分不变。
  插件因此可以开始实现（只依赖 `context.data` 的方法签名），不需要等数据模型落地。
- 反向约束由测试保证：插件实现目录与假插件里出现
  `sqlalchemy` / `asyncpg` / `alembic` / `system_analyzer.storage` / `system_analyzer.core` 的 import
  时，`test_plugin_implementations_never_import_storage` 直接失败。

## 6. 插件不得做的事

Reviewer 可以直接按这张表逐条对照检查。

| # | 不得做的事 | 检查方式 |
| --- | --- | --- |
| 1 | 直接访问数据库：不 import 数据库驱动、迁移工具、`system_analyzer.storage`、`system_analyzer.core`；不建引擎、不建会话、不写 SQL。 | 自动（`test_plugin_implementations_never_import_storage`）+ 评审 |
| 2 | 自己读全局配置：不读环境变量、不读配置文件、不调用 `get_settings()`；配置只从 `context.config` 取。 | 自动（同上，`system_analyzer.core` 在禁止清单）+ 评审 |
| 3 | 调度任务：不入队、不起线程/进程/定时器、不监听端口、不自己重试。 | 评审 |
| 4 | 读写别的插件的进程内状态：不互相调用、不用全局变量或模块级可变状态在插件之间传数据。 | 评审 |
| 5 | 修改或删除已有记录：只能追加新记录（数据入口本身没有 update / delete）。 | 自动（协议只有 append / query）+ 评审 |
| 6 | 把缺失数据写成 0、空串或默认值：必须用 0.2 定义的「不可用」表示。 | 评审 + 导入/分析用例 |
| 7 | 用会随版本变化的 `id`，或用显示名称充当标识。 | 评审 |
| 8 | 吞掉异常后返回「成功」或空结果；失败必须抛 `PluginError` 家族。 | 评审 + 契约测试的假插件示范 |
| 9 | 在 import 时执行副作用（连数据库、开网络、读文件）：注册阶段只允许构造对象。 | 评审 |
| 10 | 处理未在 `capabilities` 里声明的输入。 | 评审 |
| 11 | 采集插件访问 `request.target_id` 之外的机器；其它三类插件访问网络。 | 评审 |
| 12 | 另建插件清单或第二套注册入口。 | 自动（`test_builtin_plugin_list_lives_in_one_module_only`）+ 评审 |

## 7. 契约测试方案

契约测试是可执行的用例集合，不是描述。位置：

- `backend/tests/test_plugin_contracts.py`：契约本身（manifest 规则、四类接口结构、生命周期、异常隔离、禁止事项）。
- `backend/tests/test_plugin_registry.py`：注册表门禁（注册并列出、id 重复、版本拒绝、唯一入口）。
- `backend/tests/fake_plugins.py`：被这些用例驱动的假插件。

运行方式与验收对应关系：

```bash
cd backend
uv run pytest tests/test_plugin_contracts.py tests/test_plugin_registry.py -v
```

| 验收点 | 用例 | 断言的事实 |
| --- | --- | --- |
| 每类接口都有签名说明与可运行的假插件 | `test_fake_plugin_matches_its_kind_protocol`（4 个参数）、`test_fake_plugins_cover_all_four_kinds` | 四个假插件的 `kind` 与接口一一对应，且结构上满足对应 Protocol（`isinstance`）。 |
| 四类接口能被真正调用 | `test_collector_writes_through_the_single_data_entry`、`test_importer_detects_then_imports`、`test_analyzer_reads_then_writes_annotations`、`test_visualization_plugin_only_declares` | 每个入口方法能返回约定类型的结果，且写入走的是 `context.data`。 |
| 生命周期回调 | `test_lifecycle_callbacks_default_to_noop`、`test_plugin_may_skip_the_base_class` | 回调默认空实现；不继承基类的插件也能注册（回调可选）。 |
| 假插件能被注册并列出 | `test_fake_plugins_can_be_registered_and_listed`、`test_by_kind_filters` | 四个假插件注册后按注册顺序列出，可按类别过滤。 |
| API 版本不匹配被拒绝且指出版本差异 | `test_api_version_mismatch_is_rejected_at_registration`、`test_api_version_mismatch_message_shows_both_versions`、`test_api_version_must_match_exactly` | 注册抛 `ApiVersionMismatchError`，注册表仍为空，消息里同时出现 `0.9` 与 `1.0`；`1.0.0` 也被判为不同版本。 |
| manifest 规则 | `test_empty_required_field_is_rejected`（4 个参数）、`test_non_string_manifest_value_is_rejected`（3 个参数）、`test_kind_outside_plugin_kind_is_rejected`（3 个参数）、`test_capabilities_must_be_a_tuple`、`test_empty_capabilities_are_rejected`、`test_blank_capability_is_rejected`、`test_config_schema_must_be_object_schema`（3 个参数）、`test_plugin_with_string_kind_cannot_be_registered` | 必填为空、字段类型不对、`kind` 不是 `PluginKind` 成员（注册表层也拒绝，插件进不了注册表）、`capabilities` 不是 `tuple`/为空/含空串、配置 schema 非对象都被拒，错误信息指出字段与原因。 |
| 插件抛异常不影响已注册插件 | `test_plugin_crash_is_wrapped_and_registry_survives` | 崩溃被包成 `PluginExecutionError`（`plugin_id`、`__cause__` 齐备），注册表仍有两条，健康插件随后正常返回。 |
| 契约错误原样上抛 | `test_contract_error_is_not_rewrapped` | 插件抛 `PluginInputError` 时 Core 收到同一个错误，不被二次包装。 |
| 插件不得直接操作数据库 | `test_plugin_implementations_never_import_storage` | 扫描 `collectors/`、`importers/`、`analyzers/` 与假插件源码，禁止清单里的 import 一个都不出现。 |
| 插件清单唯一 | `test_builtin_plugin_list_lives_in_one_module_only`、`test_builtin_list_scanner_catches_every_definition_style`（4 个参数）、`test_builtin_list_scanner_ignores_other_names` | 除 `plugins/builtins.py` 外，包内没有第二个 `BUILTIN_PLUGINS` 定义；扫描器本身对 `BUILTIN_PLUGINS = ()`、带类型标注的 `BUILTIN_PLUGINS: tuple[...] = ()`、类体与函数体里的定义都能认出来。 |

新增插件时必须做的三件事（写进开发流程）：

1. 在对应插件目录实现插件，`spec` 里声明 `api_version = PLUGIN_API_VERSION`；
2. 在 `BUILTIN_PLUGINS` 加一行的插件实例；
3. 跑上面的契约测试与全量门禁：

```bash
cd backend && uv run ruff check . && uv run ruff format --check . && uv run pytest
```

## 8. 假插件样例

位置：`backend/tests/fake_plugins.py`。它们不参与生产注册，也不在 `BUILTIN_PLUGINS` 里。

| 类 | 类别 | 用途 |
| --- | --- | --- |
| `FakeCollector` | collector | 演示采集插件形状：一次调用写一条记录，返回 `CollectResult`。 |
| `FakeImporter` | importer | 演示 `detect` + `import_source`，含一份 `config_schema`。 |
| `FakeAnalyzer` | analyzer | 演示先读记录再写分析结论。 |
| `FakeVisualization` | visualization | 演示只做声明、不读写数据。 |
| `LegacyApiVersionAnalyzer` | analyzer | 声明了 `api_version = "0.9"`，证明版本不匹配会被拒绝。 |
| `ExplodingAnalyzer` | analyzer | 入口方法直接抛 `RuntimeError`，证明崩溃被隔离且不影响已注册插件。 |
| `FakeDataAccess` | — | `PluginDataAccess` 的假实现，记录「谁写了什么」，供断言使用。 |

生成上下文的辅助函数：`fake_context(plugin)` 返回 `(PluginContext, FakeDataAccess)`；
`sample_source(name)` 返回一个假的 `ImportSource`。

## 9. 与其它任务的边界

| 任务 | 关系 |
| --- | --- |
| 0.1 实施基线 | 本契约完全遵循基线第 2.4 节（插件只在 Worker 侧注册、`builtins.py` 唯一入口）；未修改 `docs/implementation-baseline.md`。 |
| 0.2 统一数据模型与 bootrec v1 | 数据字段全部由 0.2 定稿。本契约用 `type UnifiedRecord = Any` 占位，0.2 落地后由 1.1 换成真实类型。本契约未改动 `domain/`、`docs/data-model.md`、`docs/bootrec-v1.md`。 |
| 0.3 时间基准与关联规范 | 分析结论与原始记录的关联名字（`required_inputs`、`record_kinds`）取自 0.2 / 0.3 的命名，本契约不自造。 |
| 1.1 存储与领域模型 | 实现 `PluginDataAccess`。 |
| 1.2 进程内 Plugin Registry | 按本契约实现完整注册机制、生命周期管理与隔离工程化（超时、并发、状态上报）。 |
| 1.3 / 1.4 导入插件 | 按 3.3 实现；`capabilities` 至少一项。 |
| 2.1 / 2.2 分析插件 | 按 3.4 实现。 |
| 3.x 采集插件 | 按 3.2 实现。 |
| 4.x 可视化 | 按 3.5 的声明渲染；前端组件由 4.x 实现。 |

## 10. 偏差清单

1. `plugins/registry.py` 的必要改动（handoff 要求单独说明）：为了让「假插件能被注册并列出」与
   「API 版本不匹配被拒」可验证，注册表存的对象从 `PluginSpec` 换成插件对象（`Plugin` 结构契约），
   `register` 增加 `spec.validate()` 门禁；`get` / `spec` / `all` / `by_kind` 的返回类型随之调整，
   并新增一处 `plugin_spec()` 取 manifest 的小函数（`register` 与 `register_builtins` 共用，
   保证没有 manifest 时抛的是可读的 `PluginManifestError`）。完整注册机制、生命周期管理与
   隔离工程化未实现，留给任务 1.2。
2. `PluginSpec` 从 2 个字段扩到 7 个字段，`tests/test_plugin_registry.py` 中直接构造 `PluginSpec`
   的既有用例改为使用假插件对象；用例断言的行为（id 唯一、API 与 Worker 共用一份注册）未变。
3. 契约里用 `type UnifiedRecord = Any` 占位，等 0.2 的数据模型定稿后由任务 1.1 替换为真实类型。
4. 未改动父 issue 的 23 项拆分与 stage 分组。
