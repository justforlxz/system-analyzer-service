"""内置插件清单与唯一注册入口。

新增插件时：

1. 在 ``collectors`` / ``importers`` / ``analyzers`` 里实现插件并导出它的 ``PluginSpec``；
2. 只在下面的 ``BUILTIN_PLUGINS`` 里加一行。

API 进程与 Worker 进程都调用 ``register_builtins``，不允许在任意一侧另写一份清单。
"""

from system_analyzer.plugins.contracts import PluginSpec
from system_analyzer.plugins.registry import PluginRegistry, get_registry

BUILTIN_PLUGINS: tuple[PluginSpec, ...] = ()


def register_builtins(registry: PluginRegistry | None = None) -> PluginRegistry:
    """把内置插件注册进进程内注册表。

    可重复调用：已经注册过的 id 跳过，因此 API 与 Worker 各自启动一次不会重复注册，
    也就在结构上保证了"同一个插件不会在两处各注册一套"。
    """
    target = registry if registry is not None else get_registry()
    for spec in BUILTIN_PLUGINS:
        if target.get(spec.id) is None:
            target.register(spec)
    return target
