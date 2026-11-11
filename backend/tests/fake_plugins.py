"""假插件样例：验证四类接口可注册、API 版本不匹配会被拒、插件异常不影响 Core。

这些类不是真实插件，也不会进 ``plugins.builtins.BUILTIN_PLUGINS``（那个清单只放真实插件）。
它们的作用是：

- 给下游任务（1.3、1.4、2.1、2.2、3.x、4.x）一个可以直接照着写的形状；
- 给契约测试一个可注册的对象、一个 API 版本过旧的对象和一个会崩的对象。

占位记录：样例写进数据入口的记录是 ``{"kind": "sample.record"}`` 这样的字典；
真实插件写入的是任务 0.2 定义的数据模型对象，本契约不对数据字段做任何约束。
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from system_analyzer.plugins.contracts import (
    PLUGIN_API_VERSION,
    AnalysisRequest,
    AnalysisResult,
    CollectRequest,
    CollectResult,
    ImportResult,
    ImportSource,
    PluginBase,
    PluginContext,
    PluginInputError,
    PluginKind,
    PluginSpec,
    UnifiedRecord,
    VisualizationDescriptor,
)

_SAMPLE_RECORD: dict[str, Any] = {"kind": "sample.record"}


class FakeDataAccess:
    """数据入口的假实现：按插件要求记下谁写了什么，够测试断言即可。"""

    def __init__(self) -> None:
        self.written: list[tuple[str, str]] = []
        self.records: list[UnifiedRecord] = []

    def append_records(
        self,
        records: Sequence[UnifiedRecord],
        *,
        plugin_id: str,
        source: str,
    ) -> int:
        self.records.extend(records)
        self.written.append((plugin_id, source))
        return len(records)

    def query_records(
        self,
        *,
        record_kinds: Sequence[str] = (),
        limit: int | None = None,
    ) -> Sequence[UnifiedRecord]:
        return tuple(self.records)


class FakeCollector(PluginBase):
    """采集插件样例：一次调用写一条占位记录。"""

    spec = PluginSpec(
        id="sample.collector",
        kind=PluginKind.COLLECTOR,
        name="样例采集插件",
        version="0.1.0",
        api_version=PLUGIN_API_VERSION,
        capabilities=("sample.resources",),
    )

    def collect(self, request: CollectRequest, context: PluginContext) -> CollectResult:
        written = context.data.append_records(
            [_SAMPLE_RECORD],
            plugin_id=self.spec.id,
            source=request.run_id,
        )
        return CollectResult(records_written=written)


class FakeImporter(PluginBase):
    """导入插件样例：只认 ``.journal.gz``，导入一次写一条占位记录。"""

    spec = PluginSpec(
        id="sample.importer",
        kind=PluginKind.IMPORTER,
        name="样例导入插件",
        version="0.1.0",
        api_version=PLUGIN_API_VERSION,
        capabilities=("sample.archive",),
        config_schema={
            "type": "object",
            "properties": {"strict": {"type": "boolean"}},
            "additionalProperties": False,
        },
    )

    def detect(self, source: ImportSource) -> bool:
        return source.path.name.endswith(".journal.gz")

    def import_source(self, source: ImportSource, context: PluginContext) -> ImportResult:
        if not self.detect(source):
            raise PluginInputError(f"样例导入插件不认这个来源: {source.path.name}")
        written = context.data.append_records(
            [_SAMPLE_RECORD],
            plugin_id=self.spec.id,
            source=str(source.path),
        )
        return ImportResult(records_written=written)


class FakeAnalyzer(PluginBase):
    """分析插件样例：读一次记录，回写一条分析结论。"""

    spec = PluginSpec(
        id="sample.analyzer",
        kind=PluginKind.ANALYZER,
        name="样例分析插件",
        version="0.1.0",
        api_version=PLUGIN_API_VERSION,
        capabilities=("sample.annotations",),
    )

    def analyze(self, request: AnalysisRequest, context: PluginContext) -> AnalysisResult:
        context.data.query_records(record_kinds=("sample.record",))
        written = context.data.append_records(
            [_SAMPLE_RECORD],
            plugin_id=self.spec.id,
            source=request.boot_id or "all",
        )
        return AnalysisResult(annotations_written=written)


class FakeVisualization(PluginBase):
    """可视化插件样例：只做声明，不读写数据。"""

    spec = PluginSpec(
        id="sample.visualization",
        kind=PluginKind.VISUALIZATION,
        name="样例可视化插件",
        version="0.1.0",
        api_version=PLUGIN_API_VERSION,
        capabilities=("sample.timeline",),
    )

    def describe(self) -> VisualizationDescriptor:
        return VisualizationDescriptor(
            component="timeline.sample",
            required_inputs=("timeline",),
        )


class LegacyApiVersionAnalyzer(PluginBase):
    """API 版本过旧的假插件：注册时必须被拒，错误信息要指出版本差异。"""

    spec = PluginSpec(
        id="sample.legacy-analyzer",
        kind=PluginKind.ANALYZER,
        name="样例分析插件（旧版插件 API）",
        version="0.0.9",
        api_version="0.9",
        capabilities=("sample.annotations",),
    )

    def analyze(self, request: AnalysisRequest, context: PluginContext) -> AnalysisResult:
        return AnalysisResult(annotations_written=0)


class ExplodingAnalyzer(PluginBase):
    """入口方法直接崩溃的假插件：用来证明单个插件崩溃不影响 Core 与其它插件。"""

    spec = PluginSpec(
        id="sample.exploding-analyzer",
        kind=PluginKind.ANALYZER,
        name="样例分析插件（会崩溃）",
        version="0.1.0",
        api_version=PLUGIN_API_VERSION,
        capabilities=("sample.annotations",),
    )

    def analyze(self, request: AnalysisRequest, context: PluginContext) -> AnalysisResult:
        raise RuntimeError("样例插件内部崩溃")


class RecordingAnalyzer(PluginBase):
    """把生命周期回调与入口方法的调用顺序记下来，供测试断言「谁在什么时候被调用一次」。

    真实插件必须无跨调用的可变状态；这里记录调用是测试替身的用法，与 ``FakeDataAccess``
    记录写入是同一种手段。
    """

    spec = PluginSpec(
        id="sample.recording-analyzer",
        kind=PluginKind.ANALYZER,
        name="样例分析插件（记录调用）",
        version="0.1.0",
        api_version=PLUGIN_API_VERSION,
        capabilities=("sample.annotations",),
        config_schema={
            "type": "object",
            "properties": {"strict": {"type": "boolean"}},
            "required": ["strict"],
            "additionalProperties": False,
        },
    )

    def __init__(self) -> None:
        self.calls: list[tuple[str, PluginContext]] = []

    def on_load(self, context: PluginContext) -> None:
        self.calls.append(("load", context))

    def on_unload(self, context: PluginContext) -> None:
        self.calls.append(("unload", context))

    def analyze(self, request: AnalysisRequest, context: PluginContext) -> AnalysisResult:
        self.calls.append(("analyze", context))
        return AnalysisResult(annotations_written=1)

    def called(self) -> list[str]:
        """按发生顺序列出被调用过的方法名。"""
        return [name for name, _ in self.calls]


class ExplodingOnLoadAnalyzer(PluginBase):
    """``on_load`` 就抛异常的假插件：证明加载失败被隔离，入口方法不会被调用。"""

    spec = PluginSpec(
        id="sample.exploding-on-load",
        kind=PluginKind.ANALYZER,
        name="样例分析插件（加载失败）",
        version="0.1.0",
        api_version=PLUGIN_API_VERSION,
        capabilities=("sample.annotations",),
    )

    def __init__(self) -> None:
        self.load_attempts = 0

    def on_load(self, context: PluginContext) -> None:
        self.load_attempts += 1
        raise RuntimeError("样例插件加载失败")

    def analyze(self, request: AnalysisRequest, context: PluginContext) -> AnalysisResult:
        raise AssertionError("加载失败的插件不应被调用")


class FailingUnloadAnalyzer(PluginBase):
    """``on_unload`` 抛异常的假插件：证明一个插件卸载失败不阻止其它插件卸载。"""

    spec = PluginSpec(
        id="sample.failing-unload",
        kind=PluginKind.ANALYZER,
        name="样例分析插件（卸载失败）",
        version="0.1.0",
        api_version=PLUGIN_API_VERSION,
        capabilities=("sample.annotations",),
    )

    def __init__(self) -> None:
        self.unloaded = False

    def on_unload(self, context: PluginContext) -> None:
        self.unloaded = True
        raise RuntimeError("样例插件卸载失败")

    def analyze(self, request: AnalysisRequest, context: PluginContext) -> AnalysisResult:
        return AnalysisResult(annotations_written=0)


class NoCapabilitiesAnalyzer(PluginBase):
    """故意不合规的假插件：没有声明任何能力，注册时必须被认出并给出可读原因。"""

    spec = PluginSpec(
        id="sample.no-capabilities",
        kind=PluginKind.ANALYZER,
        name="样例分析插件（没有声明能力）",
        version="0.1.0",
        api_version=PLUGIN_API_VERSION,
        capabilities=(),
    )

    def analyze(self, request: AnalysisRequest, context: PluginContext) -> AnalysisResult:
        raise AssertionError("不合规的插件不应被注册，更不应被调用")


FAKE_PLUGINS: tuple[PluginBase, ...] = (
    FakeCollector(),
    FakeImporter(),
    FakeAnalyzer(),
    FakeVisualization(),
)
"""四类接口各一个可注册的假插件。"""


def fake_context(
    plugin: PluginBase,
    config: dict[str, Any] | None = None,
) -> tuple[PluginContext, FakeDataAccess]:
    """构造一个装了假数据入口的上下文，返回 ``(上下文, 数据入口)`` 便于断言。"""
    data = FakeDataAccess()
    context = PluginContext(
        plugin_id=plugin.spec.id,
        data=data,
        config=config or {},
        run_id="run-sample",
    )
    return context, data


def sample_source(name: str = "sample.journal.gz") -> ImportSource:
    """假导入来源，只用于测试与样例。"""
    return ImportSource(path=Path("/nonexistent") / name)
