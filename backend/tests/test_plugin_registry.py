"""插件注册表的测试：id 唯一，且 API 与 Worker 只注册一份。"""

import pytest

from system_analyzer.api.app import create_app
from system_analyzer.plugins.builtins import BUILTIN_PLUGINS, register_builtins
from system_analyzer.plugins.contracts import PluginKind, PluginSpec
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


def test_duplicate_id_is_rejected() -> None:
    registry = PluginRegistry()
    spec = PluginSpec(id="demo.analyzer", kind=PluginKind.ANALYZER)

    registry.register(spec)

    with pytest.raises(DuplicatePluginError):
        registry.register(spec)


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
    registry.register(PluginSpec(id="a.importer", kind=PluginKind.IMPORTER))
    registry.register(PluginSpec(id="b.analyzer", kind=PluginKind.ANALYZER))

    assert [spec.id for spec in registry.by_kind(PluginKind.IMPORTER)] == ["a.importer"]
    assert [spec.id for spec in registry.all()] == ["a.importer", "b.analyzer"]
