"""``.bootrec`` 的解析、校验与标准化。

本模块只做「读文件 → 校验 → 变成 Core 的领域对象」这一段，不碰数据库、不写任何数据。
校验复用 0.2 交付的 ``domain.bootrec.validate_bootrec``：容器、清单、数据文件、交叉引用四层
检查与 ``docs/bootrec-v1.md`` 第 11 节的错误码都在那里，本模块不另写一套解析与校验，
只在校验通过之后把三个 ``.jsonl`` 读成领域对象。

时间字段不做换算：``journal.jsonl`` 的 ``monotonic_ns`` / ``realtime_ns`` 已经是纳秒
（``docs/bootrec-v1.md`` 第 5 节的字段表，换算细则见 ``docs/time-base.md`` 第 5 节），
导入方照原值搬运，不改单位、不补低位。

第一级幂等键在这里算（``docs/data-model.md`` 第 8 节）：对容器内每个条目按名字排序，依次喂入
「名字长度 + 名字 + 内容长度 + 内容」的 sha256。用条目而不是文件本身，是因为 ZIP 自身的时间戳、
压缩等级、权限位不参与校验和，同一份数据重新打包必须得到同一个摘要。长度写成 8 字节大端字节数。
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from system_analyzer.domain.bootrec import (
    ARTIFACTS_PREFIX,
    ENTITIES_NAME,
    ENTITY_MODELS,
    JOURNAL_NAME,
    MANIFEST_NAME,
    RECORDS_NAME,
    CollectionRef,
    Issue,
    JournalEntry,
    Manifest,
    ResourceAvailability,
    ValidationReport,
    jsonl_lines,
    validate_bootrec,
)
from system_analyzer.plugins.contracts import ImportSource, PluginInputError

SUFFIX = ".bootrec"
"""``.bootrec`` 文件的扩展名，``detect`` 的第一条判据。"""

ZIP_MEDIA_TYPE = "application/zip"
"""``.bootrec`` 的媒体类型：它本身就是一个 ZIP 容器（``docs/bootrec-v1.md`` 第 1 节）。"""

ZIP_MAGIC = b"PK\x03\x04"
"""ZIP 局部文件头的魔数，用来在没有扩展名时认出 ZIP。"""

DIGEST_LENGTH_SIZE = 8
"""摘要里「名字长度」与「内容长度」占的字节数（大端）。规范只说长度，没规定写法：
本仓库的导入方统一用 8 字节大端，跨实现比对摘要时需要与此一致。"""


def is_bootrec(source: ImportSource) -> bool:
    """这个来源是不是 ``.bootrec``。只读扩展名、媒体类型与前 4 个字节，不解析内容。"""
    if source.path.suffix.lower() == SUFFIX:
        return True
    media_type = (source.media_type or "").split(";", 1)[0].strip().lower()
    if media_type != ZIP_MEDIA_TYPE:
        return False
    return _has_zip_magic(source.path)


def container_digest(entries: Iterable[tuple[str, bytes]]) -> str:
    """整个容器的摘要，用作第一级幂等键（``docs/data-model.md`` 第 8 节）。

    条目按名字排序后逐个参与计算，因此条目在 ZIP 里的物理顺序、压缩方式、时间戳都不影响结果。
    """
    digest = hashlib.sha256()
    for name, content in sorted(entries, key=lambda item: item[0]):
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(DIGEST_LENGTH_SIZE, "big"))
        digest.update(encoded)
        digest.update(len(content).to_bytes(DIGEST_LENGTH_SIZE, "big"))
        digest.update(content)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class PreparedBootrec:
    """一份通过校验、可以直接交给存储层的 ``.bootrec``。

    ``record_lines`` 保留文件里的原始字典而不是 ``Record`` 模型：``record_id`` 是按原始内容算的
    （含本主版本不认识的字段），写库时要用同一份内容核对摘要。
    """

    path: Path
    digest: str
    collection: CollectionRef
    resources: ResourceAvailability
    entities: tuple[BaseModel, ...]
    record_lines: tuple[dict[str, Any], ...]
    journal_entries: tuple[JournalEntry, ...]
    artifacts: tuple[str, ...]
    warnings: tuple[Issue, ...]


def prepare_bootrec(path: str | Path) -> PreparedBootrec:
    """校验一份 ``.bootrec`` 并把它读成领域对象；不合法时整份拒绝。

    先调 ``validate_bootrec`` 跑完整套校验：任何 ``error`` 都让导入失败并报出错误码与位置，
    与 ``docs/bootrec-v1.md`` 第 10 节「遇到 ``error`` 就整份拒绝」一致。只有 ``warning``
    （例如次版本更高、容器里有没被引用的原始文件）随 ``PreparedBootrec.warnings`` 一起返回，
    不阻断导入。
    """
    target = Path(path)
    report = validate_bootrec(target)
    if not report.ok:
        raise PluginInputError(_rejection_message(report))

    blobs, artifacts = _read_container(target)
    try:
        manifest = Manifest.model_validate_json(blobs[MANIFEST_NAME])
        entities = tuple(_entity_rows(blobs[ENTITIES_NAME]))
        record_lines = tuple(_record_rows(blobs[RECORDS_NAME]))
        journal_entries = tuple(_journal_rows(blobs[JOURNAL_NAME]))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
        # 校验刚通过却读不出来，只可能是文件在校验之后被改动或读取时损坏。
        raise PluginInputError(f"{target} 校验通过后重新读取失败，文件可能已被改动：{exc}") from exc

    return PreparedBootrec(
        path=target,
        digest=container_digest(blobs.items()),
        collection=manifest.collection,
        resources=manifest.resources,
        entities=entities,
        record_lines=record_lines,
        journal_entries=journal_entries,
        artifacts=artifacts,
        warnings=tuple(report.warnings),
    )


def _has_zip_magic(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(len(ZIP_MAGIC)) == ZIP_MAGIC
    except OSError:
        return False


def _rejection_message(report: ValidationReport) -> str:
    """把校验报告写成一条可读的拒绝原因：先给结论，再逐条列出错误码、位置与说明。"""
    lines = [f"{report.path} 不符合 bootrec v1 规范，整份拒绝（{len(report.errors)} 个错误）："]
    lines.extend(f"- {issue.format()}" for issue in report.errors)
    if report.warnings:
        lines.append(f"另有 {len(report.warnings)} 个警告，不是拒绝的原因")
    return "\n".join(lines)


def _read_container(target: Path) -> tuple[dict[str, bytes], tuple[str, ...]]:
    """读出容器内的全部文件条目。

    目录条目（名字以 ``/`` 结尾）没有内容，不参与摘要也不返回；``artifacts/`` 下的文件单独列出，
    供任务结果说明这份包带了哪些原始文件。
    """
    try:
        with zipfile.ZipFile(target) as archive:
            entries = {
                name: archive.read(name) for name in archive.namelist() if not name.endswith("/")
            }
    except (
        OSError,
        zipfile.BadZipFile,
        NotImplementedError,
        RuntimeError,
        ValueError,
    ) as exc:
        raise PluginInputError(f"{target} 重新读取失败，文件可能已被移动或损坏：{exc}") from exc
    artifacts = tuple(sorted(name for name in entries if name.startswith(ARTIFACTS_PREFIX)))
    return entries, artifacts


def _entity_rows(blob: bytes) -> list[BaseModel]:
    entities: list[BaseModel] = []
    for _, raw in jsonl_lines(blob):
        payload = json.loads(raw.decode("utf-8"))
        model = ENTITY_MODELS[payload["entity_type"]]
        entities.append(model.model_validate(payload))
    return entities


def _record_rows(blob: bytes) -> list[dict[str, Any]]:
    return [json.loads(raw.decode("utf-8")) for _, raw in jsonl_lines(blob)]


def _journal_rows(blob: bytes) -> list[JournalEntry]:
    return [JournalEntry.model_validate_json(raw) for _, raw in jsonl_lines(blob)]
