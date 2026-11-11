"""插件注册表的测试：manifest 门禁、id 唯一，且 API 与 Worker 只注册一份。"""

from dataclasses import replace

import pytest

from fake_plugins import (
    FAKE_PLUGINS,
    FakeAnalyzer,
    FakeImporter,
    LegacyApiVersionAnalyzer,
)
from system_analyzer.api.app import create_app
from system_analyzer.plugins import builtins
from system_analyzer.plugins.builtins import BUILTIN_PLUGINS, register_builtins
from system_analyzer.plugins.contracts import (
    PLUGIN_API_VERSION,
    ApiVersionMismatchError,
    PluginKind,
    PluginManifestError,
)
from system_analyzer.plugins.registry import (
    DuplicatePluginError,
    PluginRegistry,
    get_registry,
    reset_registry,
)


@pytest.fixture(autouse=True)
def clean_registry():
    reset_registry()
    yield
    reset_registry()


def test_fake_plugins_can_be_registered_and_listed() -> None:
    """四类假插件都可注册，注册表按注册顺序列出它们。"""
    registry = PluginRegistry()
    for plugin in FAKE_PLUGINS:
        registry.register(plugin)

    assert [plugin.spec.id for plugin in registry.all()] == [
        plugin.spec.id for plugin in FAKE_PLUGINS
    ]
    assert len(registry) == len(FAKE_PLUGINS)
    assert registry.spec("sample.importer") is FakeImporter.spec


def test_duplicate_id_is_rejected() -> None:
    registry = PluginRegistry()
    registry.register(FakeAnalyzer())

    with pytest.raises(DuplicatePluginError):
        registry.register(FakeAnalyzer())


def test_api_version_mismatch_is_rejected_at_registration() -> None:
    """版本不符的插件进不了注册表，错误信息给出双方版本。"""
    registry = PluginRegistry()

    with pytest.raises(ApiVersionMismatchError) as excinfo:
        registry.register(LegacyApiVersionAnalyzer())

    message = str(excinfo.value)
    assert LegacyApiVersionAnalyzer.spec.id in message
    assert LegacyApiVersionAnalyzer.spec.api_version in message
    assert PLUGIN_API_VERSION in message
    assert len(registry) == 0


def test_plugin_with_string_kind_cannot_be_registered() -> None:
    """``kind`` 写成字符串时注册必须失败：否则插件能列出但 by_kind 永远选不到。"""

    class StringKindAnalyzer(FakeAnalyzer):
        spec = replace(FakeAnalyzer.spec, kind="analyzer")

    registry = PluginRegistry()

    with pytest.raises(PluginManifestError) as excinfo:
        registry.register(StringKindAnalyzer())

    assert "kind" in str(excinfo.value)
    assert len(registry) == 0


def test_plugin_without_manifest_is_rejected() -> None:
    class NoManifest:
        pass

    with pytest.raises(PluginManifestError):
        PluginRegistry().register(NoManifest())


def test_builtin_list_entry_without_manifest_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """清单里放了没有 ``spec`` 的对象时，报的是可读的 PluginManifestError。"""
    monkeypatch.setattr(builtins, "BUILTIN_PLUGINS", (object(),))

    with pytest.raises(PluginManifestError) as excinfo:
        builtins.register_builtins(PluginRegistry())

    assert "spec" in str(excinfo.value)


def test_register_builtins_is_idempotent() -> None:
    registry = register_builtins(get_registry())
    register_builtins(registry)

    assert len(registry) == len(BUILTIN_PLUGINS)


def test_register_builtins_uses_process_wide_registry() -> None:
    register_builtins()
    registry = get_registry()

    assert len(registry) == len(BUILTIN_PLUGINS)
    assert registry is get_registry()


def test_api_and_worker_share_one_registry() -> None:
    """API 构造两次、Worker 再注册一次，注册表里仍然只有一份内置插件。"""
    create_app()
    create_app()
    register_builtins()

    assert len(get_registry()) == len(BUILTIN_PLUGINS)


def test_by_kind_filters() -> None:
    registry = PluginRegistry()
    for plugin in FAKE_PLUGINS:
        registry.register(plugin)

    assert [plugin.spec.id for plugin in registry.by_kind(PluginKind.IMPORTER)] == [
        "sample.importer"
    ]
    assert len(registry.by_kind(PluginKind.ANALYZER)) == 1


def test_all_four_kinds_register_through_the_single_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """四类插件都走 ``register_builtins`` 这一个入口，注册后可以按类别查出。"""
    monkeypatch.setattr(builtins, "BUILTIN_PLUGINS", FAKE_PLUGINS)

    registry = register_builtins(PluginRegistry())

    assert len(registry) == len(FAKE_PLUGINS)
    assert {plugin.spec.kind for plugin in registry.all()} == set(PluginKind)
    assert [plugin.spec.id for plugin in registry.by_kind(PluginKind.COLLECTOR)] == [
        "sample.collector"
    ]


def test_by_capability_finds_plugins_by_declared_capability() -> None:
    """按 manifest 的 capabilities 查插件；没人声明的能力返回空。"""
    registry = PluginRegistry()
    for plugin in FAKE_PLUGINS:
        registry.register(plugin)

    assert [plugin.spec.id for plugin in registry.by_capability("sample.annotations")] == [
        "sample.analyzer"
    ]
    assert registry.by_capability("sample.unknown") == ()


def test_builtin_list_with_api_version_mismatch_leaves_the_registry_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """清单里混入版本不符的插件时，整份清单都不注册，注册表保持原样。"""
    monkeypatch.setattr(
        builtins,
        "BUILTIN_PLUGINS",
        (FakeAnalyzer(), LegacyApiVersionAnalyzer()),
    )
    registry = PluginRegistry()

    with pytest.raises(ApiVersionMismatchError):
        register_builtins(registry)

    assert len(registry) == 0
    assert registry.get(FakeAnalyzer.spec.id) is None


def test_builtin_list_with_duplicate_ids_is_rejected_before_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一份清单里出现重复 id 时同样一条都不写。"""
    monkeypatch.setattr(builtins, "BUILTIN_PLUGINS", (FakeAnalyzer(), FakeAnalyzer()))
    registry = PluginRegistry()

    with pytest.raises(DuplicatePluginError):
        register_builtins(registry)

    assert len(registry) == 0
