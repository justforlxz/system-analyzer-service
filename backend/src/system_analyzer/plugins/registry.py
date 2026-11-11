"""进程内插件注册表。

API 与 Worker 各是一个进程，但共用同一份注册：两边的启动代码都调用
``plugins.builtins.register_builtins``，插件清单只有 ``BUILTIN_PLUGINS`` 一处。

注册表存的是插件对象本身（满足 ``contracts.Plugin`` 的结构：带 ``spec`` 并实现对应类别的
入口方法），因为它同时承担「发现」与「把插件交给调用方」两件事；manifest 通过
``plugin.spec`` 取得。完整的注册机制、生命周期管理与异常隔离的工程化属于任务 1.2，
本模块只保留 0.4 契约要求的门禁：manifest 校验（含插件 API 版本）与 id 唯一。
"""

from system_analyzer.plugins.contracts import (
    Plugin,
    PluginError,
    PluginKind,
    PluginManifestError,
    PluginSpec,
)

_registry: "PluginRegistry | None" = None


class DuplicatePluginError(PluginError):
    """同一个插件 id 被注册两次。"""


def plugin_spec(plugin: Plugin) -> PluginSpec:
    """取插件的 manifest；没有 manifest 时给出可读的错误而不是 ``AttributeError``。"""
    spec = getattr(plugin, "spec", None)
    if not isinstance(spec, PluginSpec):
        raise PluginManifestError(f"插件对象 {type(plugin).__name__} 没有 spec 属性，无法注册")
    return spec


class PluginRegistry:
    """按 manifest 的 id 索引的插件注册表。"""

    def __init__(self) -> None:
        self._plugins: dict[str, Plugin] = {}

    def register(self, plugin: Plugin) -> None:
        """注册一个插件。

        校验顺序写死：manifest 存在 → manifest 合格（含插件 API 版本）→ id 未注册过。
        任何一步不通过都抛错而不是跳过，拒绝原因写在异常信息里。
        """
        spec = plugin_spec(plugin)
        spec.validate()
        if spec.id in self._plugins:
            raise DuplicatePluginError(f"插件 id 重复注册: {spec.id}")
        self._plugins[spec.id] = plugin

    def get(self, plugin_id: str) -> Plugin | None:
        return self._plugins.get(plugin_id)

    def spec(self, plugin_id: str) -> PluginSpec | None:
        """按 id 取 manifest；插件没注册时返回 ``None``。"""
        plugin = self._plugins.get(plugin_id)
        return None if plugin is None else plugin.spec

    def all(self) -> tuple[Plugin, ...]:
        return tuple(self._plugins.values())

    def by_kind(self, kind: PluginKind) -> tuple[Plugin, ...]:
        return tuple(plugin for plugin in self._plugins.values() if plugin.spec.kind is kind)

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
