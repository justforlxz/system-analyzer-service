"""bootrec 导入插件的单元测试：解析、校验、幂等键、复用判定与插件契约。

不碰数据库：写库入口用一个记录调用的假实现替身。真实 PostgreSQL 上的导入在
``tests/integration/test_bootrec_import_store.py``。
"""

from __future__ import annotations

import inspect
import zipfile
from pathlib import Path
from typing import Any

import pytest

from bootrec_fixtures import (
    FULL_SAMPLE,
    bad_checksum_package,
    count_mismatch_package,
    missing_entry_package,
    missing_field_package,
    newer_major_version_package,
    newer_minor_version_package,
    package_entries,
    reexported_package,
    valid_package,
    write_package,
)
from system_analyzer.domain.bootrec import CollectionMode, CollectionRef
from system_analyzer.importers.bootrec import (
    container_digest,
    is_bootrec,
    prepare_bootrec,
)
from system_analyzer.importers.bootrec_importer import (
    BootrecImporter,
    CollectionImportAccess,
    collection_reuse_problem,
    import_bootrec,
)
from system_analyzer.plugins.builtins import register_builtins
from system_analyzer.plugins.contracts import (
    ImporterPlugin,
    ImportResult,
    ImportSource,
    PluginContext,
    PluginExecutionError,
    PluginInputError,
    PluginKind,
)
from system_analyzer.plugins.registry import PluginRegistry
from system_analyzer.storage import CollectionState, ImportOutcome, ImportStatus

MACHINE_ID = "machine:uos-PC"
BOOT_ID = "3f1c9a52-8f0e-4a10-9b1c-2b7d5c3a6e11"
FULL_COLLECTION = "collection:full-0001"
FULL_ENTITIES = 7
FULL_STORED_ENTITIES = 5  # machine 与 boot 各占存储层自己的表
FULL_RECORDS = 17
FULL_JOURNAL = 3


class RecordingAccess:
    """数据入口的替身：记下读了哪个采集、写入了什么。"""

    def __init__(self, existing: CollectionState | None = None) -> None:
        self.existing = existing
        self.reads: list[str] = []
        self.calls: list[dict[str, Any]] = []

    def get_collection(self, collection_id: str) -> CollectionState | None:
        self.reads.append(collection_id)
        return self.existing

    def import_collection(
        self,
        collection: CollectionRef,
        entities: Any,
        records: Any,
        *,
        source_sha256: str | None = None,
        resources: Any = None,
    ) -> ImportOutcome:
        self.calls.append(
            {
                "collection": collection,
                "entities": list(entities),
                "records": list(records),
                "source_sha256": source_sha256,
                "resources": resources,
            }
        )
        return ImportOutcome(
            collection_id=collection.collection_id,
            status=ImportStatus.IMPORTED,
            records_written=len(records),
            entities_written=sum(
                1 for entity in entities if entity.entity_type not in ("machine", "boot")
            ),
        )


def stored_state(boot_id: str = BOOT_ID, digest: str | None = None) -> CollectionState:
    """库里已经存在这个采集号时 ``get_collection`` 的返回值。"""
    return CollectionState(
        collection=CollectionRef(
            collection_id=FULL_COLLECTION,
            machine_id=MACHINE_ID,
            boot_id=boot_id,
            mode=CollectionMode.SSH_COLLECTION,
            created_realtime_ns=1794009600000000000,
        ),
        import_status=ImportStatus.IMPORTED,
        import_error=None,
        source_sha256=digest,
        resources_available=True,
        resources_reason=None,
    )


# ---------------------------------------------------------------------------
# detect：只看扩展名、媒体类型与魔数
# ---------------------------------------------------------------------------


def test_detect_accepts_the_bootrec_suffix() -> None:
    assert is_bootrec(ImportSource(path=FULL_SAMPLE)) is True
    assert is_bootrec(ImportSource(path=Path("boot-1.BOOTREC"))) is True


def test_detect_accepts_zip_magic_with_zip_media_type(tmp_path: Path) -> None:
    path = write_package(tmp_path, package_entries(), "upload.zip")

    assert is_bootrec(ImportSource(path=path, media_type="application/zip")) is True
    assert is_bootrec(ImportSource(path=path, media_type="application/zip; charset=binary")) is True
    # 没有 ZIP 媒体类型时，扩展名不对就不认这个来源
    assert is_bootrec(ImportSource(path=path)) is False


def test_detect_rejects_a_file_that_is_not_a_zip(tmp_path: Path) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("这不是 ZIP", encoding="utf-8")

    assert is_bootrec(ImportSource(path=path, media_type="application/zip")) is False


# ---------------------------------------------------------------------------
# 解析：容器里的每一部分都读到
# ---------------------------------------------------------------------------


def test_prepare_reads_every_part_of_the_container() -> None:
    prepared = prepare_bootrec(FULL_SAMPLE)

    assert prepared.collection.collection_id == FULL_COLLECTION
    assert prepared.collection.mode is CollectionMode.SSH_COLLECTION
    assert prepared.resources.available is True
    assert len(prepared.entities) == FULL_ENTITIES
    assert len(prepared.record_lines) == FULL_RECORDS
    assert len(prepared.journal_entries) == FULL_JOURNAL
    assert prepared.artifacts == ("artifacts/perf.data",)
    assert prepared.warnings == ()
    assert len(prepared.digest) == 64


def test_prepare_keeps_journal_timestamps_in_nanoseconds() -> None:
    prepared = prepare_bootrec(FULL_SAMPLE.parent / "journal-only.bootrec")

    first = prepared.journal_entries[0]
    # journal.jsonl 里已经是纳秒（bootrec-v1 第 5 节），导入方不再乘 1000
    assert first.monotonic_ns == 1_200_000_000
    assert first.realtime_ns == 1_794_009_601_200_000_000
    assert first.boot_id == BOOT_ID


def test_record_lines_stay_raw_so_the_digest_can_be_rechecked() -> None:
    prepared = prepare_bootrec(FULL_SAMPLE)

    assert all(isinstance(line, dict) for line in prepared.record_lines)
    assert all(len(line["record_id"]) == 32 for line in prepared.record_lines)


# ---------------------------------------------------------------------------
# 第一级幂等键：按条目算，不受 ZIP 元数据与条目顺序影响
# ---------------------------------------------------------------------------


def test_digest_ignores_zip_metadata_and_entry_order(tmp_path: Path) -> None:
    entries = package_entries()
    reordered = dict(reversed(list(entries.items())))
    repacked = tmp_path / "repacked.bootrec"
    with zipfile.ZipFile(repacked, "w", zipfile.ZIP_STORED) as archive:
        for name, data in reordered.items():
            info = zipfile.ZipInfo(name, date_time=(2020, 1, 2, 3, 4, 6))
            archive.writestr(info, data)

    assert container_digest(package_entries().items()) == container_digest(
        _entries(repacked).items()
    )


def test_digest_changes_when_content_changes() -> None:
    base = package_entries()

    assert container_digest(base.items()) == container_digest(package_entries().items())
    assert container_digest(base.items()) != container_digest(
        {**base, "manifest.json": base["manifest.json"] + b" "}.items()
    )


def _entries(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


# ---------------------------------------------------------------------------
# 四类夹具：正常包导入，三类坏包整份拒绝且不碰存储
# ---------------------------------------------------------------------------


def test_valid_package_imports(tmp_path: Path) -> None:
    access = RecordingAccess()

    outcome = import_bootrec(access, valid_package(tmp_path))

    assert outcome.status == ImportStatus.IMPORTED.value
    assert outcome.records_written == FULL_RECORDS
    assert outcome.entities_written == FULL_STORED_ENTITIES
    assert outcome.journal_entries == FULL_JOURNAL
    assert outcome.artifacts == ("artifacts/perf.data",)
    assert outcome.skipped_reason is None
    assert len(access.calls) == 1


@pytest.mark.parametrize(
    ("build", "code"),
    [
        (missing_field_package, "E_MANIFEST_FIELD"),
        (missing_entry_package, "E_MISSING_ENTRY"),
        (count_mismatch_package, "E_COUNT_MISMATCH"),
        (bad_checksum_package, "E_SHA256_MISMATCH"),
        (newer_major_version_package, "E_UNSUPPORTED_VERSION"),
    ],
)
def test_broken_packages_are_rejected_before_any_write(
    tmp_path: Path, build: Any, code: str
) -> None:
    access = RecordingAccess()

    with pytest.raises(PluginInputError) as rejected:
        import_bootrec(access, build(tmp_path))

    assert code in str(rejected.value)
    assert "整体拒绝" in str(rejected.value) or "整份拒绝" in str(rejected.value)
    # 拒绝发生在读库之前：没有登记采集，也没有写任何记录
    assert access.calls == []
    assert access.reads == []


def test_newer_minor_version_is_a_warning_not_a_rejection(tmp_path: Path) -> None:
    prepared = prepare_bootrec(newer_minor_version_package(tmp_path))

    assert [issue.code for issue in prepared.warnings] == ["E_NEWER_MINOR_VERSION"]


# ---------------------------------------------------------------------------
# 幂等：摘要与记录唯一键两级都成立
# ---------------------------------------------------------------------------


def test_same_file_passes_the_container_digest_to_the_store(tmp_path: Path) -> None:
    path = valid_package(tmp_path)
    access = RecordingAccess()

    first = import_bootrec(access, path)
    second = import_bootrec(RecordingAccess(existing=stored_state(digest=first.digest)), path)

    assert access.calls[0]["source_sha256"] == first.digest
    # 同一个文件再导入一次：摘要一致，不产生「采集号被重复使用」的警告
    assert second.warnings == ()


def test_another_file_of_the_same_collection_is_imported_with_a_warning(
    tmp_path: Path,
) -> None:
    existing = stored_state(digest="f" * 64)
    access = RecordingAccess(existing=existing)

    outcome = import_bootrec(access, reexported_package(tmp_path))

    assert len(access.calls) == 1
    assert outcome.digest != existing.source_sha256
    assert any(FULL_COLLECTION in warning for warning in outcome.warnings)


def test_entity_and_resource_flags_reach_the_store(tmp_path: Path) -> None:
    access = RecordingAccess()
    prepared = prepare_bootrec(valid_package(tmp_path))

    import_bootrec(access, prepared.path)

    call = access.calls[0]
    assert call["collection"] == prepared.collection
    assert call["entities"] == list(prepared.entities)
    assert call["records"] == list(prepared.record_lines)
    assert call["resources"] == prepared.resources
    assert call["source_sha256"] == container_digest(_entries(prepared.path).items())


# ---------------------------------------------------------------------------
# collection_id 被另一台机器或另一次启动复用
# ---------------------------------------------------------------------------


def test_reuse_problem_is_silent_for_the_same_scope() -> None:
    collection = stored_state().collection

    assert collection_reuse_problem(None, collection) is None
    assert collection_reuse_problem(stored_state(), collection) is None


def test_collection_id_used_by_another_boot_is_rejected(tmp_path: Path) -> None:
    other_boot = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    access = RecordingAccess(existing=stored_state(boot_id=other_boot))

    with pytest.raises(PluginInputError) as rejected:
        import_bootrec(access, valid_package(tmp_path))

    assert FULL_COLLECTION in str(rejected.value)
    assert other_boot in str(rejected.value)
    assert BOOT_ID in str(rejected.value)
    assert access.calls == []


# ---------------------------------------------------------------------------
# 插件：契约、注册、通过数据入口导入
# ---------------------------------------------------------------------------


def test_plugin_satisfies_the_importer_contract() -> None:
    plugin = BootrecImporter()

    assert isinstance(plugin, ImporterPlugin)
    assert plugin.spec.kind is PluginKind.IMPORTER
    assert plugin.spec.capabilities == ("bootrec.v1",)
    plugin.spec.validate()


def test_plugin_is_registered_as_a_builtin() -> None:
    registry = register_builtins(PluginRegistry())

    # 内置清单是追加式的（1.4 的 journal.archive 与本插件并列），只断言本插件在其中
    assert "bootrec.importer" in [
        plugin.spec.id for plugin in registry.by_kind(PluginKind.IMPORTER)
    ]
    assert [plugin.spec.id for plugin in registry.by_capability("bootrec.v1")] == [
        "bootrec.importer"
    ]


def test_plugin_imports_through_the_data_access() -> None:
    plugin = BootrecImporter()
    access = RecordingAccess()
    context = PluginContext(plugin_id=plugin.spec.id, data=access)
    source = ImportSource(path=FULL_SAMPLE)

    assert plugin.detect(source) is True
    assert isinstance(access, CollectionImportAccess)
    # 契约 §3.3 把 import_source 定成同步方法：不是协程函数，直接返回 ImportResult
    assert inspect.iscoroutinefunction(plugin.import_source) is False
    result = plugin.import_source(source, context)
    assert isinstance(result, ImportResult)

    assert result.records_written == FULL_RECORDS
    assert len(access.calls) == 1


def test_plugin_refuses_a_source_it_does_not_recognise(tmp_path: Path) -> None:
    plugin = BootrecImporter()
    context = PluginContext(plugin_id=plugin.spec.id, data=RecordingAccess())

    with pytest.raises(PluginInputError):
        plugin.import_source(ImportSource(path=tmp_path / "notes.txt"), context)


def test_plugin_reports_a_data_access_without_the_import_entry(tmp_path: Path) -> None:
    plugin = BootrecImporter()
    context = PluginContext(plugin_id=plugin.spec.id, data=object())

    with pytest.raises(PluginInputError) as rejected:
        plugin.import_source(ImportSource(path=valid_package(tmp_path)), context)

    assert "import_collection" in str(rejected.value)


# ---------------------------------------------------------------------------
# 契约要求的调用形状：同步入口、经 registry.invoke 拿到 ImportResult、异常被收敛
# ---------------------------------------------------------------------------


class FailingAccess:
    """存储层故障的替身：读采集正常，写库时抛出一个非 PluginError 的异常。"""

    def get_collection(self, collection_id: str) -> None:
        return None

    def import_collection(self, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("QA 注入的存储层缺陷")


class AsyncAccess:
    """错配的数据入口：方法返回协程，而不是结果。"""

    async def get_collection(self, collection_id: str) -> None:
        return None

    async def import_collection(self, *args: Any, **kwargs: Any) -> None:
        return None


def test_plugin_entry_point_is_synchronous() -> None:
    """契约 §3.3：``detect`` 与 ``import_source`` 都是同步方法。"""
    plugin = BootrecImporter()

    assert inspect.iscoroutinefunction(plugin.detect) is False
    assert inspect.iscoroutinefunction(plugin.import_source) is False


def test_registry_invoke_returns_the_import_result() -> None:
    """契约 §4 的唯一调用入口直接拿到 ImportResult，而不是协程。"""
    registry = register_builtins(PluginRegistry())
    access = RecordingAccess()

    result = registry.invoke(
        "bootrec.importer",
        lambda plugin, context: plugin.import_source(ImportSource(path=FULL_SAMPLE), context),
        data=access,
    )

    assert isinstance(result, ImportResult)
    assert result.records_written == FULL_RECORDS


def test_storage_failure_is_wrapped_by_the_plugin_boundary() -> None:
    """插件抛出的非 PluginError 异常在 invoke 边界被包成 PluginExecutionError（§3.6、§4.4）。"""
    registry = register_builtins(PluginRegistry())

    with pytest.raises(PluginExecutionError) as wrapped:
        registry.invoke(
            "bootrec.importer",
            lambda plugin, context: plugin.import_source(ImportSource(path=FULL_SAMPLE), context),
            data=FailingAccess(),
        )

    assert isinstance(wrapped.value.cause, RuntimeError)
    assert "QA 注入的存储层缺陷" in str(wrapped.value.cause)


def test_an_async_data_access_is_rejected_with_a_readable_reason(tmp_path: Path) -> None:
    """数据入口返回协程时明确失败，而不是「成功」返回一个没导入任何东西的协程。"""
    plugin = BootrecImporter()
    context = PluginContext(plugin_id=plugin.spec.id, data=AsyncAccess())

    with pytest.raises(PluginInputError) as rejected:
        plugin.import_source(ImportSource(path=valid_package(tmp_path)), context)

    assert "协程" in str(rejected.value)
    assert "context.data" in str(rejected.value)
