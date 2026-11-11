"""Journal 压缩包导入：把一份上传的 Journal 归档解析成统一 Event 记录与实体。

本模块是任务 1.4 的交付物，只做「读文件 → 归一化」，不碰数据库；写入由
``journal_archive_importer.JournalArchiveImporter`` 通过 ``context.data`` 完成。

三种输入都认：

- ``journalctl -o export`` 的文本（``字段=值`` 一行一个，事件之间空行分隔）；
- Journal 原生文件（journald 的 ``LPKSHHRH`` 二进制格式，``/var/log/journal`` 里就是它）；
- 上面两者的压缩包：gzip / xz / bzip2 单文件，或者 tar（可再压缩）里放多个 Journal 文件。

zstd 压缩的外层容器与 zstd / lz4 压缩的字段无法解码：仓库没有这两者的解码依赖，任务范围
又不允许加依赖。外层 zstd 容器整份拒绝并说明原因；字段级 zstd / lz4 则跳过该字段、把次数
记进 ``ParsedJournal.skipped_fields``，并写到每条记录的 ``payload.attributes.skipped_fields``，
绝不悄悄当作「当时就没有这个字段」。

时间换算全部走 ``domain.time_base.journal_event_position``（任务 0.3 的交付）：本模块只负责把
两种输入都变成「Journal 原始字段字典」，微秒换纳秒与 clock_domain 的判定只有那一处实现。

多启动：一份归档里出现两个以上 ``_BOOT_ID`` 时整份拒绝，不做拆分。理由与 ``docs/bootrec-v1.md``
第 5 节一致：一条记录必须属于唯一一次启动，硬拆会把一次上传变成多次采集、并让 ``collection_id``
的幂等含义失效。要导入多启动，请按 boot 分别导出、分别上传。
"""

from __future__ import annotations

import bz2
import hashlib
import itertools
import lzma
import re
import struct
import tarfile
import zlib
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from system_analyzer.domain.bootrec import (
    BootEntity,
    CollectionMode,
    CollectionRef,
    EntityType,
    GenericEntity,
    MachineEntity,
    RecordKind,
    ResourceAvailability,
    Severity,
    compute_record_id,
)
from system_analyzer.domain.time_base import (
    BOOT_ID_FIELD,
    MONOTONIC_FIELD,
    REALTIME_FIELD,
    JournalEventPosition,
    journal_event_position,
    normalize_boot_id,
)
from system_analyzer.plugins.contracts import PluginInputError
from system_analyzer.tools.journal_time import EXPORT_BINARY_LENGTH_SIZE

PLUGIN_ID = "journal.archive"
"""插件 id。稳定标识，不随版本改名。"""

CAPABILITY = "journal.archive"
"""能力名：本插件处理 Journal 归档。"""

STREAM_ID = "journal"
"""全部事件记录共用的逻辑流名（data-model 第 3 节）。"""

MACHINE_ID_PREFIX = "machine:"
"""``machine_id`` 的生成规则：``machine:<hostname>``（data-model 第 5.1 节）。"""

COLLECTION_ID_PREFIX = "journal-import:"
"""``collection_id`` 的生成规则，见 ``build_bundle``。"""

DEFAULT_PRIORITY = 6
"""syslog 里 info 的级别。导出缺 ``PRIORITY`` 时用它（bootrec-v1 第 5 节的缺失语义）。"""

NO_RESOURCE_REASON = "Journal 归档只含日志，没有 CPU、内存、磁盘采样"
"""``resources.available = false`` 的原因文案（data-model 第 7 节第一行）。"""

PRIORITY_ERROR = 3
"""``PRIORITY`` 不大于它算错误级别（syslog：0 emerg … 3 err）。"""

PRIORITY_WARNING = 4
"""``PRIORITY`` 等于它算警告级别。"""

EVENT_TYPE = "journal.entry"
"""事件类型。本导入只搬运日志原文，不从日志推导语义事件（那是 2.x 分析插件的事）。"""

JOURNAL_SIGNATURE = b"LPKSHHRH"
"""Journal 原生文件的文件头签名。"""

HEADER_FIELD_LIMIT = 152
"""读 ``incompatible_flags`` 与 ``n_objects`` 至少要这么长的文件头。"""

MIN_HEADER_SIZE = 96
"""文件头里 ``header_size`` / ``arena_size`` 两个字段所在的位置决定的下限。"""

HEADER_OBJECT_COUNT_OFFSET = 144
"""``n_objects`` 在原生文件头里的偏移（journal-def.h，v187 起就有）。"""

EXPORT_BINARY_FIELD_SIZE = EXPORT_BINARY_LENGTH_SIZE
"""``-o export`` 二进制字段的长度头字节数，与 ``tools/journal_time.py`` 同一处真值。"""

OBJECT_HEADER_SIZE = 16
"""原生文件里每个对象的固定头：type(1) + flags(1) + reserved(6) + size(8)。"""

OBJECT_DATA = 1
OBJECT_ENTRY = 3

OBJECT_COMPRESSED_MASK = 0b111
"""对象头上表示「内容被压缩」的三个位（XZ / LZ4 / ZSTD）。"""

OBJECT_COMPRESSED_XZ = 1 << 0
OBJECT_COMPRESSED_LZ4 = 1 << 1
OBJECT_COMPRESSED_ZSTD = 1 << 2

HEADER_INCOMPATIBLE_COMPRESSED_XZ = 1 << 0
HEADER_INCOMPATIBLE_COMPRESSED_LZ4 = 1 << 1
HEADER_INCOMPATIBLE_KEYED_HASH = 1 << 2
HEADER_INCOMPATIBLE_COMPRESSED_ZSTD = 1 << 3
HEADER_INCOMPATIBLE_COMPACT = 1 << 4

SUPPORTED_INCOMPATIBLE = (
    HEADER_INCOMPATIBLE_COMPRESSED_XZ
    | HEADER_INCOMPATIBLE_COMPRESSED_LZ4
    | HEADER_INCOMPATIBLE_KEYED_HASH
    | HEADER_INCOMPATIBLE_COMPRESSED_ZSTD
    | HEADER_INCOMPATIBLE_COMPACT
)

DATA_OBJECT_HEAD_SIZE = 64
"""普通 Journal 文件里 DATA 对象在字段内容之前的固定长度（journal-def.h ``DataObject``）。"""

DATA_OBJECT_HEAD_SIZE_COMPACT = 72
"""COMPACT 文件在 DATA 对象头部多两个 32 位字段（journal-def.h，v254 起的写法）。"""

ENTRY_OBJECT_HEAD_SIZE = 64
"""ENTRY 对象在字段偏移列表之前的固定长度。"""

ENTRY_REALTIME_OFFSET = 24
"""ENTRY 对象里墙钟读数（微秒）的偏移。"""

ENTRY_MONOTONIC_OFFSET = 32
"""ENTRY 对象里单调时钟读数（微秒）的偏移。"""

ENTRY_BOOT_ID_OFFSET = 40
"""ENTRY 对象里 ``boot_id``（16 字节原始 id128）的偏移。"""

_PROBE_SIZE = 512
"""探测时最多读这么多字节：够看到魔数与 tar 头即可。"""

CHUNK_SIZE = 64 * 1024
"""读文件与解压的块大小。整份归档都按这个粒度流动，内存不随未压缩体积线性增长。"""

DEFAULT_MAX_DECOMPRESSED_BYTES = 512 * 1024 * 1024
"""一份归档允许解压出的最大字节数，默认 512 MiB。

Journal 归档压缩比可以很高（仓库里的样例 `boot.journal.gz` 只有 10 KB，解开是 8 MiB 的预分配
文件），不设上限时一份精心构造的上传就能把 Worker 撑爆。上限按「解压后」计，因为压缩前的体积
说明不了内存开销。插件配置 `max_decompressed_bytes` 可以按部署需要调整。
"""

CONFIG_MAX_BYTES = "max_decompressed_bytes"
"""插件配置里调整上面那个上限的键。"""

ENTRY_MEMORY_ESTIMATE_BYTES = 6144
"""每条事件在内存里大约占多少字节，用来把「条数」折算进同一个体积预算。

一条 Journal 事件解析后是一份字段字典，``build_bundle`` 还会为它再建一份记录字典（含嵌套的
payload 与原始字段副本）。这两个数字是实测的：样例 `boot.export` 上分别为 2176 B 与 3788 B，
合计约 6 KiB，所以这里取 6 KiB。它只用来给条数一个上限——预算只看解压字节的话，一份体积没超限、
却拆成几百万条事件的导出文本仍然会把内存吃光。
"""

_FIELD_NAME = re.compile(rb"[A-Z_][A-Z0-9_]*")
"""Journal 字段名：大写字母或下划线开头，只含大写字母、数字与下划线。"""


# ----------------------------------------------------------------------
# 写入侧接口与数据形状
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JournalBundle:
    """一次导入要写入的全部内容：一条采集 + 实体 + 事件记录。

    形状对齐 ``storage.store.DiagnosticStore.import_collection`` 的入参：让「采集、实体、记录
    在同一个事务里写入」这条性质由存储层保证，而不是由插件自己拼事务（插件也拿不到事务）。
    """

    collection: CollectionRef
    entities: tuple[BaseModel, ...]
    records: tuple[dict[str, Any], ...]
    source_sha256: str
    resources: ResourceAvailability


@runtime_checkable
class CollectionImportAccess(Protocol):
    """导入需要的数据入口：整份采集一次写入。

    ``docs/plugin-contracts.md`` 第 5 节的 ``PluginDataAccess`` 只有逐条追加与只读查询，装不下
    导入：``docs/data-model.md`` 第 3 节要求记录的 ``entity`` 能在同一份采集里解析到，而 Machine
    与 Boot 两行是写入的前置条件（``storage/validation.py`` 的 ``entity_problems``、
    ``_reference_problems``）；三种行还必须落在同一个事务里，失败才不会留下半份数据。这件事在
    任务 1.1 的 ``DiagnosticStore.import_collection`` 上已经做好，插件通过 ``context.data`` 调它，
    自己不会 import 存储层。

    1.3 的 ``importers/bootrec_importer.py`` 里有同形的同名协议；两个任务各自声明了一份，
    合并后应当提到公共位置（见 ``docs/journal-archive-import.md`` 的偏差清单）。
    """

    async def import_collection(
        self,
        collection: CollectionRef,
        entities: Sequence[BaseModel],
        records: Sequence[Mapping[str, Any]],
        *,
        source_sha256: str | None = None,
        resources: ResourceAvailability | None = None,
    ) -> Any:
        """写入一整份采集，返回存储层的导入结果（含写入与跳过的条数）。"""


# ----------------------------------------------------------------------
# 解析结果
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsedJournal:
    """一份归档解析后的中间结果：只有数据，没有存储细节。"""

    entries: tuple[Mapping[str, str], ...]
    """每条 Journal 事件一份原始字段字典，顺序与归档里的顺序一致。"""

    streams: tuple[str, ...]
    """解析到的事件流名字（单文件就是文件名，tar 里是成员名），用于日志与排查。"""

    ignored_members: tuple[str, ...] = ()
    """tar 里既不是目录也不是 Journal 数据的成员名，导入时忽略。"""

    skipped_fields: int = 0
    """因为压缩算法没有解码依赖（zstd / lz4）而跳过的字段个数。"""


@dataclass(frozen=True, slots=True)
class JournalArchiveSummary:
    """``JournalBundle`` 的只读摘要，给日志、测试与导入结果用。"""

    machine_id: str
    boot_id: str
    collection_id: str
    hostname: str
    kernel_version: str | None
    entry_count: int
    service_units: tuple[str, ...]


# ----------------------------------------------------------------------
# 探测
# ----------------------------------------------------------------------


def detect(source_path: Path) -> bool:
    """能否处理这个文件。只读前 512 字节，不解析内容、不写数据（契约第 3.3 节的要求）。"""
    head = _read_head(source_path)
    if not head:
        return False
    payload = _peek(head)
    if not payload:
        return False
    return (
        payload.startswith(JOURNAL_SIGNATURE)
        or _looks_like_tar(payload)
        or _looks_like_export_text(payload)
    )


def _read_head(path: Path) -> bytes:
    try:
        with path.open("rb") as handle:
            return handle.read(_PROBE_SIZE)
    except OSError as error:
        raise PluginInputError(f"读不到归档文件 {path}：{error}") from error


def _peek(head: bytes) -> bytes:
    """把压缩过的开头解开一点点，用来判定格式。解不出内容时返回空字节。

    用增量解压器而不是 ``gzip.decompress``：后者要求整段数据完整，而这里手上只有 512 字节的
    开头，属于被截断的流。
    """
    compression = _compression_of(head)
    if compression is None:
        return head
    try:
        if compression == "gzip":
            return zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(head)
        if compression == "xz":
            return lzma.LZMADecompressor(format=lzma.FORMAT_XZ).decompress(head)
        if compression == "bzip2":
            return bz2.BZ2Decompressor().decompress(head)
    except (OSError, EOFError, ValueError, zlib.error, lzma.LZMAError):
        return b""
    # zstd 外层容器：没有解压依赖，判定靠扩展名，见下面 _looks_like_zstd_archive
    return b""


def _compression_of(data: bytes) -> str | None:
    if data.startswith(b"\x1f\x8b"):
        return "gzip"
    if data.startswith(b"\xfd7zXZ\x00"):
        return "xz"
    if data.startswith(b"BZh"):
        return "bzip2"
    if data.startswith(b"\x28\xb5\x2f\xfd"):
        return "zstd"
    return None


def _looks_like_tar(data: bytes) -> bool:
    return len(data) >= 262 and data[257:262] == b"ustar"


def _looks_like_export_text(data: bytes) -> bool:
    """``-o export`` 的每条事件都是 ``字段=值``，首行的字段名与等号足以判定。"""
    first_line = data.split(b"\n", 1)[0].rstrip(b"\r")
    name, separator, _ = first_line.partition(b"=")
    return bool(separator) and bool(_FIELD_NAME.fullmatch(name))


# ----------------------------------------------------------------------
# 解析
# ----------------------------------------------------------------------


def parse_journal_archive(
    path: str | Path,
    *,
    max_decompressed_bytes: int = DEFAULT_MAX_DECOMPRESSED_BYTES,
) -> ParsedJournal:
    """解析一份归档文件，返回全部 Journal 事件。任何结构问题都抛 ``PluginInputError``。

    体积受 ``max_decompressed_bytes`` 约束：文件和 tar 成员都按 ``CHUNK_SIZE`` 分块读入、分块
    解压，累计一超过上限就立刻中止并报出实际字节数与上限。tar 的成员逐个解析完就释放，不会把
    所有成员同时留在内存里。
    """
    if isinstance(max_decompressed_bytes, bool) or not isinstance(max_decompressed_bytes, int):
        raise PluginInputError(
            f"max_decompressed_bytes 必须是整数，实际是 {max_decompressed_bytes!r}"
        )
    if max_decompressed_bytes <= 0:
        raise PluginInputError(
            f"max_decompressed_bytes 必须是正整数，实际是 {max_decompressed_bytes}"
        )

    archive = Path(path)
    head = _read_head(archive)
    if not head:
        raise PluginInputError(f"归档文件 {archive.name} 是空的")

    budget = _ByteBudget(max_decompressed_bytes)
    payload = _materialise(
        _iter_file_chunks(archive),
        _compression_of(head),
        name=archive.name,
        budget=budget,
        what="整份归档解压",
    )
    if not payload:
        raise PluginInputError(f"归档文件 {archive.name} 解压后是空的")

    entries: list[Mapping[str, str]] = []
    names: list[str] = []
    ignored: list[str] = []
    skipped = 0
    for stream_name, blob in _iter_streams(payload, archive.name, budget=budget, ignored=ignored):
        parsed, skipped_fields = _parse_stream(stream_name, blob)
        if not parsed:
            raise PluginInputError(f"{stream_name} 里没有 Journal 条目")
        budget.take(
            len(parsed) * ENTRY_MEMORY_ESTIMATE_BYTES,
            name=stream_name,
            what="解析出的事件记录",
        )
        entries.extend(parsed)
        names.append(stream_name)
        skipped += skipped_fields
        # 解析完即丢弃这份流，下一份读入时它已经不在内存里
        del blob

    if not names:
        raise PluginInputError(
            f"{archive.name} 里没有 Journal 文件；打包时请只放 .journal 或导出文本"
        )
    return ParsedJournal(
        entries=tuple(entries),
        streams=tuple(names),
        ignored_members=tuple(ignored),
        skipped_fields=skipped,
    )


@dataclass
class _ByteBudget:
    """一份归档允许解压出的字节数，以及已经用掉多少。"""

    limit: int
    used: int = 0

    def take(self, count: int, *, name: str, what: str) -> None:
        self.used += count
        if self.used > self.limit:
            raise PluginInputError(
                f"{name} 超过上限：已占用 {self.used} 字节，上限 {self.limit} 字节"
                f"（{what}）。请调大插件配置 {CONFIG_MAX_BYTES}，"
                "或先按时间范围拆分导出再打包"
            )


def _iter_file_chunks(path: Path) -> Iterator[bytes]:
    """按 ``CHUNK_SIZE`` 读文件，不把整份文件读进内存。"""
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(CHUNK_SIZE):
                yield chunk
    except OSError as error:
        raise PluginInputError(f"读不到归档文件 {path}：{error}") from error


def _decompress_chunks(
    chunks: Iterable[bytes], compression: str | None, *, name: str
) -> Iterator[bytes]:
    """分块解压。``compression`` 为空表示这层没有压缩。

    两个边界都在这里守住：

    - **流必须走完。** 循环结束后没读到结束标记就报错。不查这一条的话，「上传被截断」会变成
      「少了几条日志但导入成功」，最后一条记录里还留着被切断的半句话。
    - **成员可以首尾相接。** ``cat a.gz b.gz`` 与 ``xz`` 的多流都是合法的，一个成员结束时把
      ``unused_data`` 当作下一个成员继续解，不丢后续数据。
    """
    if compression is None:
        yield from chunks
        return

    decompressor = _decompressor(compression, name)
    member_finished = False
    for chunk in chunks:
        pending = chunk
        while pending:
            if member_finished:
                decompressor = _decompressor(compression, name)
                member_finished = False
            try:
                produced = decompressor.decompress(pending)
            except (OSError, EOFError, ValueError, zlib.error, lzma.LZMAError) as error:
                raise PluginInputError(
                    f"{name} 是 {compression} 压缩，但解压失败：{error}"
                ) from error
            if produced:
                yield produced
            if decompressor.eof:
                member_finished = True
                # 成员结束后剩下的字节属于下一个成员，接着解，不能丢
                pending = decompressor.unused_data
            else:
                pending = b""
    if not member_finished:
        raise PluginInputError(
            f"{name} 的 {compression} 压缩流不完整：没有读到结束标记就结束了，"
            "上传可能被截断，或文件被改动过"
        )


def _decompressor(compression: str, name: str):
    if compression == "gzip":
        return zlib.decompressobj(16 + zlib.MAX_WBITS)
    if compression == "xz":
        return lzma.LZMADecompressor(format=lzma.FORMAT_XZ)
    if compression == "bzip2":
        return bz2.BZ2Decompressor()
    raise PluginInputError(
        f"{name} 是 zstd 压缩。本实现没有 zstd 解码依赖（本任务不允许新增依赖）："
        "请上传 gzip / xz / bzip2 压缩包，或先用 journalctl -o export 导出后再打包"
    )


def _materialise(
    chunks: Iterable[bytes],
    compression: str | None,
    *,
    name: str,
    budget: _ByteBudget,
    what: str,
) -> bytes:
    """把一层数据解出来，边解边记账，超限立刻中止。"""
    out = bytearray()
    for produced in _decompress_chunks(chunks, compression, name=name):
        budget.take(len(produced), name=name, what=what)
        out += produced
    return bytes(out)


def _iter_streams(
    payload: bytes,
    name: str,
    *,
    budget: _ByteBudget,
    ignored: list[str],
) -> Iterator[tuple[str, bytes]]:
    """逐个给出待解析的事件流：单文件就是它自己，tar 则是里面的成员。

    tar 成员在这里逐个materialise；调用方解析完就把内容丢掉，因此同一时刻只有一份成员内容在内存里。
    """
    if not _looks_like_tar(payload):
        yield name, payload
        return

    try:
        with tarfile.open(fileobj=BytesIO(payload), mode="r:") as archive:
            for member in archive.getmembers():
                if member.isdir():
                    continue
                handle = archive.extractfile(member) if member.isfile() else None
                if handle is None:
                    ignored.append(member.name)
                    continue
                chunks = _iter_handle_chunks(handle)
                first = next(chunks, b"")
                if not first:
                    ignored.append(member.name)
                    continue
                blob = _materialise(
                    itertools.chain([first], chunks),
                    _compression_of(first),
                    name=member.name,
                    budget=budget,
                    what="tar 成员解压",
                )
                if _is_journal_stream(blob):
                    yield member.name, blob
                    # 交付后立刻放掉生成器这一侧的引用：下一份 materialise 期间不再同时持有上一份
                    blob = b""
                else:
                    ignored.append(member.name)
    except tarfile.TarError as error:
        raise PluginInputError(f"{name} 看起来是 tar 包，但打不开：{error}") from error


def _iter_handle_chunks(handle) -> Iterator[bytes]:
    while chunk := handle.read(CHUNK_SIZE):
        yield chunk


def _is_journal_stream(blob: bytes) -> bool:
    return blob.startswith(JOURNAL_SIGNATURE) or _looks_like_export_text(blob[:512])


def _parse_stream(name: str, blob: bytes) -> tuple[list[Mapping[str, str]], int]:
    if blob.startswith(JOURNAL_SIGNATURE):
        return parse_native(blob, name=name)
    if _looks_like_export_text(blob[:512]):
        return list(iter_export_events(blob, name=name)), 0
    raise PluginInputError(
        f"{name} 既不是 Journal 原生文件（缺少 {JOURNAL_SIGNATURE.decode()} 文件头），"
        "也不是 journalctl -o export 的文本"
    )


# ----------------------------------------------------------------------
# `journalctl -o export` 文本
# ----------------------------------------------------------------------


def iter_export_events(blob: bytes, *, name: str = "export") -> Iterator[Mapping[str, str]]:
    """逐条读出 ``-o export`` 文本里的事件。

    两种字段写法都认，规则与 ``tools/journal_time.py`` 相同：``字段=值`` 一行一个；二进制字段
    是「字段名单独一行 + 8 字节原生长度 + 原始字节 + 换行」。二进制字段的值可能是任意字节
    （``SYSLOG_RAW`` 就是），本模块整个跳过，不解码、不保留。

    空行分隔事件，而且**每条事件都必须以空行结尾**：``journalctl -o export`` 对每个条目都会多打一个
    空行，真实文件因此以空行结束。末尾没有空行说明这份导出被截断了（``head`` 截取、上传中断、拷贝
    不完整），最后一条事件只剩半句话——整份拒绝，不把半条记录当完整数据写进库里。
    """
    fields: dict[str, str] = {}
    position = 0
    total = len(blob)
    while position < total:
        newline = blob.find(b"\n", position)
        if newline < 0:
            line, position = blob[position:], total
        else:
            line, position = blob[position:newline], newline + 1
        line = line.rstrip(b"\r")
        if not line:
            if fields:
                yield fields
                fields = {}
            continue
        field_name, separator, value = line.partition(b"=")
        if separator:
            fields[_decode(field_name)] = _decode(value)
        else:
            position = _skip_binary_field(blob, position, _decode(field_name), name)
    if fields:
        # 走到这里说明文件在一段字段中间结束了：没有收尾的空行
        raise PluginInputError(
            f"{name} 的导出文本不完整：最后一条事件没有以空行结束，上传可能被截断。"
            "journalctl -o export 的每个条目都以空行结尾，请重新导出或重新上传完整文件"
        )


def _skip_binary_field(blob: bytes, position: int, field_name: str, stream: str) -> int:
    header_end = position + EXPORT_BINARY_FIELD_SIZE
    if header_end > len(blob):
        raise PluginInputError(
            f"{stream} 在字段 {field_name} 处被截断：二进制字段后面没有完整长度头"
        )
    size = int.from_bytes(blob[position:header_end], "little")
    end = header_end + size
    if end > len(blob):
        raise PluginInputError(
            f"{stream} 在字段 {field_name} 处被截断："
            f"声明 {size} 字节，只剩 {len(blob) - header_end} 字节"
        )
    if blob[end : end + 1] == b"\n":
        end += 1
    return end


def _decode(raw: bytes) -> str:
    """字段值按 UTF-8 解码，解不出的字节替换掉——Journal 的值本来就可以不是合法 UTF-8。"""
    return raw.decode("utf-8", "replace")


# ----------------------------------------------------------------------
# Journal 原生文件
# ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _JournalHeader:
    header_size: int
    incompatible_flags: int
    object_count: int

    @property
    def compact(self) -> bool:
        return bool(self.incompatible_flags & HEADER_INCOMPATIBLE_COMPACT)


def _read_header(blob: bytes, name: str) -> _JournalHeader:
    if not blob.startswith(JOURNAL_SIGNATURE):
        raise PluginInputError(f"{name} 不是 Journal 原生文件：文件头签名不是 LPKSHHRH")
    if len(blob) < HEADER_FIELD_LIMIT:
        raise PluginInputError(f"{name} 只有 {len(blob)} 字节，装不下一个 Journal 文件头")
    (incompatible_flags,) = struct.unpack_from("<I", blob, 12)
    unsupported = incompatible_flags & ~SUPPORTED_INCOMPATIBLE
    if unsupported:
        raise PluginInputError(
            f"{name} 的 incompat flags 是 0x{incompatible_flags:x}，其中 0x{unsupported:x} "
            "本实现不认识；拒绝按猜的格式读取"
        )
    (header_size,) = struct.unpack_from("<Q", blob, 88)
    if header_size < MIN_HEADER_SIZE or header_size > len(blob):
        raise PluginInputError(
            f"{name} 的文件头声明 header_size={header_size}，与文件长度 {len(blob)} 对不上"
        )
    (object_count,) = struct.unpack_from("<Q", blob, HEADER_OBJECT_COUNT_OFFSET)
    return _JournalHeader(
        header_size=header_size,
        incompatible_flags=incompatible_flags,
        object_count=object_count,
    )


@dataclass
class _FieldTally:
    """因为压缩算法没有解码依赖而跳过的字段。

    记的是字段数据对象的偏移，不是一个固定值被多条事件共用时重复计数：同一个字段值在 Journal
    里只存一份，多条事件共用，所以「跳过了几个字段」应当按不同的字段值算。
    """

    offsets: set[int] = field(default_factory=set)

    @property
    def skipped(self) -> int:
        return len(self.offsets)


def parse_native(blob: bytes, *, name: str = "journal") -> tuple[list[Mapping[str, str]], int]:
    """解析 Journal 原生文件，同时返回跳过的字段个数。"""
    tally = _FieldTally()
    entries = list(_iter_native(blob, name=name, tally=tally))
    return entries, tally.skipped


def _iter_native(blob: bytes, *, name: str, tally: _FieldTally) -> Iterator[Mapping[str, str]]:
    """逐条读出 Journal 原生文件里的事件。

    按 ``journal-def.h`` 的对象布局顺序遍历：对象之间 8 字节对齐，文件头的 ``n_objects`` 给出
    总数。只读 ENTRY 与 DATA 两类对象，其余（字段哈希表、数据哈希表、条目数组、tag）按长度
    跳过，因此不需要实现哈希表与条目数组的查找逻辑。

    字段级压缩：对象头的 ``flags`` 非 0 表示该字段的内容被压缩过。xz 用标准库解；zstd / lz4
    没有解码依赖，遇到时跳过这一个字段并由调用方计数。
    """
    header = _read_header(blob, name)
    position = header.header_size
    seen = 0
    while seen < header.object_count:
        if position + OBJECT_HEADER_SIZE > len(blob):
            raise PluginInputError(
                f"{name} 在偏移 {position} 处截断：还有 {header.object_count - seen} 个对象没读到"
            )
        object_type = blob[position]
        object_flags = blob[position + 1]
        (object_size,) = struct.unpack_from("<Q", blob, position + 8)
        if object_size < OBJECT_HEADER_SIZE or position + object_size > len(blob):
            raise PluginInputError(
                f"{name} 在偏移 {position} 处的对象长度是 {object_size}，超出文件范围"
            )
        if object_type == OBJECT_ENTRY:
            yield _read_entry(blob, position, header, object_flags, name, tally)
        seen += 1
        position = _align8(position + object_size)


def _read_entry(
    blob: bytes,
    position: int,
    header: _JournalHeader,
    object_flags: int,
    name: str,
    tally: _FieldTally,
) -> Mapping[str, str]:
    if object_flags & OBJECT_COMPRESSED_MASK:
        raise PluginInputError(f"{name} 在偏移 {position} 处的条目对象本身被压缩，无法安全读取")

    entry_size = _object_size(blob, position)
    entry_end = position + entry_size
    fields: dict[str, str] = {}
    items_at = position + ENTRY_OBJECT_HEAD_SIZE
    item_size = 4 if header.compact else 16
    while items_at + item_size <= entry_end:
        if header.compact:
            (offset,) = struct.unpack_from("<I", blob, items_at)
        else:
            offset, _ = struct.unpack_from("<QQ", blob, items_at)
        items_at += item_size
        if offset == 0:
            break
        pair = _read_data_field(blob, offset, header, name, tally)
        if pair is None:
            continue
        field_name, value = pair
        # 多值字段在一条记录里会出现多次；Journal 的导出口径是保留最后一次
        fields[field_name] = value

    # 时间戳与 boot id 不是 DATA 字段，而是 ENTRY 对象自己的固定字段。这里补成与
    # `journalctl -o export` 一致的字段名，让两种输入走同一条换算路径（time_base）。
    fields.setdefault(REALTIME_FIELD, str(_entry_number(blob, position, ENTRY_REALTIME_OFFSET)))
    fields.setdefault(MONOTONIC_FIELD, str(_entry_number(blob, position, ENTRY_MONOTONIC_OFFSET)))
    fields.setdefault(BOOT_ID_FIELD, _boot_id_from_entry(blob, position))
    return fields


def _entry_number(blob: bytes, position: int, offset: int) -> int:
    (value,) = struct.unpack_from("<Q", blob, position + offset)
    return value


def _boot_id_from_entry(blob: bytes, position: int) -> str:
    start = position + ENTRY_BOOT_ID_OFFSET
    return blob[start : start + 16].hex()


def _object_size(blob: bytes, position: int) -> int:
    (size,) = struct.unpack_from("<Q", blob, position + 8)
    return size


def _read_data_field(
    blob: bytes,
    offset: int,
    header: _JournalHeader,
    name: str,
    tally: _FieldTally,
) -> tuple[str, str] | None:
    """读一个 DATA 对象，返回 ``(字段名, 值)``。

    字段被 zstd / lz4 压过时返回 ``None`` 并记一次跳过：没有解码依赖，只能如实报告缺了字段，
    不能悄悄当作这个字段不存在。
    """
    if offset + OBJECT_HEADER_SIZE > len(blob):
        raise PluginInputError(f"{name} 的条目引用了偏移 {offset}，超出文件范围")
    object_type = blob[offset]
    object_flags = blob[offset + 1]
    (object_size,) = struct.unpack_from("<Q", blob, offset + 8)
    if object_type != OBJECT_DATA:
        raise PluginInputError(
            f"{name} 的条目引用了偏移 {offset}：它是对象类型 {object_type}，不是字段数据"
        )
    head = DATA_OBJECT_HEAD_SIZE_COMPACT if header.compact else DATA_OBJECT_HEAD_SIZE
    if object_size < head or offset + object_size > len(blob):
        raise PluginInputError(f"{name} 在偏移 {offset} 处的字段对象长度 {object_size} 不合法")

    payload = blob[offset + head : offset + object_size]
    compression = object_flags & OBJECT_COMPRESSED_MASK
    if compression == OBJECT_COMPRESSED_XZ:
        try:
            payload = lzma.decompress(payload, format=lzma.FORMAT_XZ)
        except lzma.LZMAError as error:
            raise PluginInputError(f"{name} 在偏移 {offset} 处的 xz 字段解不开：{error}") from error
    elif compression in (OBJECT_COMPRESSED_ZSTD, OBJECT_COMPRESSED_LZ4):
        tally.offsets.add(offset)
        return None

    field_name, separator, value = payload.partition(b"=")
    if not separator:
        return None
    return _decode(field_name), _decode(value)


def _align8(value: int) -> int:
    return (value + 7) & ~7


# ----------------------------------------------------------------------
# 归一化成统一记录
# ----------------------------------------------------------------------


def build_bundle(parsed: ParsedJournal, *, source_sha256: str) -> JournalBundle:
    """把解析结果归一化成一次采集：一条 collection、若干实体、每条日志一条 event 记录。

    写入口径（PL 在 1.4 任务交接里定下的）：

    - 每条 Journal 条目写成一条 ``event`` 记录，``payload.attributes.derived_from = "journal"``；
    - 原文关键字段保留在 ``payload``：``message``、``priority``、``unit``、``identifier``、
      ``cursor``，以及完整的原始 ``fields``；
    - 关联信息：Boot ID 落成 Boot 实体，Unit 落成 service 实体（记录用 ``entity`` 指向它）；
      PID 与 Invocation ID 是逐条日志的属性，落在 ``payload.attributes``——它们随每条日志变化，
      做成实体会产生大量一次性实体，反而查不回来；
    - 整份归档没有资源采样，采集上标 ``resources.available = false`` 并写明原因。
    """
    if not parsed.entries:
        raise PluginInputError("归档里没有 Journal 条目，没有可导入的数据")

    boot_ids = sorted({_normalized_boot_id(entry) for entry in parsed.entries})
    if len(boot_ids) > 1:
        raise PluginInputError(
            f"归档里有 {len(boot_ids)} 次启动（{'、'.join(boot_ids)}）。一份归档只能对应一次启动："
            "请按 boot 分别导出、分别上传"
        )
    boot_id = boot_ids[0]

    hostnames = sorted({host for entry in parsed.entries if (host := _hostname(entry))})
    if not hostnames:
        raise PluginInputError("归档里没有 _HOSTNAME，无法确定 machine_id")
    if len(hostnames) > 1:
        raise PluginInputError(
            f"归档里有 {len(hostnames)} 台机器的日志（{'、'.join(hostnames)}）。"
            "一份归档只能对应一台机器"
        )
    hostname = hostnames[0]
    machine_id = f"{MACHINE_ID_PREFIX}{hostname}"
    kernel_version = _first(parsed.entries, "_KERNEL_VERSION")

    machine = MachineEntity(
        entity_type=EntityType.MACHINE,
        machine_id=machine_id,
        hostname=hostname,
        kernel_version=kernel_version,
    )
    boot = BootEntity(
        entity_type=EntityType.BOOT,
        machine_id=machine_id,
        boot_id=boot_id,
        # Journal-only 导入没有采集到启动时刻。data-model 第 5.2 节要求写 null，不允许用
        # 「realtime − monotonic」的估计值冒充实测值。
        started_realtime_ns=None,
        kernel_version=kernel_version,
    )
    services = _service_entities(parsed.entries, machine_id=machine_id, boot_id=boot_id)
    collection_id = f"{COLLECTION_ID_PREFIX}{machine_id}:{boot_id}"
    records = _event_records(
        parsed.entries,
        machine_id=machine_id,
        boot_id=boot_id,
        collection_id=collection_id,
        services={str(entity.entity_id) for entity in services},
        skipped_fields=parsed.skipped_fields,
    )
    collection = CollectionRef(
        collection_id=collection_id,
        machine_id=machine_id,
        boot_id=boot_id,
        mode=CollectionMode.JOURNAL_IMPORT,
        created_realtime_ns=None,
    )
    return JournalBundle(
        collection=collection,
        entities=(machine, boot, *services),
        records=records,
        source_sha256=source_sha256,
        resources=ResourceAvailability(available=False, reason=NO_RESOURCE_REASON),
    )


def summarise(bundle: JournalBundle) -> JournalArchiveSummary:
    """给人看的摘要：机器、启动、采集、条目数与涉及的单元。"""
    machine = next(item for item in bundle.entities if isinstance(item, MachineEntity))
    boot = next(item for item in bundle.entities if isinstance(item, BootEntity))
    units = tuple(
        sorted(
            str(item.entity_id)
            for item in bundle.entities
            if getattr(item, "entity_type", None) == EntityType.SERVICE
        )
    )
    return JournalArchiveSummary(
        machine_id=machine.machine_id,
        boot_id=boot.boot_id,
        collection_id=bundle.collection.collection_id,
        hostname=machine.hostname,
        kernel_version=machine.kernel_version,
        entry_count=len(bundle.records),
        service_units=units,
    )


def _service_entities(
    entries: Sequence[Mapping[str, str]], *, machine_id: str, boot_id: str
) -> tuple[GenericEntity, ...]:
    """日志里出现过的 systemd 单元，每个一条 service 实体。"""
    units = sorted({unit for entry in entries if (unit := _unit(entry))})
    return tuple(
        GenericEntity(
            entity_type=EntityType.SERVICE,
            entity_id=f"{EntityType.SERVICE}:{unit}",
            machine_id=machine_id,
            boot_id=boot_id,
            name=unit,
            attributes={"unit": unit},
        )
        for unit in units
    )


def _event_records(
    entries: Sequence[Mapping[str, str]],
    *,
    machine_id: str,
    boot_id: str,
    collection_id: str,
    services: set[str],
    skipped_fields: int,
) -> tuple[dict[str, Any], ...]:
    """把每条日志变成一条 event 记录字典，``record_id`` 按 data-model 第 8 节算。"""
    records: list[dict[str, Any]] = []
    for index, entry in enumerate(entries, start=1):
        position = _position(entry, index)
        priority = _priority(entry, index)
        unit = _unit(entry)
        entity_id = f"{EntityType.SERVICE}:{unit}" if unit else None
        if entity_id is not None and entity_id not in services:
            entity_id = None
        attributes: dict[str, Any] = {
            "derived_from": "journal",
            "priority": priority,
            "fields": dict(entry),
        }
        for key, field_name in (
            ("unit", "_SYSTEMD_UNIT"),
            ("identifier", "SYSLOG_IDENTIFIER"),
            ("cursor", "__CURSOR"),
            ("invocation_id", "_SYSTEMD_INVOCATION_ID"),
            ("hostname", "_HOSTNAME"),
        ):
            value = entry.get(field_name)
            if value is not None:
                attributes[key] = value
        pid = _pid(entry, index)
        if pid is not None:
            attributes["pid"] = pid
        if skipped_fields:
            attributes["skipped_fields"] = skipped_fields

        body: dict[str, Any] = {
            "kind": RecordKind.EVENT.value,
            "machine_id": machine_id,
            "boot_id": boot_id,
            "collection_id": collection_id,
            "source_plugin": PLUGIN_ID,
            "stream_id": STREAM_ID,
            "monotonic_ns": position.monotonic_ns,
            "realtime_ns": position.realtime_ns,
            "clock_domain": position.clock_domain.value,
            "entity": entity_id,
            "payload": {
                "event_type": EVENT_TYPE,
                "severity": _severity(priority).value,
                "message": entry.get("MESSAGE", ""),
                "attributes": attributes,
            },
        }
        records.append({"record_id": compute_record_id(body), **body})
    return tuple(records)


def _position(entry: Mapping[str, str], index: int) -> JournalEventPosition:
    try:
        position = journal_event_position(entry)
    except (ValueError, OverflowError) as error:
        raise PluginInputError(
            f"第 {index} 条 Journal 事件换算不到 boot 时间轴：{error}"
        ) from error
    if position.realtime_ns is None:
        # ``docs/bootrec-v1.md`` 第 5 节把 journal 条目的 realtime_ns 定为必填，
        # ``docs/time-base.md`` 5.1 也写明缺失时「该条目不能进 journal.jsonl」。
        # 只有单调读数的条目因此整份拒绝，而不是写一条 realtime_ns 为 null 的记录。
        raise PluginInputError(
            f"第 {index} 条 Journal 事件没有 {REALTIME_FIELD}：按 docs/bootrec-v1.md 第 5 节，"
            "journal 条目的墙钟时间是必填的，缺失说明导出过程有问题，整份拒绝"
        )
    return position


def _normalized_boot_id(entry: Mapping[str, str]) -> str:
    raw = entry.get(BOOT_ID_FIELD)
    if not raw:
        raise PluginInputError(f"归档里有事件缺少 {BOOT_ID_FIELD}")
    try:
        return normalize_boot_id(raw)
    except ValueError as error:
        raise PluginInputError(f"{BOOT_ID_FIELD} 不合法：{error}") from error


def _hostname(entry: Mapping[str, str]) -> str | None:
    host = entry.get("_HOSTNAME", "").strip()
    return host or None


def _unit(entry: Mapping[str, str]) -> str | None:
    unit = entry.get("_SYSTEMD_UNIT", "").strip()
    return unit or None


def _priority(entry: Mapping[str, str], index: int) -> int:
    raw = entry.get("PRIORITY")
    if raw is None or raw == "":
        return DEFAULT_PRIORITY
    try:
        priority = int(raw)
    except ValueError as error:
        raise PluginInputError(f"第 {index} 条 Journal 事件的 PRIORITY={raw!r} 不是整数") from error
    if not 0 <= priority <= 7:
        raise PluginInputError(f"第 {index} 条 Journal 事件的 PRIORITY={priority} 超出 0-7")
    return priority


def _pid(entry: Mapping[str, str], index: int) -> int | None:
    raw = entry.get("_PID")
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError as error:
        raise PluginInputError(f"第 {index} 条 Journal 事件的 _PID={raw!r} 不是整数") from error


def _severity(priority: int) -> Severity:
    if priority <= PRIORITY_ERROR:
        return Severity.ERROR
    if priority == PRIORITY_WARNING:
        return Severity.WARNING
    return Severity.INFO


def _first(entries: Sequence[Mapping[str, str]], field_name: str) -> str | None:
    for entry in entries:
        value = entry.get(field_name, "").strip()
        if value:
            return value
    return None


def archive_digest(path: Path) -> str:
    """整份归档的摘要，落到 ``collections.source_sha256`` 做第一级幂等（data-model 第 8 节）。"""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise PluginInputError(f"读不到归档文件 {path}：{error}") from error
    return digest.hexdigest()


__all__ = [
    "CAPABILITY",
    "CONFIG_MAX_BYTES",
    "DEFAULT_MAX_DECOMPRESSED_BYTES",
    "ENTRY_MEMORY_ESTIMATE_BYTES",
    "PLUGIN_ID",
    "CollectionImportAccess",
    "JournalArchiveSummary",
    "JournalBundle",
    "ParsedJournal",
    "archive_digest",
    "build_bundle",
    "detect",
    "iter_export_events",
    "parse_journal_archive",
    "parse_native",
    "summarise",
]
