"""回传重试与远端保留的单元测试（任务 3.7）。

用可控的假远端连接模拟「断网 → 远端文件仍在 → 恢复后重新下载」，并核对：
- 下载失败不会删除远端文件；
- 重试成功后本地文件与远端内容一致；
- 导入没有确认成功时，清理动作根本不发出去。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from system_analyzer.collection.transfer import (
    pull_with_retry,
    remove_remote_file,
    stage_remote_file,
)
from system_analyzer.remote.client import CommandResult
from system_analyzer.remote.errors import SshConnectError


class FlakyRemote:
    """一个会按次数失败的文件服务，模拟网络中断后恢复。"""

    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files = dict(files or {})
        self.fail_downloads = 0
        self.downloads: list[str] = []
        self.uploads: list[str] = []
        self.commands: list[str] = []

    async def download(self, remote_path: str, local_path: Path | str) -> None:
        if self.fail_downloads > 0:
            self.fail_downloads -= 1
            raise SshConnectError("测试注入：网络中断")
        if remote_path not in self.files:
            raise FileNotFoundError(remote_path)
        self.downloads.append(remote_path)
        Path(local_path).write_bytes(self.files[remote_path])  # noqa: ASYNC240  测试假实现

    async def upload(self, local_path: Path | str, remote_path: str) -> None:
        self.uploads.append(remote_path)
        self.files[remote_path] = Path(local_path).read_bytes()  # noqa: ASYNC240  测试假实现

    async def run(
        self,
        command: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109  与 SshClient 协议保持一致
        check: bool = True,
    ) -> CommandResult:
        self.commands.append(command)
        if command.startswith("rm -f -- "):
            path = command.removeprefix("rm -f -- ").strip("'")
            self.files.pop(path, None)
        return CommandResult(command=command, stdout="", stderr="", exit_status=0)


async def test_download_failure_keeps_remote_and_retry_succeeds(tmp_path: Path) -> None:
    remote_path = "/tmp/sas/delivery.bootrec"
    remote = FlakyRemote({remote_path: b"bootrec-bytes"})
    remote.fail_downloads = 2

    report = await pull_with_retry(
        remote,
        remote_path,
        tmp_path / "local.bootrec",
        attempts=3,
    )

    assert report.succeeded is True
    assert [attempt.succeeded for attempt in report.attempts] == [False, False, True]
    assert report.remote_retained is True
    assert remote.files[remote_path] == b"bootrec-bytes"
    assert (tmp_path / "local.bootrec").read_bytes() == b"bootrec-bytes"


async def test_exhausted_retries_leave_remote_untouched(tmp_path: Path) -> None:
    remote_path = "/tmp/sas/delivery.bootrec"
    remote = FlakyRemote({remote_path: b"bootrec-bytes"})
    remote.fail_downloads = 5

    report = await pull_with_retry(
        remote,
        remote_path,
        tmp_path / "local.bootrec",
        attempts=2,
    )

    assert report.succeeded is False
    assert len(report.attempts) == 2
    assert report.remote_retained is True
    assert remote.files[remote_path] == b"bootrec-bytes"
    assert remote.downloads == []
    assert not (tmp_path / "local.bootrec").exists()


async def test_failed_download_does_not_overwrite_existing_local_pack(tmp_path: Path) -> None:
    remote_path = "/tmp/sas/delivery.bootrec"
    local = tmp_path / "local.bootrec"
    local.write_bytes(b"locally-built-pack")
    remote = FlakyRemote({remote_path: b"remote-bytes"})
    remote.fail_downloads = 5

    report = await pull_with_retry(remote, remote_path, local, attempts=2)

    assert report.succeeded is False
    assert local.read_bytes() == b"locally-built-pack"
    assert not (tmp_path / "local.bootrec.part").exists()


async def test_cleanup_only_after_confirmed_import() -> None:
    remote_path = "/tmp/sas/delivery.bootrec"
    remote = FlakyRemote({remote_path: b"bootrec-bytes"})

    kept = await remove_remote_file(remote, remote_path, imported=False)
    assert kept is False
    assert remote.files[remote_path] == b"bootrec-bytes"
    assert remote.commands == []

    removed = await remove_remote_file(remote, remote_path, imported=True)
    assert removed is True
    assert remote_path not in remote.files


async def test_cleanup_network_failure_reports_not_removed() -> None:
    class BrokenRemote(FlakyRemote):
        async def run(
            self,
            command: str,
            *,
            timeout: float | None = None,  # noqa: ASYNC109  与 SshClient 协议保持一致
            check: bool = True,
        ) -> CommandResult:
            raise SshConnectError("测试注入：清理时网络中断")

    assert await remove_remote_file(BrokenRemote(), "/tmp/x", imported=True) is False


async def test_stage_remote_file_creates_directory_and_uploads(tmp_path: Path) -> None:
    local = tmp_path / "delivery.bootrec"
    local.write_bytes(b"pack")
    remote = FlakyRemote()

    await stage_remote_file(remote, local, "/tmp/sas/delivery.bootrec")

    assert remote.commands == ["mkdir -p -- /tmp/sas"]
    assert remote.uploads == ["/tmp/sas/delivery.bootrec"]
    assert remote.files["/tmp/sas/delivery.bootrec"] == b"pack"


def test_retry_arguments_are_validated(tmp_path: Path) -> None:
    remote = FlakyRemote()
    with pytest.raises(ValueError):
        import asyncio

        asyncio.run(pull_with_retry(remote, "/x", tmp_path / "x", attempts=0))
