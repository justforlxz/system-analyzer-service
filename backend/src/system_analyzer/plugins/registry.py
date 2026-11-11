"""进程内插件注册表。

API 与 Worker 各是一个进程，但共用同一份注册：两边的启动代码都调用
``plugins.builtins.register_builtins``，插件清单只有 ``BUILTIN_PLUGINS`` 一处。
"""

from system_analyzer.plugins.contracts import PluginKind, PluginSpec

_registry: "PluginRegistry | None" = None


class DuplicatePluginError(RuntimeError):
    """同一个插件 id 被注册两次。"""


class PluginRegistry:
    """按 id 索引的插件注册表。"""

    def __init__(self) -> None:
        self._plugins: dict[str, PluginSpec] = {}

    def register(self, spec: PluginSpec) -> None:
        """注册一个插件，id 重复时抛错而不是覆盖。"""
        if spec.id in self._plugins:
            raise DuplicatePluginError(f"插件 id 重复注册: {spec.id}")
        self._plugins[spec.id] = spec

    def get(self, plugin_id: str) -> PluginSpec | None:
        return self._plugins.get(plugin_id)

    def all(self) -> tuple[PluginSpec, ...]:
        return tuple(self._plugins.values())

    def by_kind(self, kind: PluginKind) -> tuple[PluginSpec, ...]:
        return tuple(spec for spec in self._plugins.values() if spec.kind is kind)

    def __len__(self) -> int:
        return len(self._plugins)


def get_registry() -> PluginRegistry:
    """返回进程内唯一的注册表。"""
    global _registry
    if _registry is None:
        _registry = PluginRegistry()
    return _registry


def reset_registry() -> None:
    """清空进程内注册表，只供测试与进程重新初始化使用。"""
    global _registry
    _registry = PluginRegistry()
