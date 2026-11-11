"""``.bootrec`` 的导入编排与导入插件（任务 1.3）。

解析与校验在 ``importers.bootrec``，写库走任务 1.1 的 ``DiagnosticStore.import_collection``：

- 登记采集、写实体、写记录、把状态推进到 ``imported`` 在同一个事务里完成，任何一步失败整批
  回滚并把采集标成 ``failed``。本模块不自己开事务、不逐条写记录，因此 Worker 里重试不会留下
  半份数据。
- 两级幂等（``docs/data-model.md`` 第 8 节）：第一级是容器摘要 ``source_sha256``（同一份文件再
  导入一次直接跳过），第二级是记录唯一键 ``(collection_id, record_id)``（同一采集的补充数据按
  键去重）。导入方每次都把摘要传给存储层，两级因此都成立。
- ``collection_id`` 被另一台机器或另一次启动复用时整份拒绝，这是 ``docs/data-model.md`` 第 8 节
  交给导入方的判断：存储层只会在完整性检查里报不一致，不会拒绝写入。

整条导入路径都是同步的：``docs/plugin-contracts.md`` 第 3.3 节把 ``import_source`` 声明为同步
方法，返回 ``ImportResult``，插件抛出的非 ``PluginError`` 异常由 Core 在 ``invoke_plugin`` 处包成
``PluginExecutionError``。因此数据入口也必须是同步的——契约第 5 节的 ``PluginDataAccess`` 本来就是
同步接口（``append_records`` 返回 ``int``）。异步存储层到同步入口的包装属于 Core 的数据入口实现，
不在本模块里做；本模块只声明需要的入口形状（``CollectionImportAccess``）并检查它真的是同步的。

Journal 条目与 ``artifacts/`` 的处置：本模块读出并统计它们，但不落库。1.1 的五张表里没有
Journal 表和原始文件表，记录（含 ``artifact`` 记录）才是写入对象；``artifacts/`` 里的字节随
``.bootrec`` 文件本身保存，与 ``docs/data-model.md`` 第 6 节「原始文件只在容器内有效」一致。
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from system_analyzer.domain.bootrec import CollectionRef, ResourceAvailability
from system_analyzer.importers.bootrec import is_bootrec, prepare_bootrec
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


@runtime_checkable
class CollectionImportAccess(Protocol):
    """导入插件需要的数据入口，全部是同步方法。

    ``docs/plugin-contracts.md`` 第 5 节的 ``PluginDataAccess`` 只有逐条追加与只读查询，而导入是
    「一整份采集一次写入」：登记采集、写实体、写记录、推进导入状态必须在一个事务里完成，
    否则失败会留下半份数据。这件事在任务 1.1 的 ``DiagnosticStore`` 上已经实现
    （``get_collection`` 与 ``import_collection``），Core 需要把它包成同步入口后交给
    ``context.data``：插件不 import 存储层——插件实现目录里出现 ``system_analyzer.storage`` 的
    import 会让 ``test_plugin_implementations_never_import_storage`` 失败。
    """

    def get_collection(self, collection_id: str) -> Any: ...

    def import_collection(
        self,
        collection: CollectionRef,
        entities: Sequence[BaseModel],
        records: Sequence[Mapping[str, Any]],
        *,
        source_sha256: str | None = None,
        resources: ResourceAvailability | None = None,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class BootrecImportOutcome:
    """一次导入的结果，供任务结果与日志使用。

    ``journal_entries`` 与 ``artifacts`` 是这份包里的条目数，只说明「读到了什么」；
    ``records_written`` / ``records_skipped`` 才是落库的条数。
    """

    collection_id: str
    status: str
    records_written: int
    records_skipped: int
    entities_written: int
    entities_skipped: int
    journal_entries: int
    artifacts: tuple[str, ...]
    digest: str
    warnings: tuple[str, ...]
    skipped_reason: str | None = None


def import_bootrec(data: CollectionImportAccess, path: str | Path) -> BootrecImportOutcome:
    """把一份 ``.bootrec`` 导进采集库。

    顺序写死：校验并解析 → 检查 ``collection_id`` 有没有被别的机器或启动用过 → 交给存储层一次
    写入。前两步都不写库，所以不合法或复用的包不会在库里留下任何痕迹。
    """
    prepared = prepare_bootrec(path)
    warnings = [issue.format() for issue in prepared.warnings]

    existing = _require_synchronous(data.get_collection, prepared.collection.collection_id)
    conflict = collection_reuse_problem(existing, prepared.collection)
    if conflict is not None:
        raise PluginInputError(conflict)
    if existing is not None and existing.source_sha256 not in (None, prepared.digest):
        # 同一台机器、同一次启动下的另一份文件：按 data-model 第 8 节当作补充数据追加，
        # 不拒绝，但要在任务结果里说明采集号被重复使用过。
        warnings.append(
            f"collection_id {prepared.collection.collection_id} 已被另一份文件用过"
            f"（{existing.source_sha256}）；本次按记录唯一键去重后写入，重复记录会被跳过"
        )

    outcome = _require_synchronous(
        data.import_collection,
        prepared.collection,
        prepared.entities,
        prepared.record_lines,
        source_sha256=prepared.digest,
        resources=prepared.resources,
    )
    return BootrecImportOutcome(
        collection_id=outcome.collection_id,
        status=outcome.status.value,
        records_written=outcome.records_written,
        records_skipped=outcome.records_skipped,
        entities_written=outcome.entities_written,
        entities_skipped=outcome.entities_skipped,
        journal_entries=len(prepared.journal_entries),
        artifacts=prepared.artifacts,
        digest=prepared.digest,
        warnings=tuple(warnings),
        skipped_reason=outcome.skipped_reason,
    )


def collection_reuse_problem(existing: Any, collection: CollectionRef) -> str | None:
    """``collection_id`` 是否已经被另一台机器或另一次启动用过。

    ``existing`` 是存储层的采集状态（``get_collection`` 的返回值），``None`` 表示库里没有这个
    采集号。复用判定只看机器与启动两个作用域：同一个采集号落到别的机器或别的启动上，说明导入的
    是另一份数据，收下会让采集与它的记录、启动互相对不上，只能整份拒绝。
    """
    if existing is None:
        return None
    stored = existing.collection
    if stored.machine_id == collection.machine_id and stored.boot_id == collection.boot_id:
        return None
    return (
        f"collection_id {collection.collection_id} 已经被 {stored.machine_id} / {stored.boot_id} "
        f"用过，这份文件声称它属于 {collection.machine_id} / {collection.boot_id}；"
        "同一个 collection_id 不能分给两台机器或两次启动，整份拒绝"
    )


class BootrecImporter(PluginBase):
    """``.bootrec``（bootrec v1）导入插件。

    ``import_source`` 是同步方法，返回 ``ImportResult``，与 ``docs/plugin-contracts.md``
    第 3.3 节一致：经 ``PluginRegistry.invoke`` 直接拿到结果，插件抛出的非 ``PluginError``
    异常也在那层边界里被包成 ``PluginExecutionError``。
    """

    spec = PluginSpec(
        id="bootrec.importer",
        kind=PluginKind.IMPORTER,
        name="bootrec v1 导入",
        version="1.0.0",
        api_version=PLUGIN_API_VERSION,
        capabilities=("bootrec.v1",),
    )

    def detect(self, source: ImportSource) -> bool:
        return is_bootrec(source)

    def import_source(self, source: ImportSource, context: PluginContext) -> ImportResult:
        if not self.detect(source):
            raise PluginInputError(f"bootrec 导入插件不认这个来源: {source.path.name}")
        if not isinstance(context.data, CollectionImportAccess):
            raise PluginInputError(
                "导入需要 context.data 提供同步的 get_collection 与 import_collection"
                f"（存储层的整份采集写入入口），实际拿到的是 {type(context.data).__name__}"
            )
        outcome = import_bootrec(context.data, source.path)
        return ImportResult(records_written=outcome.records_written)


def _require_synchronous(call: Any, *args: Any, **kwargs: Any) -> Any:
    """调一次数据入口，并要求它真的返回结果而不是协程。

    契约把插件的入口方法定成同步的，因此数据入口也必须是同步的。若 Core 直接把异步存储层
    （方法返回协程）交给插件，调用会「成功」返回一个协程对象，导入其实没有发生——这种错配在
    结果里看不出来。这里把它变成一条可读的失败原因。
    """
    result = call(*args, **kwargs)
    if inspect.isawaitable(result):
        close = getattr(result, "close", None)
        if callable(close):
            close()
        raise PluginInputError(
            f"数据入口 {getattr(call, '__name__', call)!r} 返回了协程，导入需要同步入口；"
            "Core 应把存储层包成同步的 get_collection / import_collection 后写入 context.data"
            "（见 docs/bootrec-import.md 第 6 节）"
        )
    return result
