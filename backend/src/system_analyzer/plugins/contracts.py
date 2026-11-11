"""Core 与 Plugin 的接口契约。

本模块是任务 0.4 的交付物，也是后续全部插件实现（1.2 注册机制、1.3 / 1.4 导入、
2.1 / 2.2 分析、3.x 采集、4.x 可视化）唯一的契约来源；设计说明见
``docs/plugin-contracts.md``。

三条边界，全部落在类型上：

1. 插件只通过 ``PluginContext.data`` 这一个入口读写诊断数据。插件模块不得导入
   ``sqlalchemy``、``asyncpg``、``system_analyzer.storage``、``system_analyzer.core``，
   测试 ``tests/test_plugin_contracts.py`` 会扫源码对照检查。
2. Core 通过 ``invoke_plugin`` 调用插件的每一个入口方法。插件抛出的其它异常都在那里
   被包成 ``PluginExecutionError``，不会以插件自己的异常类型进入 Core。
3. 插件清单与注册入口只有 ``system_analyzer.plugins.builtins`` 一处。

数据字段不在本契约里定义：统一数据模型的字段由任务 0.2 定稿（``docs/data-model.md``），
本契约只约定「插件搬运的是一条统一数据模型记录」，不对字段做任何声明。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Protocol, runtime_checkable

PLUGIN_API_VERSION = "1.0"
"""Core 支持的插件 API 版本。

插件的 manifest 必须声明与它完全相同的 ``api_version``。比较规则是字符串全等：
不做范围协商，也不做「主版本相同就兼容」的放宽。版本不符时注册直接失败，
原因见 ``ApiVersionMismatchError``。
"""


class PluginKind(StrEnum):
    """插件类别，与项目描述的四类插件一致。"""

    COLLECTOR = "collector"
    IMPORTER = "importer"
    ANALYZER = "analyzer"
    VISUALIZATION = "visualization"


class PluginError(Exception):
    """插件体系里所有可预期错误的基类。

    插件可以用它表达「这次调用失败」而不被 Core 当成缺陷；Core 收到 ``PluginError``
    后按类型决定是否重试，具体见 ``docs/plugin-contracts.md`` 的「错误约定」。
    """


class PluginManifestError(PluginError):
    """插件的 manifest 不合格（缺字段、字段为空、config schema 不是对象）。"""


class ApiVersionMismatchError(PluginError):
    """插件声明的 API 版本与 Core 支持的不一致，拒绝加载。

    错误信息里同时给出插件的版本与 Core 的版本，读日志的人不需要再翻代码。
    """

    def __init__(self, plugin_id: str, required: str, supported: str) -> None:
        super().__init__(
            f"插件 {plugin_id} 要求插件 API {required}，Core 支持 {supported}；"
            f"版本不匹配，拒绝加载。请以插件 API {supported} 重写该插件或升级 Core。"
        )
        self.plugin_id = plugin_id
        self.required = required
        self.supported = supported


class PluginInputError(PluginError):
    """插件收到的输入不符合预期（数据损坏、格式不支持）。Core 记为失败，不重试。"""


class PluginUnavailableError(PluginError):
    """插件依赖暂时不可用（SSH 连不上、目标忙）。Core 可以按任务的重试策略重试。"""


class PluginExecutionError(PluginError):
    """插件抛出的未处理异常，由 Core 在 ``invoke_plugin`` 里包装成这个错误。"""

    def __init__(self, plugin_id: str, cause: BaseException) -> None:
        super().__init__(
            f"插件 {plugin_id} 抛出未处理异常，Core 已隔离：{type(cause).__name__}: {cause}"
        )
        self.plugin_id = plugin_id
        self.cause = cause


@dataclass(frozen=True, slots=True)
class PluginSpec:
    """插件 manifest：注册、发现与校验只看这个对象，不看插件实现。

    每个字段的类型与是否必填见 ``docs/plugin-contracts.md`` 的「Plugin Manifest」一节；
    ``validate`` 是这些规则的唯一实现，注册与测试都调它。
    """

    id: str
    """稳定标识，形如 ``systemd.lifecycle``：小写、点分、全局唯一，不允许随版本改名。"""

    kind: PluginKind
    """插件类别，决定 Core 用哪一套接口调用它。"""

    name: str
    """给人看的名称，可随版本改进文案，不允许作为标识使用。"""

    version: str
    """插件自身的版本，建议语义化写法（``1.2.0``）。"""

    api_version: str
    """插件按哪一版插件 API 编写；必须与 ``PLUGIN_API_VERSION`` 完全相同。"""

    capabilities: tuple[str, ...]
    """插件能处理的对象，至少一项，例如 ``("journal.archive",)``。

    名字是自由字符串但按「领域.对象」书写；Core 用它把任务分给对应插件，
    不认识的 capability 一律忽略（老 Core 不因新插件多声明一项而拒绝它）。
    """

    config_schema: Mapping[str, Any] = field(default_factory=dict)
    """插件配置的 JSON Schema（对象形态）。``{}`` 表示不接受任何配置。"""

    def validate(self) -> None:
        """检查 manifest 能否被注册；不合格时抛 ``PluginManifestError`` 或版本错误。

        顺序写死，测试按这个顺序断言：必填项非空 → kind 值域 → API 版本 → capabilities →
        config schema。
        """
        empty = [
            name
            for name in ("id", "name", "version", "api_version")
            if not isinstance(value := getattr(self, name), str) or not value.strip()
        ]
        if empty:
            raise PluginManifestError(
                f"插件 manifest（id={self.id!r}）的必填项为空：{', '.join(empty)}"
            )

        if not isinstance(self.kind, PluginKind):
            raise PluginManifestError(
                f"插件 {self.id} 的 kind 不是 PluginKind 成员，实际是 {self.kind!r}；"
                f"只能是 {', '.join(kind.value for kind in PluginKind)} 之一"
            )
        if self.api_version != PLUGIN_API_VERSION:
            raise ApiVersionMismatchError(
                plugin_id=self.id,
                required=self.api_version,
                supported=PLUGIN_API_VERSION,
            )

        if not isinstance(self.capabilities, tuple):
            raise PluginManifestError(
                f"插件 {self.id} 的 capabilities 必须是 tuple[str, ...]，"
                f"实际是 {type(self.capabilities).__name__}"
            )
        if not self.capabilities:
            raise PluginManifestError(
                f"插件 {self.id} 没有声明 capabilities：至少写一项，Core 靠它匹配任务"
            )
        if any(
            not isinstance(capability, str) or not capability.strip()
            for capability in self.capabilities
        ):
            raise PluginManifestError(f"插件 {self.id} 的 capabilities 里有空字符串")

        if not isinstance(self.config_schema, Mapping):
            raise PluginManifestError(f"插件 {self.id} 的 config_schema 必须是 JSON Schema 对象")
        schema_type = self.config_schema.get("type", "object")
        if schema_type != "object":
            raise PluginManifestError(
                f"插件 {self.id} 的 config_schema 顶层 type 必须是 object，实际是 {schema_type!r}"
            )


#: 统一数据模型里的一条记录。类型与字段由任务 0.2 定稿（``docs/data-model.md``），
#: 本契约只约定「插件搬运的就是这种记录」，不重复声明任何字段，因此这里用 ``Any`` 占位。
type UnifiedRecord = Any


@runtime_checkable
class PluginDataAccess(Protocol):
    """插件读写诊断数据的唯一入口，由 Core 的存储层实现（任务 1.1）。

    插件拿到的是这个协议的实例，而不是数据库连接、会话或仓储对象；
    插件也就不可能越权改表结构、开事务或绕过 Core 的校验。
    """

    def append_records(
        self,
        records: Sequence[UnifiedRecord],
        *,
        plugin_id: str,
        source: str,
    ) -> int:
        """写入一批记录，返回实际写入条数。

        ``plugin_id`` 与 ``source`` 由调用方传入，落到记录上用于追溯「这条数据是谁写的、
        来自哪次采集或导入」。
        """

    def query_records(
        self,
        *,
        record_kinds: Sequence[str] = (),
        limit: int | None = None,
    ) -> Sequence[UnifiedRecord]:
        """只读查询记录。插件不得改写别的插件写入的记录。"""


@dataclass(frozen=True, slots=True)
class PluginContext:
    """Core 调插件时交给它的全部外部能力。

    插件只能通过它拿配置与数据入口，不得自己读环境变量、开数据库连接或创建网络客户端。
    """

    plugin_id: str
    data: PluginDataAccess
    config: Mapping[str, Any] = field(default_factory=dict)
    run_id: str = ""
    """本次任务 id，用于把插件写的数据关联到一次任务记录；空串表示不关联任何任务。"""


class PluginBase:
    """插件实现的基类：只放 manifest 的位置与两个空的生命周期回调。

    生命周期回调可选，Core 用 ``getattr(plugin, "on_load", None)`` 判断，
    所以插件可以不继承本类；继承只是省掉两个空方法。
    """

    spec: ClassVar[PluginSpec]
    """子类必须给出的 manifest；缺失时注册抛 ``PluginManifestError``。"""

    def on_load(self, context: PluginContext) -> None:
        """Core 第一次调用该插件之前执行一次；抛异常表示加载失败。默认什么都不做。"""

    def on_unload(self, context: PluginContext) -> None:
        """Core 停止使用该插件时执行一次；默认什么都不做。"""


@runtime_checkable
class Plugin(Protocol):
    """任何带 manifest 的插件对象。

    插件可以不继承 ``PluginBase``，只要满足这个结构契约（有 ``spec``）并实现对应类别的
    入口方法即可；四类接口都是它的细化。
    """

    spec: PluginSpec


@dataclass(frozen=True, slots=True)
class CollectRequest:
    """一次远程采集请求，由 Core 的任务调度器下发。"""

    target_id: str
    run_id: str
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CollectResult:
    """采集结果。记录通过 ``PluginDataAccess`` 写入，这里只报数量与产物引用。"""

    records_written: int
    artifacts: tuple[str, ...] = ()
    """已回传的原始文件引用（例如 ``.bootrec``），路径或对象键由任务 3.7 决定。"""


@runtime_checkable
class CollectorPlugin(Plugin, Protocol):
    """远程采集插件：Core 在 Worker 进程里调用 ``collect``。"""

    def collect(self, request: CollectRequest, context: PluginContext) -> CollectResult:
        """对 ``request.target_id`` 执行一次采集，把结果写进 ``context.data``。"""


@dataclass(frozen=True, slots=True)
class ImportSource:
    """待导入的本地来源。只描述文件本身，不描述数据内容。"""

    path: Path
    media_type: str | None = None
    size_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class ImportResult:
    """导入结果。记录通过 ``PluginDataAccess`` 写入，这里只报数量与产物引用。"""

    records_written: int
    artifacts: tuple[str, ...] = ()


@runtime_checkable
class ImporterPlugin(Plugin, Protocol):
    """外部数据导入插件：Core 在 Worker 进程里先 ``detect`` 再 ``import_source``。"""

    def detect(self, source: ImportSource) -> bool:
        """能否处理这个来源。只读文件元信息，不解析内容、不写数据。"""

    def import_source(self, source: ImportSource, context: PluginContext) -> ImportResult:
        """解析来源并把标准化记录写进 ``context.data``。同一个来源重复导入必须幂等。"""


@dataclass(frozen=True, slots=True)
class AnalysisRequest:
    """一次分析请求，由 Core 下发。"""

    boot_id: str = ""
    """要分析的启动过程；空串表示分析 Core 选定的全部记录。"""

    record_ids: tuple[str, ...] = ()
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    """分析结果。分析结论作为记录写进 ``PluginDataAccess``，这里只报数量。"""

    annotations_written: int


@runtime_checkable
class AnalyzerPlugin(Plugin, Protocol):
    """分析插件：只读统一数据，结论以记录形式写回，不修改原始数据。"""

    def analyze(self, request: AnalysisRequest, context: PluginContext) -> AnalysisResult:
        """按 ``request`` 分析 ``context.data`` 里的记录并写出分析结论。"""


@dataclass(frozen=True, slots=True)
class VisualizationDescriptor:
    """可视化插件的声明。

    渲染在前端完成（任务 4.x），Python 侧只声明「画什么、需要哪些输入」，
    因此可视化插件没有执行入口，Core 也不会在任务里调用它。
    """

    component: str
    """前端组件的稳定标识，例如 ``timeline.replay``。"""

    required_inputs: tuple[str, ...]
    """需要的时间线输入种类，名字取自 0.2 的数据模型。"""

    options_schema: Mapping[str, Any] = field(default_factory=dict)
    """展示选项的 JSON Schema（对象形态），``{}`` 表示没有可调选项。"""


@runtime_checkable
class VisualizationPlugin(Plugin, Protocol):
    """可视化插件：只做声明，不读写数据。"""

    def describe(self) -> VisualizationDescriptor:
        """返回本插件的展示声明。必须是纯函数，不访问数据库与网络。"""


def invoke_plugin[T](plugin_id: str, call: Callable[[], T]) -> T:
    """Core 调用插件入口方法的唯一方式。

    插件主动抛出的 ``PluginError`` 原样上抛，这是契约内的失败信号（Core 据此决定重试与否）；
    其它任何异常都被包成 ``PluginExecutionError``，原始异常挂在 ``__cause__`` 上。
    因此单个插件崩溃只会让本次任务失败，不会把插件自己的异常类型带进 Core，
    也不会影响已注册的其它插件。
    """
    try:
        return call()
    except PluginError:
        raise
    except Exception as exc:
        raise PluginExecutionError(plugin_id, exc) from exc
