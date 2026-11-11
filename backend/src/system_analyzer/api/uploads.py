"""把上传文件写到磁盘，以及按内容挑一个导入插件。"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import UploadFile

from system_analyzer.api.errors import invalid_format, invalid_parameter
from system_analyzer.api.sync_store import SyncStoreAccess
from system_analyzer.plugins.contracts import ImportSource, PluginKind
from system_analyzer.plugins.registry import PluginRegistry

CHUNK_SIZE = 64 * 1024
"""上传文件每次读多少字节。分块是为了大文件不整份进内存。"""

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
MAX_NAME_LENGTH = 80


class UploadTooLargeError(Exception):
    """上传文件超过配置的大小上限。"""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(f"文件超过上传上限 {limit} 字节")


@dataclass(frozen=True, slots=True)
class SavedUpload:
    """一个已经写到磁盘的上传文件。"""

    upload_id: str
    path: Path
    filename: str | None
    size_bytes: int
    source_sha256: str
    """整份文件的 sha256。导入方把它当第一级幂等键（data-model 第 8 节）。"""


async def save_upload(file: UploadFile, directory: Path, *, max_bytes: int) -> SavedUpload:
    """把上传文件写到 ``directory``，边写边算 sha256，超过 ``max_bytes`` 立即中止。

    文件名由「随机 upload_id + 安全化后的原名」拼成：同名文件多次上传不会互相覆盖，
    调用方给的名字也不会带出目录分隔符（``../`` 这类写法在安全化时已经被替换掉）。

    超过上限时由 ``_copy_with_digest`` 删掉已写入的那部分再抛 ``UploadTooLargeError``，
    不会在磁盘上留下半份文件。
    """
    await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
    upload_id = uuid4().hex
    filename = file.filename
    target = directory / f"{upload_id}-{_safe_name(filename)}"
    # 写文件与算摘要放到工作线程：大文件写成百 MiB，不该占住处理请求的事件循环。
    size_bytes, source_sha256 = await asyncio.to_thread(
        _copy_with_digest, file.file, target, max_bytes
    )
    return SavedUpload(
        upload_id=upload_id,
        path=target,
        filename=filename,
        size_bytes=size_bytes,
        source_sha256=source_sha256,
    )


def discard_upload(path: Path) -> None:
    """删掉这次上传写入的文件：格式不被接受或任务入队失败时调用，避免留下垃圾文件。"""
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def _copy_with_digest(source: Any, target: Path, max_bytes: int) -> tuple[int, str]:
    """把 ``source`` 拷到 ``target``，同时算 sha256。返回（字节数，十六进制摘要）。

    上限按「写入过程中累计的字节数」判断，而不是写完再看大小：后者要先把磁盘写满才发现。
    任何失败（超限、磁盘写满、读取中断）都删掉半份文件，再抛给调用方。
    """
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        with target.open("wb") as handle:
            while chunk := source.read(CHUNK_SIZE):
                size_bytes += len(chunk)
                if size_bytes > max_bytes:
                    raise UploadTooLargeError(max_bytes)
                digest.update(chunk)
                handle.write(chunk)
    except BaseException:
        discard_upload(target)
        raise
    return size_bytes, digest.hexdigest()


def resolve_importer(
    registry: PluginRegistry,
    source: ImportSource,
    *,
    plugin_id: str | None,
    data: SyncStoreAccess,
    display_name: str,
) -> str:
    """确定用哪个导入插件处理这个文件，返回插件 id。

    指定了 ``plugin_id`` 就直接校验它；没指定就按注册顺序问每个导入插件的 ``detect``。
    一个都不认得时按「格式错误」拒绝，并列出已注册的导入插件。

    检测本身经由 ``PluginRegistry.invoke`` 调用，所以插件在里面抛的任何异常都会被包成
    ``PluginExecutionError``，最终表现为内部错误，而不是让请求线程裸崩。
    """
    if plugin_id is not None:
        plugin = registry.require(plugin_id)
        if plugin.spec.kind is not PluginKind.IMPORTER:
            raise invalid_parameter(
                f"插件 {plugin_id} 不是导入插件，它的类别是 {plugin.spec.kind.value}"
            )
        if not _detect(registry, plugin_id, source, data):
            raise invalid_format(f"插件 {plugin_id} 不认这个文件：{display_name}")
        return plugin_id

    for plugin in registry.by_kind(PluginKind.IMPORTER):
        if _detect(registry, plugin.spec.id, source, data):
            return plugin.spec.id
    registered = [plugin.spec.id for plugin in registry.by_kind(PluginKind.IMPORTER)]
    raise invalid_format(
        f"没有导入插件认得这个文件：{display_name}",
        details=[
            f"已注册的导入插件：{'、'.join(registered)}" if registered else "没有注册任何导入插件"
        ],
    )


def _detect(
    registry: PluginRegistry, plugin_id: str, source: ImportSource, data: SyncStoreAccess
) -> bool:
    return registry.invoke(
        plugin_id,
        lambda plugin, context: plugin.detect(source),
        data=data,
    )


def _safe_name(name: str | None) -> str:
    """只保留字母、数字、点、下划线与连字符，并限长。"""
    candidate = Path(name or "").name
    candidate = _UNSAFE_NAME.sub("_", candidate).strip("._")
    return candidate[:MAX_NAME_LENGTH] or "upload"
