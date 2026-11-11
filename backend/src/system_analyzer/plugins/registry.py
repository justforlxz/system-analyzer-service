"""进程内插件注册表与插件生命周期管理。

API 与 Worker 各是一个进程，但共用同一份注册：两边的启动代码都调用
``plugins.builtins.register_builtins``，插件清单只有 ``BUILTIN_PLUGINS`` 一处。

注册表存的是插件对象本身（满足 ``contracts.Plugin`` 的结构：带 ``spec`` 并实现对应类别的
入口方法），因为它同时承担注册、发现与把插件交给调用方三件事；manifest 通过
``plugin.spec`` 取得。

契约第 4 节剩下的两件工程化工作也在本模块：

- **加载**：注册只做校验与登记，``on_load`` 在第一次真正调用该插件之前执行一次；加载失败
  时不记为已加载，也不调用入口方法。
- **卸载**：Core 停止使用插件（进程退出）时执行 ``on_unload``。卸载用的上下文就是加载时
  那一次传入的 ``PluginContext``，因为进程退出时已经没有任务上下文可给。

加载、卸载与入口方法调用都在 ``contracts.invoke_plugin`` 的边界内执行，所以插件抛出的异常
不会以插件自己的类型进入 Core，也不会影响已注册的其它插件。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from functools import partial
from typing import Any

from system_analyzer.plugins.config import validate_plugin_config
from system_analyzer.plugins.contracts import (
    Plugin,
    PluginContext,
    PluginDataAccess,
    PluginError,
    PluginKind,
    PluginManifestError,
    PluginSpec,
    invoke_plugin,
)

logger = logging.getLogger(__name__)

_registry: PluginRegistry | None = None


class DuplicatePluginError(PluginError):
    """同一个插件 id 被注册两次。"""


class PluginNotRegisteredError(LookupError):
    """Core 索要的插件不在注册表里。

    这不是插件失败而是调用方的配置错误（例如任务引用了未注册的插件 id），
    因此不继承 ``PluginError``：Core 不应把它当成「插件跑失败」去决定重试。
    """


def plugin_spec(plugin: Plugin) -> PluginSpec:
    """取插件的 manifest；没有 manifest 时给出可读的错误而不是 ``AttributeError``。"""
    spec = getattr(plugin, "spec", None)
    if not isinstance(spec, PluginSpec):
        raise PluginManifestError(f"插件对象 {type(plugin).__name__} 没有 spec 属性，无法注册")
    return spec


class PluginRegistry:
    """按 manifest 的 id 索引的插件注册表，同时记录每个插件是否已经加载。"""

    def __init__(self) -> None:
        self._plugins: dict[str, Plugin] = {}
        #: 已加载的插件 id → 加载时那一次传入的上下文，卸载时原样交回 ``on_unload``。
        self._loaded_with: dict[str, PluginContext] = {}

    # --- 注册与发现 ---------------------------------------------------------------

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

    def register_all(self, plugins: Sequence[Plugin]) -> None:
        """注册一整份插件清单：先全部校验，再统一写入。

        校验阶段任何一条不合格都抛错，此时注册表保持原样——不会残留已经校验通过的条目。
        内置清单因此要么整体可用，要么整体失败，不存在「装了一半」的中间状态。
        """
        specs = [(plugin, plugin_spec(plugin)) for plugin in plugins]
        seen: set[str] = set()
        for _, spec in specs:
            spec.validate()
            if spec.id in seen:
                raise DuplicatePluginError(f"插件清单里出现重复的插件 id: {spec.id}")
            seen.add(spec.id)
        for plugin, spec in specs:
            if spec.id not in self._plugins:
                self.register(plugin)

    def get(self, plugin_id: str) -> Plugin | None:
        return self._plugins.get(plugin_id)

    def require(self, plugin_id: str) -> Plugin:
        """按 id 取插件；未注册时抛 ``PluginNotRegisteredError``。"""
        plugin = self._plugins.get(plugin_id)
        if plugin is None:
            raise PluginNotRegisteredError(f"插件 {plugin_id} 未注册，无法调用")
        return plugin

    def spec(self, plugin_id: str) -> PluginSpec | None:
        """按 id 取 manifest；插件没注册时返回 ``None``。"""
        plugin = self._plugins.get(plugin_id)
        return None if plugin is None else plugin.spec

    def all(self) -> tuple[Plugin, ...]:
        return tuple(self._plugins.values())

    def by_kind(self, kind: PluginKind) -> tuple[Plugin, ...]:
        return tuple(plugin for plugin in self._plugins.values() if plugin.spec.kind is kind)

    def by_capability(self, capability: str) -> tuple[Plugin, ...]:
        """按能力查插件：manifest 的 ``capabilities`` 里声明了该能力的插件。

        同一个 capability 允许被多个插件声明，Core 再按任务与配置挑一个。
        """
        return tuple(
            plugin for plugin in self._plugins.values() if capability in plugin.spec.capabilities
        )

    # --- 加载与卸载 ---------------------------------------------------------------

    def is_loaded(self, plugin_id: str) -> bool:
        return plugin_id in self._loaded_with

    def ensure_loaded(self, plugin_id: str, context: PluginContext) -> Plugin:
        """确保插件已加载：第一次调用时执行一次 ``on_load``。

        ``on_load`` 可选，用 ``getattr`` 判断，所以不继承 ``PluginBase`` 的插件同样能用。
        它抛异常表示加载失败：本次不记为已加载（下一次调用会重新尝试），异常照契约边界向外
        抛，调用方因此不会继续调用入口方法。
        """
        plugin = self.require(plugin_id)
        if plugin_id in self._loaded_with:
            return plugin
        on_load = getattr(plugin, "on_load", None)
        if callable(on_load):
            invoke_plugin(plugin_id, lambda: on_load(context))
        self._loaded_with[plugin_id] = context
        return plugin

    def invoke[T](
        self,
        plugin_id: str,
        call: Callable[[Plugin, PluginContext], T],
        *,
        data: PluginDataAccess,
        config: Mapping[str, Any] | None = None,
        run_id: str = "",
    ) -> T:
        """Core 调用插件入口方法的入口：校验配置 → 加载一次 → 调用。

        顺序写死：先校验配置（不合法就直接拒绝，插件一次都不会被碰到），再加载
        （``on_load`` 只执行一次），最后在 ``invoke_plugin`` 边界内执行
        ``call(plugin, context)``。下游任务（1.3 起）应当只用这个方法调插件，
        这样「配置先校验」「先加载再调用」两条不会因为忘记调用而失效。
        """
        plugin = self.require(plugin_id)
        context = PluginContext(
            plugin_id=plugin_id,
            data=data,
            config=validate_plugin_config(plugin.spec, config),
            run_id=run_id,
        )
        self.ensure_loaded(plugin_id, context)
        return invoke_plugin(plugin_id, lambda: call(plugin, context))

    def unload_all(self) -> None:
        """Core 停止使用插件时执行一次 ``on_unload``，然后清空加载记录。

        卸载是收尾动作：单个插件的 ``on_unload`` 抛异常只记日志并继续卸载其它插件，
        不能让一个插件的清理失败把进程卡在退出过程里。从未加载过的插件不调用
        ``on_unload``——没有加载就没有需要释放的东西。
        """
        for plugin_id in list(self._loaded_with):
            plugin = self._plugins[plugin_id]
            context = self._loaded_with.pop(plugin_id)
            on_unload = getattr(plugin, "on_unload", None)
            if not callable(on_unload):
                continue
            try:
                invoke_plugin(plugin_id, partial(on_unload, context))
            except PluginError:
                logger.exception("插件 %s 卸载时抛异常，已跳过", plugin_id)

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
