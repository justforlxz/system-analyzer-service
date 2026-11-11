"""Journal 压缩包导入的单元测试。

覆盖：两种输入格式、时间换算与 0.3 文档示例对齐、无资源数据的标记、实体与记录关联、
幂等用的 ``record_id``、多启动与多机器的拒绝、压缩容器、以及插件形状与注册。
真实数据库上的写入在 ``tests/integration/test_journal_archive_import.py``。
"""

from __future__ import annotations

import gzip
import inspect
import io
import struct
import tarfile
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from system_analyzer.domain.bootrec import (
    BootEntity,
    ClockDomain,
    EntityType,
    GenericEntity,
    MachineEntity,
    RecordKind,
    compute_record_id,
)
from system_analyzer.domain.time_base import format_boot_relative
from system_analyzer.importers import journal_archive as archive
from system_analyzer.importers.journal_archive_importer import JournalArchiveImporter
from system_analyzer.plugins.builtins import register_builtins
from system_analyzer.plugins.contracts import (
    ImporterPlugin,
    ImportResult,
    ImportSource,
    PluginContext,
    PluginInputError,
    PluginKind,
)
from system_analyzer.plugins.registry import PluginRegistry

FIXTURES = Path(__file__).parent / "fixtures" / "journal_archive"
EXPORT_FIXTURE = FIXTURES / "boot.export"
NATIVE_FIXTURE = FIXTURES / "boot.journal.gz"

BOOT_ID = "abc7451f-65c8-4be8-ab01-4b9a1e49a8b9"
#: 样例里第一条事件的两个读数（微秒），同时也是 ``docs/time-base.md`` 第 5.3 节的示例值。
MONOTONIC_MICROS = 175830828563
REALTIME_MICROS = 1794367420210127


class RecordingStore:
    """记录插件写了什么的数据入口：形状与存储层的整份写入入口一致，不碰数据库。"""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, Any, Any, str | None, Any]] = []

    async def import_collection(
        self,
        collection: Any,
        entities: Sequence[Any],
        records: Sequence[Any],
        *,
        source_sha256: str | None = None,
        resources: Any = None,
    ) -> SimpleNamespace:
        self.calls.append((collection, entities, records, source_sha256, resources))
        return SimpleNamespace(records_written=len(records), collection_id=collection.collection_id)


class AccessWithoutImport:
    """只有契约里的逐条入口，没有整份写入能力的对象。"""

    def append_records(self, records: Any, *, plugin_id: str, source: str) -> int:
        return 0

    def query_records(self, *, record_kinds: Any = (), limit: Any = None) -> tuple[()]:
        return ()


@pytest.fixture(autouse=True)
def clean_registry():
    from system_analyzer.plugins.registry import reset_registry

    reset_registry()
    yield
    reset_registry()


def parsed_export() -> archive.ParsedJournal:
    return archive.parse_journal_archive(EXPORT_FIXTURE)


def parsed_native() -> archive.ParsedJournal:
    return archive.parse_journal_archive(NATIVE_FIXTURE)


def bundle(**kwargs: Any) -> archive.JournalBundle:
    return archive.build_bundle(kwargs.pop("parsed", parsed_export()), source_sha256="0" * 64)


def write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def export_text(*, boot_id: str = BOOT_ID, hostname: str = "uos-PC") -> bytes:
    """一条最小的 export 事件，用来构造各种反例。"""
    return (
        "__REALTIME_TIMESTAMP=1794367420210127\n"
        "__MONOTONIC_TIMESTAMP=175830828563\n"
        f"_BOOT_ID={boot_id}\n"
        f"_HOSTNAME={hostname}\n"
        "PRIORITY=6\n"
        "SYSLOG_IDENTIFIER=systemd\n"
        "_SYSTEMD_UNIT=nginx.service\n"
        "MESSAGE=Started nginx\n"
        "\n"
    ).encode()


# --- 两种输入格式 ------------------------------------------------------------


def test_detect_recognises_the_supported_sources(tmp_path: Path) -> None:
    assert archive.detect(EXPORT_FIXTURE) is True
    assert archive.detect(NATIVE_FIXTURE) is True

    tar_path = write(
        tmp_path, "archive.tar", tar_bytes([("boot.export", EXPORT_FIXTURE.read_bytes())])
    )
    assert archive.detect(tar_path) is True

    unrelated = write(tmp_path, "notes.txt", b"hello world\n")
    assert archive.detect(unrelated) is False


def test_export_and_native_agree_on_every_field_but_the_cursor() -> None:
    """两种输入是同一份日志的两种写法，除了游标（原生文件不存游标）以外必须一致。"""
    exported = parsed_export().entries
    native = parsed_native().entries

    assert len(exported) == len(native) == 4
    for from_export, from_native in zip(exported, native, strict=True):
        assert {k: v for k, v in from_export.items() if k != "__CURSOR"} == {
            k: v for k, v in from_native.items() if k != "__CURSOR"
        }


def test_native_file_reads_the_entry_timestamps_the_export_shows() -> None:
    """原生文件的时间戳在 ENTRY 对象上，不在 DATA 字段里，必须补成与导出一致的字段名。"""
    entry = parsed_native().entries[0]
    assert entry["__MONOTONIC_TIMESTAMP"] == str(MONOTONIC_MICROS)
    assert entry["__REALTIME_TIMESTAMP"] == str(REALTIME_MICROS)
    assert entry["_BOOT_ID"] == "abc7451f65c84be8ab014b9a1e49a8b9"


def test_binary_export_fields_are_skipped_without_breaking_the_entry(
    tmp_path: Path,
) -> None:
    raw = b"\x00\xff binary payload \n"
    blob = (
        b"__REALTIME_TIMESTAMP=1794367420210127\n"
        b"__MONOTONIC_TIMESTAMP=175830828563\n"
        b"_BOOT_ID=abc7451f65c84be8ab014b9a1e49a8b9\n"
        b"_HOSTNAME=uos-PC\n"
        b"SYSLOG_RAW\n" + struct.pack("<Q", len(raw)) + raw + b"\n"
        b"MESSAGE=after the binary field\n"
        b"\n"
    )
    path = write(tmp_path, "binary.export", blob)
    entries = list(archive.iter_export_events(blob, name="binary.export"))

    assert len(entries) == 1
    assert entries[0]["MESSAGE"] == "after the binary field"
    assert "SYSLOG_RAW" not in entries[0]
    assert archive.detect(path) is True


# --- 时间换算 ----------------------------------------------------------------


def test_time_conversion_matches_the_time_base_document() -> None:
    """§5.3 的示例值：175830828563 µs → 175830828563000 ns，即启动后 2 天 50 分 30.828563 秒。"""
    record = bundle().records[0]

    assert record["monotonic_ns"] == MONOTONIC_MICROS * 1000
    assert record["realtime_ns"] == REALTIME_MICROS * 1000
    assert record["clock_domain"] == ClockDomain.BOOT_MONOTONIC.value
    assert format_boot_relative(record["monotonic_ns"]) == "2d 00:50:30.828563000"


def test_entries_without_monotonic_clock_land_in_the_wall_clock_domain(
    tmp_path: Path,
) -> None:
    blob = (
        b"__REALTIME_TIMESTAMP=1794367420210127\n"
        b"_BOOT_ID=abc7451f65c84be8ab014b9a1e49a8b9\n"
        b"_HOSTNAME=uos-PC\n"
        b"MESSAGE=no monotonic reading\n"
        b"\n"
    )
    path = write(tmp_path, "wall.export", blob)
    record = archive.build_bundle(
        archive.parse_journal_archive(path), source_sha256="0" * 64
    ).records[0]

    assert record["monotonic_ns"] is None
    assert record["clock_domain"] == ClockDomain.WALL_REALTIME.value
    assert record["realtime_ns"] == REALTIME_MICROS * 1000


def test_entry_without_any_timestamp_is_rejected(tmp_path: Path) -> None:
    blob = b"_BOOT_ID=abc7451f65c84be8ab014b9a1e49a8b9\n_HOSTNAME=uos-PC\nMESSAGE=x\n\n"
    path = write(tmp_path, "no-time.export", blob)
    with pytest.raises(PluginInputError, match="换算不到 boot 时间轴"):
        archive.build_bundle(archive.parse_journal_archive(path), source_sha256="0" * 64)


# --- 统一记录与实体 ----------------------------------------------------------


def test_records_are_events_derived_from_journal() -> None:
    records = bundle().records

    assert len(records) == 4
    assert {record["kind"] for record in records} == {RecordKind.EVENT.value}
    assert {record["source_plugin"] for record in records} == {archive.PLUGIN_ID}
    assert {record["stream_id"] for record in records} == {archive.STREAM_ID}
    for record in records:
        attributes = record["payload"]["attributes"]
        assert attributes["derived_from"] == "journal"
        assert record["payload"]["event_type"] == archive.EVENT_TYPE
        # 原文关键字段都在：message / priority / fields，还有原始整份字段
        assert isinstance(record["payload"]["message"], str)
        assert isinstance(attributes["priority"], int)
        assert attributes["fields"] == {key: value for key, value in attributes["fields"].items()}


def test_record_id_is_the_content_digest() -> None:
    """``record_id`` 必须等于按内容算出的摘要，否则存储层会拒绝写入（data-model 第 8 节）。"""
    for record in bundle().records:
        body = {key: value for key, value in record.items() if key != "record_id"}
        assert record["record_id"] == compute_record_id(body)


def test_boot_and_service_entities_are_created_and_linked() -> None:
    built = bundle()
    machine = next(item for item in built.entities if isinstance(item, MachineEntity))
    boot = next(item for item in built.entities if isinstance(item, BootEntity))
    services = [
        item for item in built.entities if getattr(item, "entity_type", "") == EntityType.SERVICE
    ]

    assert machine.machine_id == "machine:uos-PC"
    assert machine.hostname == "uos-PC"
    assert machine.kernel_version == "6.6.0-amd64-desktop"
    assert boot.boot_id == BOOT_ID
    # Journal-only 导入没有采集到启动时刻：写 null，不用估算值（data-model 第 5.2 节）
    assert boot.started_realtime_ns is None
    assert {service.entity_id for service in services} == {
        "service:nginx.service",
        "service:session-674.scope",
        "service:sshd.service",
    }

    known = {item.entity_id for item in built.entities if hasattr(item, "entity_id")}
    for record in built.records:
        assert record["entity"] is None or record["entity"] in known


def test_kernel_log_has_no_unit_and_stays_unlinked() -> None:
    records = bundle().records
    oom = next(record for record in records if "Out of memory" in record["payload"]["message"])

    assert oom["entity"] is None
    assert oom["payload"]["severity"] == "error"
    assert "unit" not in oom["payload"]["attributes"]


def test_pid_unit_and_invocation_id_are_kept_as_attributes() -> None:
    first = bundle().records[0]["payload"]["attributes"]

    assert first["unit"] == "session-674.scope"
    assert first["identifier"] == "sudo"
    assert first["pid"] == 3439748
    assert first["invocation_id"] == "39b369d1e917432bb0a9bb9d4df7d03e"
    assert first["cursor"].startswith("s=ee9702b862c943f8b9fcf0bfaa016180")


def test_missing_priority_defaults_to_info(tmp_path: Path) -> None:
    blob = (
        b"__REALTIME_TIMESTAMP=1794367420210127\n"
        b"__MONOTONIC_TIMESTAMP=175830828563\n"
        b"_BOOT_ID=abc7451f65c84be8ab014b9a1e49a8b9\n"
        b"_HOSTNAME=uos-PC\n"
        b"MESSAGE=no priority field\n"
        b"\n"
    )
    path = write(tmp_path, "no-priority.export", blob)
    record = archive.build_bundle(
        archive.parse_journal_archive(path), source_sha256="0" * 64
    ).records[0]

    assert record["payload"]["attributes"]["priority"] == archive.DEFAULT_PRIORITY
    assert record["payload"]["severity"] == "info"


# --- 没有资源采样 ------------------------------------------------------------


def test_archive_is_marked_as_having_no_resource_data() -> None:
    built = bundle()

    assert built.resources.available is False
    assert built.resources.reason
    assert [record for record in built.records if record["kind"] == RecordKind.METRIC.value] == []
    assert built.collection.mode.value == "journal_import"


def test_no_metric_stream_entity_is_fabricated() -> None:
    built = bundle()
    assert [
        item
        for item in built.entities
        if getattr(item, "entity_type", "") == EntityType.METRIC_STREAM
    ] == []


# --- 拒绝的情形 --------------------------------------------------------------


def test_multi_boot_archive_is_rejected(tmp_path: Path) -> None:
    blob = export_text() + export_text(boot_id="7ccd5356e1de450bbe90fd63765ea774")
    path = write(tmp_path, "two-boots.export", blob)

    with pytest.raises(PluginInputError, match="一份归档只能对应一次启动"):
        archive.build_bundle(archive.parse_journal_archive(path), source_sha256="0" * 64)


def test_two_hostnames_are_rejected(tmp_path: Path) -> None:
    blob = export_text() + export_text(hostname="other-PC")
    path = write(tmp_path, "two-hosts.export", blob)

    with pytest.raises(PluginInputError, match="一份归档只能对应一台机器"):
        archive.build_bundle(archive.parse_journal_archive(path), source_sha256="0" * 64)


def test_missing_hostname_is_rejected(tmp_path: Path) -> None:
    blob = b"__REALTIME_TIMESTAMP=1794367420210127\n_BOOT_ID=abc7451f65c84be8ab014b9a1e49a8b9\n\n"
    path = write(tmp_path, "no-host.export", blob)

    with pytest.raises(PluginInputError, match="_HOSTNAME"):
        archive.build_bundle(archive.parse_journal_archive(path), source_sha256="0" * 64)


def test_invalid_boot_id_is_rejected(tmp_path: Path) -> None:
    path = write(tmp_path, "bad-boot.export", export_text(boot_id="not-a-boot-id"))

    with pytest.raises(PluginInputError, match="_BOOT_ID"):
        archive.build_bundle(archive.parse_journal_archive(path), source_sha256="0" * 64)


def test_unsupported_field_compression_is_reported_not_hidden(
    tmp_path: Path,
) -> None:
    """字段被 zstd 压过时没有解码依赖：跳过该字段，但要在 ParsedJournal 上计数。"""
    native = decompress_native()
    patched = bytearray(native)
    data_offset = first_data_object(native)
    patched[data_offset + 1] = archive.OBJECT_COMPRESSED_ZSTD
    path = write(tmp_path, "zstd-field.journal", bytes(patched))

    parsed = archive.parse_journal_archive(path)

    assert parsed.skipped_fields == 1
    assert len(parsed.entries) == 4


def test_zstd_container_is_rejected_with_a_readable_reason(tmp_path: Path) -> None:
    path = write(tmp_path, "archive.tar.zst", b"\x28\xb5\x2f\xfd" + b"\x00" * 64)

    with pytest.raises(PluginInputError, match="zstd"):
        archive.parse_journal_archive(path)


# --- 压缩包 ------------------------------------------------------------------


def tar_bytes(members: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def test_tar_archive_with_several_journal_files_is_concatenated(tmp_path: Path) -> None:
    first = export_text()
    second = export_text().replace(b"MESSAGE=Started nginx", b"MESSAGE=Second event")
    path = write(
        tmp_path, "journal.tar", tar_bytes([("boot-1.export", first), ("boot-2.export", second)])
    )

    parsed = archive.parse_journal_archive(path)

    assert parsed.streams == ("boot-1.export", "boot-2.export")
    assert len(parsed.entries) == 2


def test_tar_archive_ignores_non_journal_members(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "journal.tar",
        tar_bytes([("boot.export", export_text()), ("README", b"not a journal\n")]),
    )

    parsed = archive.parse_journal_archive(path)

    assert parsed.ignored_members == ("README",)
    assert len(parsed.entries) == 1


def test_tar_archive_without_any_journal_file_is_rejected(tmp_path: Path) -> None:
    path = write(tmp_path, "empty.tar", tar_bytes([("README", b"not a journal\n")]))

    with pytest.raises(PluginInputError, match="没有 Journal 文件"):
        archive.parse_journal_archive(path)


def test_gzipped_export_text_is_parsed(tmp_path: Path) -> None:
    path = write(tmp_path, "boot-export.gz", gzip.compress(export_text()))

    assert archive.detect(path) is True
    assert len(archive.parse_journal_archive(path).entries) == 1


# --- 插件形状与注册 ----------------------------------------------------------


def test_plugin_declares_a_working_importer_manifest() -> None:
    """``isinstance`` 只查属性是否存在，查不出签名差异；入口是 async 是一条已上报的偏差。

    见 ``docs/journal-archive-import.md`` 偏差清单第 1 条：契约 3.3 节写的是同步签名，这里显式
    断言「现在是异步」，让偏差在代码里可见，而不是被一个名字夸大的用例盖住。
    """
    plugin = JournalArchiveImporter()

    assert isinstance(plugin, ImporterPlugin)
    assert plugin.spec.kind is PluginKind.IMPORTER
    assert plugin.spec.capabilities == (archive.CAPABILITY,)
    assert plugin.detect(ImportSource(path=EXPORT_FIXTURE)) is True
    assert inspect.iscoroutinefunction(plugin.import_source)


def test_plugin_is_registered_as_a_builtin_importer() -> None:
    registry = register_builtins(PluginRegistry())

    # 内置清单是追加式的（1.3 的 bootrec.importer 与本插件并列），只断言本插件在其中；
    # capability 是本插件独有的，可以断言得精确一些。
    assert archive.PLUGIN_ID in [plugin.spec.id for plugin in registry.by_kind(PluginKind.IMPORTER)]
    assert [plugin.spec.id for plugin in registry.by_capability(archive.CAPABILITY)] == [
        archive.PLUGIN_ID
    ]


async def test_import_source_writes_the_bundle_through_the_data_entry() -> None:
    plugin = JournalArchiveImporter()
    data = RecordingStore()
    context = PluginContext(plugin_id=plugin.spec.id, data=data, run_id="run-1")

    result = await plugin.import_source(ImportSource(path=EXPORT_FIXTURE), context)

    assert isinstance(result, ImportResult)
    assert result.records_written == 4
    assert len(data.calls) == 1
    collection, entities, records, digest, resources = data.calls[0]
    assert collection.collection_id == f"journal-import:machine:uos-PC:{BOOT_ID}"
    assert len(records) == 4
    assert [entity.entity_id for entity in entities if hasattr(entity, "entity_id")] == [
        "service:nginx.service",
        "service:session-674.scope",
        "service:sshd.service",
    ]
    assert digest == archive.archive_digest(EXPORT_FIXTURE)
    assert resources.available is False


async def test_import_source_rejects_a_source_it_does_not_recognise(tmp_path: Path) -> None:
    plugin = JournalArchiveImporter()
    path = write(tmp_path, "notes.txt", b"hello\n")

    with pytest.raises(PluginInputError, match="不认这个来源"):
        await plugin.import_source(
            ImportSource(path=path),
            PluginContext(plugin_id=plugin.spec.id, data=RecordingStore()),
        )


async def test_import_source_requires_a_collection_writing_data_entry() -> None:
    plugin = JournalArchiveImporter()

    with pytest.raises(PluginInputError, match="import_collection"):
        await plugin.import_source(
            ImportSource(path=EXPORT_FIXTURE),
            PluginContext(plugin_id=plugin.spec.id, data=AccessWithoutImport()),
        )


def test_summary_lists_units_and_counts() -> None:
    summary = archive.summarise(bundle())

    assert summary.machine_id == "machine:uos-PC"
    assert summary.boot_id == BOOT_ID
    assert summary.entry_count == 4
    assert summary.collection_id == f"journal-import:machine:uos-PC:{BOOT_ID}"
    assert summary.service_units == (
        "service:nginx.service",
        "service:session-674.scope",
        "service:sshd.service",
    )


# --- 原生文件的辅助 ----------------------------------------------------------


def decompress_native() -> bytes:
    return gzip.decompress(NATIVE_FIXTURE.read_bytes())


def first_data_object(blob: bytes) -> int:
    """找到第一个 DATA 对象的偏移，用来造「字段被压缩」的用例。"""
    (header_size,) = struct.unpack_from("<Q", blob, 88)
    position = header_size
    while position + 16 <= len(blob):
        object_type = blob[position]
        (size,) = struct.unpack_from("<Q", blob, position + 8)
        if object_type == archive.OBJECT_DATA:
            return position
        position = (position + size + 7) & ~7
    raise AssertionError("样例里没有 DATA 对象")


def test_generic_entity_type_is_used_for_units() -> None:
    services = [
        item for item in bundle().entities if getattr(item, "entity_type", "") == EntityType.SERVICE
    ]
    assert all(isinstance(item, GenericEntity) for item in services)
    assert all(item.attributes == {"unit": item.name} for item in services)


# --- 体积上限 ----------------------------------------------------------------


def big_export(min_bytes: int) -> bytes:
    """一份解压后不小于 ``min_bytes`` 的导出文本。"""
    filler = b"M" * max(1, min_bytes - 200)
    return (
        b"__REALTIME_TIMESTAMP=1794367420210127\n"
        b"__MONOTONIC_TIMESTAMP=175830828563\n"
        b"_BOOT_ID=abc7451f65c84be8ab014b9a1e49a8b9\n"
        b"_HOSTNAME=uos-PC\n"
        b"MESSAGE=" + filler + b"\n\n"
    )


def test_archive_over_the_decompressed_limit_is_rejected(tmp_path: Path) -> None:
    """上限按解压后的字节数计：压缩前很小也要拦住，并报出实际字节数与上限。"""
    path = write(tmp_path, "big.export.gz", gzip.compress(big_export(50_000)))

    with pytest.raises(PluginInputError, match="超过上限") as raised:
        archive.parse_journal_archive(path, max_decompressed_bytes=4096)

    message = str(raised.value)
    assert "上限 4096 字节" in message
    assert f"{archive.CONFIG_MAX_BYTES}" in message


def test_high_compression_ratio_archive_is_rejected(tmp_path: Path) -> None:
    """压缩炸弹：1 MiB 的零压成几百字节，上限 64 KiB 时立即中止。"""
    compressed = gzip.compress(b"\0" * (1024 * 1024))
    path = write(tmp_path, "bomb.gz", compressed)
    assert len(compressed) < 4096

    with pytest.raises(PluginInputError, match="超过上限"):
        archive.parse_journal_archive(path, max_decompressed_bytes=64 * 1024)


def test_tar_member_over_the_remaining_limit_is_rejected(tmp_path: Path) -> None:
    member = gzip.compress(big_export(50_000))
    path = write(tmp_path, "archive.tar", tar_bytes([("boot.export.gz", member)]))

    with pytest.raises(PluginInputError, match="超过上限"):
        archive.parse_journal_archive(path, max_decompressed_bytes=4096)


def test_the_native_fixture_needs_a_limit_above_its_expanded_size() -> None:
    """样例只有 10 KB，解开是 8 MiB 的预分配文件：上限 1 MiB 会被拒，默认 512 MiB 能过。"""
    with pytest.raises(PluginInputError, match="超过上限"):
        archive.parse_journal_archive(NATIVE_FIXTURE, max_decompressed_bytes=1024 * 1024)

    assert len(archive.parse_journal_archive(NATIVE_FIXTURE).entries) == 4


@pytest.mark.parametrize("limit", [0, -1, True, "1024"])
async def test_plugin_rejects_an_invalid_limit(limit: Any) -> None:
    """直接构造 ``PluginContext`` 时不过 Core 的校验，插件自己也要挡住非法上限。"""
    plugin = JournalArchiveImporter()

    with pytest.raises(PluginInputError, match="必须是正整数"):
        await plugin.import_source(
            ImportSource(path=EXPORT_FIXTURE),
            PluginContext(
                plugin_id=plugin.spec.id,
                data=RecordingStore(),
                config={archive.CONFIG_MAX_BYTES: limit},
            ),
        )


async def test_plugin_applies_the_configured_limit(tmp_path: Path) -> None:
    plugin = JournalArchiveImporter()
    path = write(tmp_path, "big.export.gz", gzip.compress(big_export(50_000)))

    with pytest.raises(PluginInputError, match="超过上限"):
        await plugin.import_source(
            ImportSource(path=path),
            PluginContext(
                plugin_id=plugin.spec.id,
                data=RecordingStore(),
                config={archive.CONFIG_MAX_BYTES: 4096},
            ),
        )

    result = await plugin.import_source(
        ImportSource(path=path),
        PluginContext(
            plugin_id=plugin.spec.id,
            data=RecordingStore(),
            config={archive.CONFIG_MAX_BYTES: 200_000},
        ),
    )
    assert result.records_written == 1


def test_config_schema_is_enforced_by_the_core_validator() -> None:
    """上限是真正的插件配置：Core 的校验器认得它，也拒绝不合法的值。"""
    from system_analyzer.plugins.config import PluginConfigError, validate_plugin_config

    plugin = JournalArchiveImporter()

    assert validate_plugin_config(plugin.spec, {}) == {}
    assert validate_plugin_config(plugin.spec, {archive.CONFIG_MAX_BYTES: 1024}) == {
        archive.CONFIG_MAX_BYTES: 1024
    }
    with pytest.raises(PluginConfigError, match=archive.CONFIG_MAX_BYTES):
        validate_plugin_config(plugin.spec, {archive.CONFIG_MAX_BYTES: "big"})
    with pytest.raises(PluginConfigError):
        validate_plugin_config(plugin.spec, {"unknown_key": 1})


def test_default_limit_is_512_mib() -> None:
    assert archive.DEFAULT_MAX_DECOMPRESSED_BYTES == 512 * 1024 * 1024


def test_truncated_compressed_archive_is_rejected(tmp_path: Path) -> None:
    """上传被打断时必须报错，不能当成「少了几条日志但导入成功」。"""
    whole = gzip.compress(EXPORT_FIXTURE.read_bytes())
    path = write(tmp_path, "cut.gz", whole[: len(whole) // 2])

    with pytest.raises(PluginInputError, match="压缩流不完整"):
        archive.parse_journal_archive(path)


def test_concatenated_gzip_members_are_all_read(tmp_path: Path) -> None:
    """``cat a.gz b.gz`` 是合法的 gzip 多成员写法，后面的成员不能被丢掉。"""
    first = export_text()
    second = export_text().replace(b"MESSAGE=Started nginx", b"MESSAGE=Second member")
    path = write(tmp_path, "two-members.gz", gzip.compress(first) + gzip.compress(second))

    entries = archive.parse_journal_archive(path).entries

    assert [entry["MESSAGE"] for entry in entries] == ["Started nginx", "Second member"]


def test_entry_count_is_charged_against_the_budget(tmp_path: Path) -> None:
    """预算同时管条数：体积没超限时，条数按每条 6 KiB 估算计入，避免几百万条把内存吃光。"""
    many = b"".join(
        (
            f"__REALTIME_TIMESTAMP={REALTIME_MICROS + index}\n"
            f"__MONOTONIC_TIMESTAMP={MONOTONIC_MICROS + index}\n"
            "_BOOT_ID=abc7451f65c84be8ab014b9a1e49a8b9\n"
            "_HOSTNAME=uos-PC\n"
            f"MESSAGE=event {index}\n\n"
        ).encode()
        for index in range(20)
    )
    path = write(tmp_path, "many.export", many)
    estimated = 20 * archive.ENTRY_MEMORY_ESTIMATE_BYTES
    assert len(many) < 65536 < estimated

    with pytest.raises(PluginInputError, match="解析出的事件记录"):
        archive.parse_journal_archive(path, max_decompressed_bytes=65536)

    assert (
        len(
            archive.parse_journal_archive(
                path, max_decompressed_bytes=estimated + len(many) + 1
            ).entries
        )
        == 20
    )


def test_truncated_export_text_is_rejected(tmp_path: Path) -> None:
    """未压缩的导出文本被截断：最后一条事件只剩半句，必须整份拒绝。"""
    data = EXPORT_FIXTURE.read_bytes()
    cut = data.index(b"MESSAGE=Out of memory: ") + len(b"MESSAGE=Out of memory: ") + 15
    path = write(tmp_path, "trunc.export", data[:cut])

    with pytest.raises(PluginInputError, match="导出文本不完整"):
        archive.parse_journal_archive(path)


async def test_plugin_rejects_a_truncated_export_without_writing(tmp_path: Path) -> None:
    """走插件的路径同样拒绝，而且一条记录都没写（不留半份数据）。"""
    data = EXPORT_FIXTURE.read_bytes()
    cut = data.index(b"MESSAGE=Out of memory: ") + len(b"MESSAGE=Out of memory: ") + 15
    path = write(tmp_path, "trunc.export", data[:cut])
    data_access = RecordingStore()
    plugin = JournalArchiveImporter()

    with pytest.raises(PluginInputError, match="导出文本不完整"):
        await plugin.import_source(
            ImportSource(path=path),
            PluginContext(plugin_id=plugin.spec.id, data=data_access),
        )

    assert data_access.calls == []


def test_missing_realtime_timestamp_is_rejected(tmp_path: Path) -> None:
    """``realtime_ns`` 在 journal 条目里必填（bootrec-v1 第 5 节），缺了整份拒绝。"""
    stripped = b"".join(
        line
        for line in EXPORT_FIXTURE.read_bytes().splitlines(keepends=True)
        if not line.startswith(b"__REALTIME_TIMESTAMP=")
    )
    path = write(tmp_path, "no-realtime.export", stripped)

    with pytest.raises(PluginInputError, match="__REALTIME_TIMESTAMP"):
        archive.build_bundle(archive.parse_journal_archive(path), source_sha256="0" * 64)


def test_monotonic_only_entry_is_rejected(tmp_path: Path) -> None:
    """只有单调读数的条目进不了 journal 时间轴：以前会被写成 realtime_ns 为空的记录。"""
    path = write(
        tmp_path,
        "mono-only.export",
        (
            f"__MONOTONIC_TIMESTAMP={MONOTONIC_MICROS}\n"
            "_BOOT_ID=abc7451f65c84be8ab014b9a1e49a8b9\n"
            "_HOSTNAME=uos-PC\n"
            "MESSAGE=x\n\n"
        ).encode(),
    )

    with pytest.raises(PluginInputError, match="__REALTIME_TIMESTAMP"):
        archive.build_bundle(archive.parse_journal_archive(path), source_sha256="0" * 64)
