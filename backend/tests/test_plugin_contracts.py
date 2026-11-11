"""插件契约测试：manifest 规则、四类接口结构、生命周期、异常隔离与「插件不得做的事」。

这一组用例就是 ``docs/plugin-contracts.md`` 里「契约测试方案」的实现，跑法见该文档。
"""

import ast
from dataclasses import replace
from pathlib import Path

import pytest

import system_analyzer
from fake_plugins import (
    FAKE_PLUGINS,
    ExplodingAnalyzer,
    ExplodingOnLoadAnalyzer,
    FailingUnloadAnalyzer,
    FakeAnalyzer,
    FakeCollector,
    FakeDataAccess,
    FakeImporter,
    FakeVisualization,
    LegacyApiVersionAnalyzer,
    NoCapabilitiesAnalyzer,
    RecordingAnalyzer,
    fake_context,
    sample_source,
)
from system_analyzer.plugins.config import PluginConfigError, validate_plugin_config
from system_analyzer.plugins.contracts import (
    PLUGIN_API_VERSION,
    AnalysisRequest,
    AnalyzerPlugin,
    ApiVersionMismatchError,
    CollectorPlugin,
    CollectRequest,
    ImporterPlugin,
    PluginBase,
    PluginContext,
    PluginExecutionError,
    PluginInputError,
    PluginKind,
    PluginManifestError,
    VisualizationPlugin,
    invoke_plugin,
)
from system_analyzer.plugins.registry import (
    PluginNotRegisteredError,
    PluginRegistry,
    reset_registry,
)

PACKAGE_ROOT = Path(system_analyzer.__file__).parent
TESTS_ROOT = Path(__file__).parent

#: 插件实现不允许碰的东西：数据库驱动、迁移工具、Core 的存储层与全局配置/引擎。
BANNED_IMPORTS = (
    "sqlalchemy",
    "asyncpg",
    "alembic",
    "system_analyzer.storage",
    "system_analyzer.core",
)

#: 三类需要写实现文件的插件目录。
PLUGIN_DIRS = ("collectors", "importers", "analyzers")


@pytest.fixture(autouse=True)
def clean_registry():
    reset_registry()
    yield
    reset_registry()


# --- 四类接口：结构可检查，样例可运行 -----------------------------------------------


@pytest.mark.parametrize(
    ("plugin", "protocol", "kind"),
    [
        (FakeCollector(), CollectorPlugin, PluginKind.COLLECTOR),
        (FakeImporter(), ImporterPlugin, PluginKind.IMPORTER),
        (FakeAnalyzer(), AnalyzerPlugin, PluginKind.ANALYZER),
        (FakeVisualization(), VisualizationPlugin, PluginKind.VISUALIZATION),
    ],
)
def test_fake_plugin_matches_its_kind_protocol(
    plugin: PluginBase, protocol: type, kind: PluginKind
) -> None:
    """每类接口都有一个假插件，且结构上满足该类接口。"""
    assert plugin.spec.kind is kind
    assert isinstance(plugin, protocol)
    plugin.spec.validate()


def test_fake_plugins_cover_all_four_kinds() -> None:
    assert {plugin.spec.kind for plugin in FAKE_PLUGINS} == set(PluginKind)


def test_collector_writes_through_the_single_data_entry() -> None:
    collector = FakeCollector()
    context, data = fake_context(collector)

    result = collector.collect(CollectRequest(target_id="target-1", run_id="run-1"), context)

    assert result.records_written == 1
    assert data.written == [(collector.spec.id, "run-1")]


def test_importer_detects_then_imports() -> None:
    importer = FakeImporter()
    context, data = fake_context(importer)
    source = sample_source("boot-1.journal.gz")

    assert importer.detect(source) is True
    result = importer.import_source(source, context)

    assert result.records_written == 1
    assert data.written == [(importer.spec.id, str(source.path))]


def test_analyzer_reads_then_writes_annotations() -> None:
    analyzer = FakeAnalyzer()
    context, data = fake_context(analyzer)

    result = analyzer.analyze(AnalysisRequest(boot_id="boot-1"), context)

    assert result.annotations_written == 1
    assert data.written == [(analyzer.spec.id, "boot-1")]


def test_visualization_plugin_only_declares() -> None:
    descriptor = FakeVisualization().describe()

    assert descriptor.component == "timeline.sample"
    assert descriptor.required_inputs == ("timeline",)


def test_lifecycle_callbacks_default_to_noop() -> None:
    plugin = FakeAnalyzer()
    context, _ = fake_context(plugin)

    assert plugin.on_load(context) is None
    assert plugin.on_unload(context) is None


def test_plugin_may_skip_the_base_class() -> None:
    """生命周期回调可选：不继承 ``PluginBase`` 的插件也能注册。"""

    class BareCollector:
        spec = replace(FakeCollector.spec, id="sample.bare-collector")

        def collect(self, request: CollectRequest, context: PluginContext) -> object:
            raise AssertionError("本用例不调用采集")

    registry = PluginRegistry()
    registry.register(BareCollector())

    assert registry.get("sample.bare-collector") is not None


# --- manifest 规则 ------------------------------------------------------------------


def test_valid_manifest_passes() -> None:
    for plugin in FAKE_PLUGINS:
        plugin.spec.validate()


@pytest.mark.parametrize("field", ["id", "name", "version", "api_version"])
def test_empty_required_field_is_rejected(field: str) -> None:
    spec = replace(FakeAnalyzer.spec, **{field: "  "})

    with pytest.raises(PluginManifestError) as excinfo:
        spec.validate()

    assert field in str(excinfo.value)


@pytest.mark.parametrize(
    ("field", "value"),
    [("id", None), ("name", 0), ("capabilities", (1,))],
)
def test_non_string_manifest_value_is_rejected(field: str, value: object) -> None:
    """字段写成了非字符串也要给出可读原因，而不是抛出 AttributeError。"""
    spec = replace(FakeAnalyzer.spec, **{field: value})

    with pytest.raises(PluginManifestError):
        spec.validate()


@pytest.mark.parametrize("kind", ["analyzer", "analyser", ""])
def test_kind_outside_plugin_kind_is_rejected(kind: str) -> None:
    """``kind`` 写成字符串或拼错时必须被拒：否则 by_kind 永远查不到，插件静默失效。"""
    spec = replace(FakeAnalyzer.spec, kind=kind)

    with pytest.raises(PluginManifestError) as excinfo:
        spec.validate()

    assert "kind" in str(excinfo.value)
    assert repr(kind) in str(excinfo.value)


def test_capabilities_must_be_a_tuple() -> None:
    """写成单个字符串会被逐字符当成 capability，必须拒绝。"""
    spec = replace(FakeAnalyzer.spec, capabilities="sample.annotations")

    with pytest.raises(PluginManifestError) as excinfo:
        spec.validate()

    assert "tuple" in str(excinfo.value)


def test_api_version_must_match_exactly() -> None:
    """版本比较是字符串全等：``1.0.0`` 与 ``1.0`` 不是同一个版本。"""
    spec = replace(FakeAnalyzer.spec, api_version="1.0.0")

    with pytest.raises(ApiVersionMismatchError):
        spec.validate()


def test_api_version_mismatch_message_shows_both_versions() -> None:
    spec = LegacyApiVersionAnalyzer.spec

    with pytest.raises(ApiVersionMismatchError) as excinfo:
        spec.validate()

    error = excinfo.value
    assert error.plugin_id == spec.id
    assert error.required == "0.9"
    assert error.supported == PLUGIN_API_VERSION
    assert "0.9" in str(error)
    assert PLUGIN_API_VERSION in str(error)


def test_empty_capabilities_are_rejected() -> None:
    spec = replace(FakeAnalyzer.spec, capabilities=())

    with pytest.raises(PluginManifestError) as excinfo:
        spec.validate()

    assert "capabilities" in str(excinfo.value)


def test_blank_capability_is_rejected() -> None:
    spec = replace(FakeAnalyzer.spec, capabilities=("sample.annotations", " "))

    with pytest.raises(PluginManifestError):
        spec.validate()


@pytest.mark.parametrize("schema", [{"type": "array"}, [], "not-a-schema"])
def test_config_schema_must_be_object_schema(schema: object) -> None:
    spec = replace(FakeAnalyzer.spec, config_schema=schema)  # type: ignore[arg-type]

    with pytest.raises(PluginManifestError):
        spec.validate()


def test_config_schema_defaults_to_no_config() -> None:
    assert FakeAnalyzer.spec.config_schema == {}


# --- 异常隔离 -----------------------------------------------------------------------


def test_plugin_crash_is_wrapped_and_registry_survives() -> None:
    """插件抛异常不影响 Core，也不影响已经注册的其它插件。"""
    registry = PluginRegistry()
    registry.register(FakeAnalyzer())
    registry.register(ExplodingAnalyzer())

    exploding = registry.get(ExplodingAnalyzer.spec.id)
    assert exploding is not None
    context, _ = fake_context(exploding)

    with pytest.raises(PluginExecutionError) as excinfo:
        invoke_plugin(
            ExplodingAnalyzer.spec.id,
            lambda: exploding.analyze(AnalysisRequest(), context),  # type: ignore[attr-defined]
        )

    assert excinfo.value.plugin_id == ExplodingAnalyzer.spec.id
    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert len(registry) == 2

    healthy = registry.get(FakeAnalyzer.spec.id)
    assert healthy is not None
    healthy_context, data = fake_context(healthy)
    result = invoke_plugin(
        healthy.spec.id,
        lambda: healthy.analyze(AnalysisRequest(boot_id="boot-1"), healthy_context),  # type: ignore[attr-defined]
    )

    assert result.annotations_written == 1
    assert data.written == [(FakeAnalyzer.spec.id, "boot-1")]


def test_contract_error_is_not_rewrapped() -> None:
    """插件主动抛的 ``PluginError`` 是契约内的失败信号，原样交给 Core。"""
    importer = FakeImporter()
    context, _ = fake_context(importer)

    with pytest.raises(PluginInputError):
        invoke_plugin(
            importer.spec.id,
            lambda: importer.import_source(sample_source("other.tar"), context),
        )


# --- 插件不得做的事 ---------------------------------------------------------------


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def _plugin_sources() -> list[Path]:
    sources = [path for name in PLUGIN_DIRS for path in (PACKAGE_ROOT / name).rglob("*.py")]
    sources.append(TESTS_ROOT / "fake_plugins.py")
    return sources


def test_plugin_implementations_never_import_storage() -> None:
    """插件不得直接操作数据库：实现文件里连存储层的 import 都不许出现。"""
    offenders = []
    for path in _plugin_sources():
        for module in _imported_modules(path):
            if any(
                module == banned or module.startswith(f"{banned}.") for banned in BANNED_IMPORTS
            ):
                offenders.append(f"{path.relative_to(PACKAGE_ROOT.parent)}: {module}")

    assert not offenders, "插件实现里出现了存储层或全局配置的 import：" + "; ".join(offenders)


def _defines_builtin_plugins(source: str) -> bool:
    """源码里是否定义了 ``BUILTIN_PLUGINS``。

    同时处理 ``BUILTIN_PLUGINS = ()`` 与 ``BUILTIN_PLUGINS: tuple[Plugin, ...] = ()``：
    后者在语法树里是 ``ast.AnnAssign``，没有 ``targets`` 属性。
    """
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if any(
            isinstance(target, ast.Name) and target.id == "BUILTIN_PLUGINS" for target in targets
        ):
            return True
    return False


def test_builtin_plugin_list_lives_in_one_module_only() -> None:
    """内置插件清单只允许出现在 ``plugins/builtins.py``。"""
    offenders = [
        str(path.relative_to(PACKAGE_ROOT.parent))
        for path in PACKAGE_ROOT.rglob("*.py")
        if not (path.name == "builtins.py" and path.parent.name == "plugins")
        and _defines_builtin_plugins(path.read_text(encoding="utf-8"))
    ]

    assert not offenders, "内置插件清单出现在别的模块里：" + "; ".join(offenders)


@pytest.mark.parametrize(
    "source",
    [
        "BUILTIN_PLUGINS = ()\n",
        "BUILTIN_PLUGINS: tuple[Plugin, ...] = ()\n",
        "class Registry:\n    BUILTIN_PLUGINS: tuple = ()\n",
        "def load():\n    BUILTIN_PLUGINS = ()\n",
    ],
)
def test_builtin_list_scanner_catches_every_definition_style(source: str) -> None:
    """守卫用例本身要有覆盖：两种赋值写法都要被认出来。"""
    assert _defines_builtin_plugins(source) is True


def test_builtin_list_scanner_ignores_other_names() -> None:
    assert _defines_builtin_plugins("OTHER_PLUGINS = ()\n") is False


# --- 生命周期：on_load / on_unload 的真实调用方 ---------------------------------------


def test_invoke_loads_once_before_the_entry_method() -> None:
    """第一次调用前执行一次 ``on_load``，之后每次调用都不再重复加载。"""
    plugin = RecordingAnalyzer()
    registry = PluginRegistry()
    registry.register(plugin)
    data = FakeDataAccess()

    first = registry.invoke(
        plugin.spec.id,
        lambda loaded, context: loaded.analyze(AnalysisRequest(boot_id="boot-1"), context),
        data=data,
        config={"strict": True},
        run_id="run-1",
    )
    second = registry.invoke(
        plugin.spec.id,
        lambda loaded, context: loaded.analyze(AnalysisRequest(boot_id="boot-1"), context),
        data=data,
        config={"strict": True},
        run_id="run-1",
    )

    assert first.annotations_written == second.annotations_written == 1
    assert plugin.called() == ["load", "analyze", "analyze"]
    assert registry.is_loaded(plugin.spec.id)

    # 配置与任务 id 通过同一个上下文交给插件，而不是让插件自己去读环境变量。
    entry_context = plugin.calls[1][1]
    assert entry_context.plugin_id == plugin.spec.id
    assert entry_context.config == {"strict": True}
    assert entry_context.run_id == "run-1"
    assert entry_context.data is data


def test_unload_all_calls_on_unload_with_the_load_time_context() -> None:
    """卸载时执行 ``on_unload``，用的是加载那一次的上下文；重复卸载不再调用。"""
    plugin = RecordingAnalyzer()
    registry = PluginRegistry()
    registry.register(plugin)
    registry.invoke(
        plugin.spec.id,
        lambda loaded, context: loaded.analyze(AnalysisRequest(), context),
        data=FakeDataAccess(),
        config={"strict": True},
    )

    registry.unload_all()

    assert plugin.called() == ["load", "analyze", "unload"]
    assert plugin.calls[0][1] is plugin.calls[-1][1]
    assert not registry.is_loaded(plugin.spec.id)
    assert len(registry) == 1

    registry.unload_all()

    assert plugin.called() == ["load", "analyze", "unload"]


def test_unload_all_skips_plugins_that_were_never_loaded() -> None:
    """没有加载就没有需要释放的东西，``on_unload`` 不调用。"""
    plugin = RecordingAnalyzer()
    registry = PluginRegistry()
    registry.register(plugin)

    registry.unload_all()

    assert plugin.called() == []


def test_on_load_failure_is_isolated_and_other_plugins_still_work() -> None:
    """加载失败的插件不调用入口方法、不改注册表，其它插件照常可用。"""
    exploding = ExplodingOnLoadAnalyzer()
    healthy = FakeAnalyzer()
    registry = PluginRegistry()
    registry.register(healthy)
    registry.register(exploding)

    with pytest.raises(PluginExecutionError) as excinfo:
        registry.invoke(
            exploding.spec.id,
            lambda loaded, context: loaded.analyze(AnalysisRequest(), context),
            data=FakeDataAccess(),
        )

    assert isinstance(excinfo.value.__cause__, RuntimeError)
    assert exploding.load_attempts == 1
    assert not registry.is_loaded(exploding.spec.id)
    assert len(registry) == 2

    data = FakeDataAccess()
    result = registry.invoke(
        healthy.spec.id,
        lambda loaded, context: loaded.analyze(AnalysisRequest(boot_id="boot-1"), context),
        data=data,
    )

    assert result.annotations_written == 1
    assert data.written == [(healthy.spec.id, "boot-1")]


def test_unload_failure_of_one_plugin_does_not_block_the_others() -> None:
    """卸载是收尾动作：一个插件的 ``on_unload`` 抛异常，其余插件仍然被卸载。"""
    failing = FailingUnloadAnalyzer()
    healthy = RecordingAnalyzer()
    registry = PluginRegistry()
    registry.register(failing)
    registry.register(healthy)
    registry.invoke(
        failing.spec.id,
        lambda loaded, context: loaded.analyze(AnalysisRequest(), context),
        data=FakeDataAccess(),
    )
    registry.invoke(
        healthy.spec.id,
        lambda loaded, context: loaded.analyze(AnalysisRequest(), context),
        data=FakeDataAccess(),
        config={"strict": True},
    )

    registry.unload_all()

    assert failing.unloaded is True
    assert healthy.called() == ["load", "analyze", "unload"]


def test_invoke_rejects_an_unknown_plugin_id() -> None:
    with pytest.raises(PluginNotRegisteredError):
        PluginRegistry().invoke(
            "sample.missing", lambda _plugin, _context: None, data=FakeDataAccess()
        )


# --- 配置校验 -----------------------------------------------------------------------


def test_invalid_config_is_rejected_before_the_plugin_is_touched() -> None:
    """配置不合法时直接拒绝：连 ``on_load`` 都不执行，插件一次都没被碰到。"""
    plugin = RecordingAnalyzer()
    registry = PluginRegistry()
    registry.register(plugin)

    with pytest.raises(PluginConfigError) as excinfo:
        registry.invoke(
            plugin.spec.id,
            lambda loaded, context: loaded.analyze(AnalysisRequest(), context),
            data=FakeDataAccess(),
            config={"strict": "yes"},
        )

    message = str(excinfo.value)
    assert plugin.spec.id in message
    assert "strict" in message
    assert plugin.called() == []
    assert not registry.is_loaded(plugin.spec.id)


def test_config_is_refused_when_the_manifest_declares_no_config() -> None:
    """``config_schema = {}`` 表示不接受任何配置，传了就是错。"""
    assert validate_plugin_config(FakeAnalyzer.spec) == {}
    assert validate_plugin_config(FakeAnalyzer.spec, {}) == {}

    with pytest.raises(PluginConfigError) as excinfo:
        validate_plugin_config(FakeAnalyzer.spec, {"strict": True})

    assert FakeAnalyzer.spec.id in str(excinfo.value)


def test_missing_required_config_is_rejected() -> None:
    with pytest.raises(PluginConfigError) as excinfo:
        validate_plugin_config(RecordingAnalyzer.spec, {})

    assert "strict" in str(excinfo.value)


# --- API 版本的全部拒绝形态 ----------------------------------------------------------


@pytest.mark.parametrize(
    ("api_version", "error"),
    [
        ("0.9", ApiVersionMismatchError),
        ("1", ApiVersionMismatchError),
        ("1.0.0", ApiVersionMismatchError),
        (" 1.0", ApiVersionMismatchError),
        ("1.0 ", ApiVersionMismatchError),
        ("", PluginManifestError),
        ("   ", PluginManifestError),
    ],
)
def test_api_version_outside_the_exact_match_is_rejected(
    api_version: str,
    error: type[Exception],
) -> None:
    """版本比较是字符串全等：少一个分量、多一个分量、多一个空格都要拒绝。"""
    spec = replace(FakeAnalyzer.spec, api_version=api_version)

    with pytest.raises(error) as excinfo:
        spec.validate()

    message = str(excinfo.value)
    if error is ApiVersionMismatchError:
        assert api_version in message
    else:
        assert "api_version" in message


def test_intentionally_noncompliant_plugin_is_rejected_with_a_readable_reason() -> None:
    """故意不合规的插件（没有声明能力）进不了注册表，错误信息指出插件与字段。"""
    registry = PluginRegistry()

    with pytest.raises(PluginManifestError) as excinfo:
        registry.register(NoCapabilitiesAnalyzer())

    message = str(excinfo.value)
    assert NoCapabilitiesAnalyzer.spec.id in message
    assert "capabilities" in message
    assert len(registry) == 0
