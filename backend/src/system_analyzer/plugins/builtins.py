"""内置插件清单与唯一注册入口。

新增插件时：

1. 在 ``collectors`` / ``importers`` / ``analyzers`` 里实现插件（给出 ``spec``，实现对应类别的
   入口方法；继承 ``plugins.contracts.PluginBase`` 可以省掉两个空的回调）；
2. 只在下面的 ``BUILTIN_PLUGINS`` 里加一行的插件实例。

API 进程与 Worker 进程都调用 ``register_builtins``，不允许在任意一侧另写一份清单。
"""

from system_analyzer.importers.bootrec_importer import BootrecImporter
from system_analyzer.importers.journal_archive_importer import JournalArchiveImporter
from system_analyzer.plugins.contracts import Plugin
from system_analyzer.plugins.registry import PluginRegistry, get_registry

BUILTIN_PLUGINS: tuple[Plugin, ...] = (
    # 1.3：bootrec v1 导入。新增插件追加在后面，不重排已有内容。
    BootrecImporter(),
    # 1.4：Journal 压缩包导入。
    JournalArchiveImporter(),
)
"""内置插件清单。

插件对象是无状态的：配置与数据入口在每次调用时通过 ``PluginContext`` 传入，
因此可以在模块加载时直接构造实例。
"""


def register_builtins(registry: PluginRegistry | None = None) -> PluginRegistry:
    """把内置插件注册进进程内注册表。

    可重复调用：已经注册过的 id 跳过，因此 API 与 Worker 各自启动一次不会重复注册，
    也就在结构上保证了"同一个插件不会在两处各注册一套"。

    注册不合格（清单里放了没有 manifest 的对象、manifest 有错、插件 API 版本不符）时直接抛错，
    不做"跳过继续"：``BUILTIN_PLUGINS`` 是仓库代码，出错必须立刻暴露，
    而不是让服务带着半个插件集启动。整份清单先全部校验通过才写入注册表，失败时注册表保持原样。
    """
    target = registry if registry is not None else get_registry()
    target.register_all(BUILTIN_PLUGINS)
    return target
