"""Journal 压缩包导入插件：把归档解析后写进统一存储。

解析与归一化在 ``journal_archive`` 模块；本模块只做插件该做的两件事：``detect`` 认来源、
``import_source`` 调数据入口写入。这样「怎么解析」可以单独测，「怎么注册与调用」按
``docs/plugin-contracts.md`` 第 3.3 节测。

``import_source`` 是**同步**方法，与 ``docs/plugin-contracts.md`` 第 3.3 节一致：经
``PluginRegistry.invoke`` 直接拿到 ``ImportResult``，插件抛出的非 ``PluginError`` 异常也在那层边界
里被包成 ``PluginExecutionError``。数据入口用 1.3 已经定下的同步协议
``importers.bootrec_importer.CollectionImportAccess``——导入要等一次完整的 ``import_collection``
事务，而存储层是协程，所以 Core 要把它包成同步入口后写进 ``context.data``；如果传进来的是异步
入口，这里会给出可读的失败原因，而不是「成功」返回一个没导入任何东西的协程。
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from system_analyzer.importers.bootrec_importer import CollectionImportAccess
from system_analyzer.importers.journal_archive import (
    CAPABILITY,
    CONFIG_MAX_BYTES,
    DEFAULT_MAX_DECOMPRESSED_BYTES,
    PLUGIN_ID,
    archive_digest,
    build_bundle,
    detect,
    parse_journal_archive,
)
from system_analyzer.plugins.contracts import (
    PLUGIN_API_VERSION,
    ImportResult,
    ImportSource,
    PluginBase,
    PluginContext,
    PluginInputError,
    PluginKind,
    PluginSpec,
)

SOURCE_HINT = (
    "支持 Journal 原生文件（.journal）、journalctl -o export 文本，"
    "以及它们的 gzip / xz / bzip2 / tar 压缩包"
)


class JournalArchiveImporter(PluginBase):
    """Journal 压缩包导入插件。无状态：配置与数据入口每次调用时由 ``PluginContext`` 传入。"""

    spec = PluginSpec(
        id=PLUGIN_ID,
        kind=PluginKind.IMPORTER,
        name="Journal 压缩包导入",
        version="1.0.0",
        api_version=PLUGIN_API_VERSION,
        capabilities=(CAPABILITY,),
        config_schema={
            "type": "object",
            "properties": {
                CONFIG_MAX_BYTES: {
                    "type": "integer",
                    "minimum": 1,
                    "description": (
                        "一份归档允许解压出的最大字节数；超过就整份拒绝并报出实际字节数与上限。"
                        f"不填时用默认值 {DEFAULT_MAX_DECOMPRESSED_BYTES}（512 MiB）"
                    ),
                }
            },
            "additionalProperties": False,
        },
    )

    def detect(self, source: ImportSource) -> bool:
        """只读前 512 字节判断格式，不解析内容、不写数据。"""
        return detect(Path(source.path))

    def import_source(self, source: ImportSource, context: PluginContext) -> ImportResult:
        path = Path(source.path)
        if not self.detect(source):
            raise PluginInputError(f"不认这个来源：{path.name}；{SOURCE_HINT}")
        data = context.data
        if not isinstance(data, CollectionImportAccess):
            raise PluginInputError(
                "导入需要 context.data 提供同步的 get_collection 与 import_collection"
                f"（存储层的整份采集写入入口），实际拿到的是 {type(data).__name__}"
            )

        parsed = parse_journal_archive(
            path, max_decompressed_bytes=_max_decompressed_bytes(context.config)
        )
        bundle = build_bundle(parsed, source_sha256=archive_digest(path))
        outcome = data.import_collection(
            bundle.collection,
            bundle.entities,
            bundle.records,
            source_sha256=bundle.source_sha256,
            resources=bundle.resources,
        )
        _reject_coroutine(outcome)
        return ImportResult(records_written=outcome.records_written)


def _reject_coroutine(result: Any) -> None:
    """数据入口返回协程时明确失败，而不是让调用方拿到一个没导入任何东西的协程。

    契约把插件入口定成同步，数据入口因此也必须是同步的。这里只判一次，所以不抽公共函数；
    1.3 的 ``_require_synchronous`` 是同一判断，等 PL 定了公共位置再合并。
    """
    if not inspect.isawaitable(result):
        return
    close = getattr(result, "close", None)
    if callable(close):
        close()
    raise PluginInputError(
        "数据入口 import_collection 返回了协程，导入需要同步入口；"
        "Core 应把存储层包成同步的 get_collection / import_collection 后写入 context.data"
    )


def _max_decompressed_bytes(config: Mapping[str, Any]) -> int:
    """从插件配置取体积上限，没有配置就用默认值。

    Core 会先用 ``config_schema`` 校验一遍；这里再查一次类型与正负，因为调用方也可以直接构造
    ``PluginContext``（测试与将来的 Worker 接线），那条路径不过 Core 的校验。
    """
    value = config.get(CONFIG_MAX_BYTES, DEFAULT_MAX_DECOMPRESSED_BYTES)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PluginInputError(f"插件配置 {CONFIG_MAX_BYTES} 必须是正整数，实际是 {value!r}")
    return value


__all__ = ["JournalArchiveImporter"]
