"""Systemd 服务生命周期分析插件（任务 2.1）。

推导规则与数据形状在 ``analyzers/systemd_lifecycle.py``；本模块只做插件该做的事：声明 manifest、
经同步数据入口读 Journal 事件、把重建出的区间与结论写回。

``analyze`` 是**同步**方法，与 ``docs/plugin-contracts.md`` 第 3.4 节一致：经
``PluginRegistry.invoke`` 直接拿到 ``AnalysisResult``，插件抛出的非 ``PluginError`` 异常也在那层
边界里被包成 ``PluginExecutionError``。

数据入口：存储层是协程，插件入口是同步方法，两者之间的桥接由 Core 负责（与 1.3 / 1.4 的导入
插件同一套做法）。本模块只声明它需要的同步入口形状 ``LifecycleDataAccess``：读一个 boot 的记录、
按采集追加记录。拿到的入口不是同步形状时抛可读的 ``PluginInputError``，不在插件里偷开事件循环。
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from typing import Any, Protocol, runtime_checkable

from system_analyzer.analyzers.systemd_lifecycle import (
    PLUGIN_ID,
    LifecycleReconstruction,
    reconstruct,
)
from system_analyzer.domain.bootrec import RecordKind
from system_analyzer.plugins.contracts import (
    PLUGIN_API_VERSION,
    AnalysisRequest,
    AnalysisResult,
    PluginBase,
    PluginContext,
    PluginInputError,
    PluginKind,
    PluginSpec,
)

MACHINE_ID_OPTION = "machine_id"
"""``AnalysisRequest.options`` 里的机器作用域。

``AnalysisRequest`` 只有 ``boot_id``（0.4 定稿，本任务不改契约），而存储层按
``(machine_id, boot_id)`` 取数：同一个 ``boot_id`` 可能出现在两台机器上（虚拟机从快照恢复，
见 ``docs/data-model.md`` 第 5.2 节与 ``docs/time-base.md`` 第 9 节）。所以 Core 必须显式给出机器，
插件不猜。

``options`` 是契约留给调用方的自由项，这里只占用一个键。
"""


@runtime_checkable
class LifecycleDataAccess(Protocol):
    """分析插件需要的数据入口，全部是同步方法。

    插件不 import 存储层：``tests/test_plugin_contracts.py`` 会扫描插件实现目录里的 import，
    出现 ``system_analyzer.storage`` 或 ``system_analyzer.core`` 直接失败。Core 把
    ``DiagnosticStore`` 包成这个形状后放进 ``context.data``：

    - ``list_records_in_boot`` 对应 ``DiagnosticStore.list_records_in_boot``，参数同名同序；
    - ``ensure_entities`` 保证本次分析引用的 service 实体在库中存在（幂等）：真实 Journal 的导入
      只为 ``_SYSTEMD_UNIT`` 建过实体，而生命周期消息的 ``_SYSTEMD_UNIT`` 是 PID 1 的
      ``init.scope``，被分析的 Unit 大多没有实体，不补就只有「实体不存在」一种结局；
    - ``append_records`` 对应 ``DiagnosticStore.append_records``，但只返回写入条数（存储层返回的
      是导入结果对象）。写的是「追加」：数据入口没有 update / delete，原始记录不会被改写。
    """

    def list_records_in_boot(
        self,
        machine_id: str,
        boot_id: str,
        *,
        kinds: Sequence[RecordKind] | None = None,
        limit: int | None = None,
    ) -> Sequence[Any]: ...

    def ensure_entities(self, collection_id: str, entities: Sequence[Any]) -> int:
        """把 ``entities``（``domain.bootrec.GenericEntity``）写进该采集所属启动的实体集合。

        必须幂等：实体已存在时返回 0 且不改写已有定义。存储层现在只有整份采集写入
        （``import_collection``）能写实体，Core 接这个入口时复用它或补一个专用的实体追加方法。
        """

    def append_records(self, collection_id: str, records: Sequence[Mapping[str, Any]]) -> int: ...


class SystemdLifecycleAnalyzer(PluginBase):
    """从 Journal 事件重建 systemd Unit 生命周期。

    无状态：配置与数据入口每次调用时由 ``PluginContext`` 传入，插件对象在模块加载时构造一次即可。
    """

    spec = PluginSpec(
        id=PLUGIN_ID,
        kind=PluginKind.ANALYZER,
        name="Systemd 服务生命周期分析",
        version="1.0.0",
        api_version=PLUGIN_API_VERSION,
        capabilities=("systemd.lifecycle",),
    )

    def analyze(self, request: AnalysisRequest, context: PluginContext) -> AnalysisResult:
        data = context.data
        if not isinstance(data, LifecycleDataAccess):
            raise PluginInputError(
                "分析需要 context.data 提供同步的 list_records_in_boot、ensure_entities 与"
                " append_records（存储层的只读查询、实体补齐与追加写入入口），"
                f"实际拿到的是 {type(data).__name__}"
            )
        machine_id = _machine_id(request.options)
        boot_id = request.boot_id.strip()
        if not boot_id:
            raise PluginInputError(
                "analyze 需要明确的 boot_id：生命周期区间按一次启动重建，"
                "空 boot_id（表示「全部分析」）在这里无法定位到某一次启动的时间轴"
            )

        records = _require_synchronous(
            data.list_records_in_boot,
            machine_id,
            boot_id,
            kinds=(RecordKind.EVENT,),
        )
        if request.record_ids:
            wanted = set(request.record_ids)
            records = [record for record in records if _record_id(record) in wanted]

        reconstruction = reconstruct(records)
        if not reconstruction.segments and not reconstruction.unattributed:
            # 没有任何可用的 Journal 生命周期证据：这是「确实没有数据」，不是失败
            return AnalysisResult(annotations_written=0)
        return AnalysisResult(annotations_written=_write(data, reconstruction))


def _write(data: LifecycleDataAccess, reconstruction: LifecycleReconstruction) -> int:
    """补齐实体，然后把区间与结论按采集分段写回，返回真正写进去的结论条数。

    顺序写死：先补实体（记录引用它们），再写区间、写结论。结论的 ``evidence_refs`` 指向原始
    Journal 记录，不指向本次产出的区间，所以区间与结论之间没有先后依赖；分开写只是为了能报出
    「写了几条结论」这个准确的数字（存储层按 ``(collection_id, record_id)`` 幂等，重复分析时
    结论条数是 0，不是重复计数）。
    """
    entities = reconstruction.service_entities()
    if entities:
        # 一条 Unit 都没重建出来时没有实体可补，也不该为了一次纯汇总去动实体表
        for collection_id in _collection_ids(reconstruction):
            _count(
                _require_synchronous(data.ensure_entities, collection_id, entities),
                "ensure_entities",
            )
    for collection_id, batch in _by_collection(reconstruction.span_records()).items():
        _require_synchronous(data.append_records, collection_id, batch)
    written = 0
    for collection_id, batch in _by_collection(reconstruction.annotation_records()).items():
        count = _require_synchronous(data.append_records, collection_id, batch)
        written += _count(count, "append_records")
    return written


def _collection_ids(reconstruction: LifecycleReconstruction) -> tuple[str, ...]:
    return tuple(sorted({record["collection_id"] for record in reconstruction.records()}))


def _by_collection(records: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
    """按 ``collection_id`` 分组：记录只能追加到它自己所属的采集。"""
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        grouped.setdefault(str(record["collection_id"]), []).append(record)
    return grouped


def _count(result: Any, entry: str) -> int:
    if isinstance(result, bool) or not isinstance(result, int):
        raise PluginInputError(
            f"数据入口 {entry} 必须返回写入条数（int），实际返回 {type(result).__name__}"
        )
    return result


def _machine_id(options: Mapping[str, Any]) -> str:
    value = options.get(MACHINE_ID_OPTION)
    if not isinstance(value, str) or not value.strip():
        raise PluginInputError(
            f"analyze 需要 request.options[{MACHINE_ID_OPTION!r}] 给出机器作用域："
            f"存储层按 (machine_id, boot_id) 取数，实际收到 {value!r}"
        )
    return value.strip()


def _record_id(record: Any) -> str:
    """取记录的 ``record_id``：数据入口可能给统一记录模型，也可能给原始字典。"""
    if isinstance(record, Mapping):
        return str(record.get("record_id", ""))
    return str(getattr(record, "record_id", ""))


def _require_synchronous(call: Any, *args: Any, **kwargs: Any) -> Any:
    """调一次数据入口，并要求它真的返回结果而不是协程。

    契约把插件入口方法定成同步的，因此数据入口也必须是同步的。若 Core 把异步存储层（方法返回
    协程）直接交给插件，调用会「成功」返回一个协程对象，分析其实没有发生——这种错配在结果里
    看不出来。这里把它变成一条可读的失败原因。
    """
    result = call(*args, **kwargs)
    if inspect.isawaitable(result):
        close = getattr(result, "close", None)
        if callable(close):
            close()
        raise PluginInputError(
            f"数据入口 {getattr(call, '__name__', call)!r} 返回了协程，分析需要同步入口；"
            "Core 应把存储层的 list_records_in_boot / append_records 包成同步入口后写入"
            " context.data"
        )
    return result


__all__ = ["MACHINE_ID_OPTION", "LifecycleDataAccess", "SystemdLifecycleAnalyzer"]
