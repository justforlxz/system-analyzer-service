"""远程 Journal 采集插件的单元测试（任务 3.5）。

假远程入口按命令内容模拟目标机器：``cat`` 返回 boot id、``mktemp`` 建目录、
``journalctl`` 按 ``--after-cursor`` 过滤条目并写到远端文件、``rm -rf`` 清理。
这样单位测试既不连真机，也能断言插件实际发出去的命令。
"""

from __future__ import annotations

import inspect
import json
import shlex
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from system_analyzer.analyzers.oom import detect_kill
from system_analyzer.collectors.journal import (
    PLUGIN_ID,
    JournalCollector,
    build_export_command,
    journal_entities,
)
from system_analyzer.domain.bootrec import (
    ENTITY_MODELS,
    BootEntity,
    GenericEntity,
    MachineEntity,
    Record,
)
from system_analyzer.importers.journal_archive import build_bundle, detect, parse_journal_archive
from system_analyzer.plugins.builtins import register_builtins
from system_analyzer.plugins.contracts import (
    CollectorPlugin,
    CollectRequest,
    PluginContext,
    PluginExecutionError,
    PluginInputError,
    PluginKind,
)
from system_analyzer.plugins.registry import PluginRegistry
from system_analyzer.remote.errors import SshConnectError

BOOT_ID = "3f1c9a52-8f0e-4a10-9b1c-2b7d5c3a6e11"
BOOT_HEX = BOOT_ID.replace("-", "")
MACHINE_ID = "machine:uos-PC"
COLLECTION_ID = "collection:test-0001"
RUN_ID = "run-3-5"


class RecordingDataAccess:
    """只为断言写的同步数据入口。"""

    def __init__(self) -> None:
        self.records: list[Any] = []
        self.writes: list[tuple[str, str]] = []

    def append_records(
        self,
        records: Sequence[Any],
        *,
        plugin_id: str,
        source: str,
    ) -> int:
        self.records.extend(records)
        self.writes.append((plugin_id, source))
        return len(records)

    def query_records(
        self,
        *,
        record_kinds: Sequence[str] = (),
        limit: int | None = None,
    ) -> Sequence[Any]:
        return tuple(self.records)


class FakeJournalRemote:
    """按 shell 命令模拟一台只有 Journal 的目标机器。"""

    def __init__(self, entries: Sequence[Mapping[str, str]]) -> None:
        self.entries = list(entries)
        self.commands: list[str] = []
        self.downloads: list[str] = []
        self.files: dict[str, bytes] = {}
        self.directories: set[str] = set()
        self.fail_with: Exception | None = None

    def run(self, target_id: str, command: str, *, timeout: float | None = None) -> str:
        self.commands.append(command)
        if self.fail_with is not None:
            raise self.fail_with
        if command.startswith("cat "):
            return f"{BOOT_ID}\n"
        if command.startswith("mktemp -d "):
            directory = "/tmp/sas-journal.fake"
            self.directories.add(directory)
            return f"{directory}\n"
        if command.startswith("journalctl "):
            shell, _, target = command.partition(" > ")
            arguments = shlex.split(shell)
            after = next(
                (
                    argument.removeprefix("--after-cursor=")
                    for argument in arguments
                    if argument.startswith("--after-cursor=")
                ),
                None,
            )
            selected = self.entries
            if after is not None:
                position = next(
                    (
                        index
                        for index, entry in enumerate(self.entries)
                        if entry["__CURSOR"] == after
                    ),
                    None,
                )
                selected = [] if position is None else self.entries[position + 1 :]
            path = shlex.split(target)[0]
            self.files[path] = _render(selected).encode()
            return ""
        if command.startswith("rm -rf "):
            path = shlex.split(command)[2]
            self.files.pop(f"{path}/journal.export", None)
            self.directories.discard(path)
            return ""
        raise AssertionError(f"假远程入口不认识的命令：{command}")

    def download(self, target_id: str, remote_path: str, local_path: Any) -> None:
        self.downloads.append(remote_path)
        local_path.write_bytes(self.files[remote_path])


def _entry(
    index: int,
    *,
    message: str | None = None,
    extra: Mapping[str, str] | None = None,
    monotonic_us: int | None = None,
) -> dict[str, str]:
    monotonic = monotonic_us if monotonic_us is not None else 1_800_000_000 + index * 1_000_000
    fields = {
        "__CURSOR": f"cursor-{index}",
        "__REALTIME_TIMESTAMP": str(1_794_367_420_000_000 + index * 1_000_000),
        "__MONOTONIC_TIMESTAMP": str(monotonic),
        "_BOOT_ID": BOOT_HEX,
        "_HOSTNAME": "uos-PC",
        "_KERNEL_VERSION": "6.6.0-amd64-desktop",
        "PRIORITY": "6",
        "MESSAGE": message or f"journal entry {index}",
    }
    if extra:
        fields.update(extra)
    return fields


def _render(entries: Sequence[Mapping[str, str]]) -> str:
    blocks = [
        "\n".join(f"{name}={value}" for name, value in entry.items()) + "\n\n" for entry in entries
    ]
    return "".join(blocks)


def _context(plugin: JournalCollector, data: RecordingDataAccess) -> PluginContext:
    return PluginContext(plugin_id=plugin.spec.id, data=data, run_id=RUN_ID)


def _collect(
    tmp_path: Any,
    *,
    entries: Sequence[Mapping[str, str]],
    options: Mapping[str, Any] | None = None,
) -> tuple[JournalCollector, FakeJournalRemote, RecordingDataAccess, Any]:
    remote = FakeJournalRemote(entries)
    plugin = JournalCollector(remote_factory=lambda _options: remote)
    data = RecordingDataAccess()
    request = CollectRequest(
        target_id="uos-152",
        run_id=RUN_ID,
        options={
            "collection_id": COLLECTION_ID,
            "machine_id": MACHINE_ID,
            "work_dir": str(tmp_path),
            **(options or {}),
        },
    )
    result = plugin.collect(request, _context(plugin, data))
    return plugin, remote, data, result


def test_collector_contract_is_synchronous_and_signature_matches() -> None:
    plugin = JournalCollector()

    assert isinstance(plugin, CollectorPlugin)
    assert plugin.spec.kind is PluginKind.COLLECTOR
    assert plugin.spec.id == PLUGIN_ID
    assert any("journal" in capability for capability in plugin.spec.capabilities)
    assert inspect.iscoroutinefunction(JournalCollector.collect) is False
    assert list(inspect.signature(JournalCollector.collect).parameters) == [
        "self",
        "request",
        "context",
    ]
    plugin.spec.validate()


def test_export_command_scopes_boot_and_quotes_window(tmp_path: Any) -> None:
    command = build_export_command(
        BOOT_ID,
        after_cursor="s=abc;i=1;b=def",
        since="2026-11-12 10:00:00",
        until="2026-11-12 10:05:00",
    )
    arguments = shlex.split(command)

    assert f"_BOOT_ID={BOOT_HEX}" in arguments
    assert "--after-cursor=s=abc;i=1;b=def" in arguments
    assert "--since=2026-11-12 10:00:00" in arguments
    assert "--until=2026-11-12 10:05:00" in arguments
    assert "-o" in arguments and "export" in arguments


def test_collect_reads_boot_id_exports_and_leaves_cursor(tmp_path: Any) -> None:
    entries = [_entry(index) for index in range(1, 4)]
    _plugin, remote, data, result = _collect(tmp_path, entries=entries)

    assert result.records_written == 3
    assert data.writes == [(PLUGIN_ID, RUN_ID)]
    assert all(record["boot_id"] == BOOT_ID for record in data.records)
    assert all(record["collection_id"] == COLLECTION_ID for record in data.records)
    assert all(record["machine_id"] == MACHINE_ID for record in data.records)
    assert all(record["source_plugin"] == PLUGIN_ID for record in data.records)
    assert all(record["stream_id"] == "journal" for record in data.records)

    export = tmp_path / f"journal-{RUN_ID}.export"
    entities_file = tmp_path / f"journal-{RUN_ID}.entities.jsonl"
    cursor_file = tmp_path / f"journal-{RUN_ID}.cursor.json"
    assert result.artifacts == (str(export), str(entities_file), str(cursor_file))
    assert detect(export) is True
    payload = json.loads(cursor_file.read_text(encoding="utf-8"))
    assert payload == {
        "plugin_id": PLUGIN_ID,
        "target_id": "uos-152",
        "run_id": RUN_ID,
        "collection_id": COLLECTION_ID,
        "machine_id": MACHINE_ID,
        "boot_id": BOOT_ID,
        "cursor": "cursor-3",
        "after_cursor": None,
        "since": None,
        "until": None,
        "entry_count": 3,
        "source_sha256": payload["source_sha256"],
        "export_file": export.name,
        "entities_file": entities_file.name,
    }
    assert len(payload["source_sha256"]) == 64
    assert any(f"_BOOT_ID={BOOT_HEX}" in command for command in remote.commands)
    assert not any("--after-cursor" in command for command in remote.commands)
    assert any(command.startswith("rm -rf ") for command in remote.commands)
    assert remote.directories == set()


def _entity_rows(path: Any) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def test_entities_artifact_covers_every_referenced_entity_and_matches_collection(
    tmp_path: Any,
) -> None:
    entries = [
        _entry(1, extra={"_SYSTEMD_UNIT": "firefox.service"}),
        _entry(2, extra={"_SYSTEMD_UNIT": "user@1000.service"}),
        _entry(3),
    ]
    _, _, data, result = _collect(tmp_path, entries=entries)

    entities_file = tmp_path / f"journal-{RUN_ID}.entities.jsonl"
    assert str(entities_file) in result.artifacts
    rows = _entity_rows(entities_file)
    entities = [ENTITY_MODELS[row["entity_type"]].model_validate(row) for row in rows]
    by_id = {entity.entity_id: entity for entity in entities if hasattr(entity, "entity_id")}

    machine = next(entity for entity in entities if isinstance(entity, MachineEntity))
    boot = next(entity for entity in entities if isinstance(entity, BootEntity))
    assert machine.machine_id == MACHINE_ID
    assert boot.machine_id == MACHINE_ID
    assert boot.boot_id == BOOT_ID

    referenced = {record["entity"] for record in data.records if record["entity"] is not None}
    assert referenced == {"service:firefox.service", "service:user@1000.service"}
    assert referenced <= by_id.keys()
    for entity_id in referenced:
        entity = by_id[entity_id]
        assert isinstance(entity, GenericEntity)
        assert entity.entity_type == "service"
        assert entity.machine_id == MACHINE_ID
        assert entity.boot_id == BOOT_ID


def test_journal_entities_are_rebuilt_for_the_current_collection_scope(tmp_path: Any) -> None:
    export = tmp_path / "sample.export"
    export.write_text(_render([_entry(1, extra={"_SYSTEMD_UNIT": "firefox.service"})]))
    bundle = build_bundle(parse_journal_archive(export), source_sha256="0" * 64)
    other_boot = "11111111-2222-4333-8444-555555555555"

    entities = journal_entities(bundle, machine_id=MACHINE_ID, boot_id=other_boot)
    machine = next(entity for entity in entities if isinstance(entity, MachineEntity))
    boot = next(entity for entity in entities if isinstance(entity, BootEntity))
    service = next(entity for entity in entities if isinstance(entity, GenericEntity))

    assert machine.machine_id == MACHINE_ID
    assert machine.hostname == "uos-PC"
    assert boot.machine_id == MACHINE_ID
    assert boot.boot_id == other_boot
    assert service.entity_id == "service:firefox.service"
    assert service.machine_id == MACHINE_ID
    assert service.boot_id == other_boot


def test_two_incremental_collections_do_not_overlap_or_skip(tmp_path: Any) -> None:
    first = [_entry(index) for index in range(1, 4)]
    later = [_entry(index) for index in range(4, 6)]
    remote = FakeJournalRemote(first)
    plugin = JournalCollector(remote_factory=lambda _options: remote)
    data = RecordingDataAccess()

    def collect(options: Mapping[str, Any]) -> int:
        request = CollectRequest(
            target_id="uos-152",
            run_id=RUN_ID,
            options={
                "collection_id": COLLECTION_ID,
                "machine_id": MACHINE_ID,
                "work_dir": str(tmp_path),
                **options,
            },
        )
        return plugin.collect(request, _context(plugin, data)).records_written

    assert collect({}) == 3
    first_records = list(data.records)
    remote.entries.extend(later)
    assert collect({"after_cursor": "cursor-3"}) == 2
    second_records = data.records[len(first_records) :]

    cursors = [record["payload"]["attributes"]["cursor"] for record in data.records]
    assert cursors == ["cursor-1", "cursor-2", "cursor-3", "cursor-4", "cursor-5"]
    assert len(set(cursors)) == 5
    assert set(cursors[:3]).isdisjoint(cursors[3:])
    assert any("--after-cursor=cursor-3" in command for command in remote.commands)

    full_remote = FakeJournalRemote([*first, *later])
    full_plugin = JournalCollector(remote_factory=lambda _options: full_remote)
    full_data = RecordingDataAccess()
    full_plugin.collect(
        CollectRequest(
            target_id="uos-152",
            run_id=RUN_ID,
            options={
                "collection_id": COLLECTION_ID,
                "machine_id": MACHINE_ID,
                "work_dir": str(tmp_path),
            },
        ),
        _context(full_plugin, full_data),
    )
    first_ids = {record["record_id"] for record in first_records}
    second_ids = {record["record_id"] for record in second_records}
    full_ids = {record["record_id"] for record in full_data.records}
    assert first_ids.isdisjoint(second_ids)
    assert first_ids | second_ids == full_ids


def test_original_journal_fields_survive_and_kernel_oom_is_detected(tmp_path: Any) -> None:
    oom = _entry(
        1,
        message=(
            "Out of memory: Killed process 4310 (firefox) total-vm:2846920kB, "
            "anon-rss:1804808kB, file-rss:1024kB"
        ),
        extra={
            "_TRANSPORT": "kernel",
            "SYSLOG_IDENTIFIER": "kernel",
            "UNIT": "firefox.service",
            "USER_UNIT": "user@1000.service",
            "_SYSTEMD_UNIT": "firefox.service",
            "_SYSTEMD_CGROUP": "/system.slice/firefox.service",
            "INVOCATION_ID": "deadbeefdeadbeefdeadbeefdeadbeef",
        },
    )
    _, _, data, result = _collect(tmp_path, entries=[oom])

    assert result.records_written == 1
    record = Record.model_validate(data.records[0])
    attributes = record.payload["attributes"]
    fields = attributes["fields"]
    assert fields["_TRANSPORT"] == "kernel"
    assert fields["SYSLOG_IDENTIFIER"] == "kernel"
    assert fields["UNIT"] == "firefox.service"
    assert fields["USER_UNIT"] == "user@1000.service"
    assert fields["_SYSTEMD_UNIT"] == "firefox.service"
    assert fields["INVOCATION_ID"] == "deadbeefdeadbeefdeadbeefdeadbeef"
    assert attributes["unit"] == "firefox.service"

    kill = detect_kill(record)
    assert kill is not None
    assert kill.source == "kernel"
    assert kill.pid == 4310
    assert kill.process == "firefox"


def test_empty_window_writes_only_cursor(tmp_path: Any) -> None:
    _, _, data, result = _collect(tmp_path, entries=[], options={"after_cursor": "cursor-old"})

    assert result.records_written == 0
    assert data.records == []
    cursor_file = tmp_path / f"journal-{RUN_ID}.cursor.json"
    assert result.artifacts == (str(cursor_file),)
    payload = json.loads(cursor_file.read_text(encoding="utf-8"))
    assert payload["cursor"] == "cursor-old"
    assert payload["entry_count"] == 0


def test_missing_collection_id_is_a_plugin_input_error() -> None:
    plugin = JournalCollector(remote_factory=lambda _options: FakeJournalRemote([]))
    request = CollectRequest(target_id="uos-152", run_id=RUN_ID, options={})

    with pytest.raises(PluginInputError, match="collection_id"):
        plugin.collect(request, _context(plugin, RecordingDataAccess()))


def test_default_remote_requires_database_url() -> None:
    plugin = JournalCollector()
    request = CollectRequest(
        target_id="uos-152",
        run_id=RUN_ID,
        options={"collection_id": COLLECTION_ID},
    )

    with pytest.raises(PluginInputError, match="database_url"):
        plugin.collect(request, _context(plugin, RecordingDataAccess()))


class ExplodingRemote:
    def run(self, target_id: str, command: str, *, timeout: float | None = None) -> str:
        raise ValueError("boom")

    def download(self, target_id: str, remote_path: str, local_path: Any) -> None:
        raise AssertionError("不应该走到下载")


def test_unexpected_exception_is_wrapped_by_registry() -> None:
    registry = PluginRegistry()
    plugin = JournalCollector(remote_factory=lambda _options: ExplodingRemote())
    registry.register(plugin)
    request = CollectRequest(
        target_id="uos-152",
        run_id=RUN_ID,
        options={"collection_id": COLLECTION_ID},
    )

    with pytest.raises(PluginExecutionError) as caught:
        registry.invoke(
            plugin.spec.id,
            lambda loaded, context: loaded.collect(request, context),
            data=RecordingDataAccess(),
            run_id=RUN_ID,
        )

    assert isinstance(caught.value.__cause__, ValueError)


def test_remote_connection_failure_is_retryable() -> None:
    remote = FakeJournalRemote([])
    remote.fail_with = SshConnectError("目标不可达", host="10.0.0.1", port=22)
    plugin = JournalCollector(remote_factory=lambda _options: remote)
    request = CollectRequest(
        target_id="uos-152",
        run_id=RUN_ID,
        options={"collection_id": COLLECTION_ID},
    )

    from system_analyzer.plugins.contracts import PluginUnavailableError

    with pytest.raises(PluginUnavailableError):
        plugin.collect(request, _context(plugin, RecordingDataAccess()))


def test_builtin_registry_contains_the_journal_collector() -> None:
    registry = register_builtins(PluginRegistry())

    collector_ids = [plugin.spec.id for plugin in registry.by_kind(PluginKind.COLLECTOR)]
    assert PLUGIN_ID in collector_ids
    assert isinstance(registry.require(PLUGIN_ID), JournalCollector)
