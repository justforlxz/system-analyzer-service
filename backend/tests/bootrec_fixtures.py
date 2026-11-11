"""导入夹具：正常包、缺字段包、坏校验和包、版本过高包。

全部从仓库样例 ``docs/samples/bootrec-v1/full.bootrec`` 改写而来。样例是 0.2 交付的固定数据
（``python -m system_analyzer.tools.make_bootrec_samples`` 生成，内容可逐字节比对），因此夹具
与规范之间不会各说一套。生成物写到 ``tmp_path``，仓库里不放二进制文件。

另外两个夹具服务于幂等与版本规则：``reexported_package`` 只改 ``producer.version``（同一份采集
被重新导出，记录完全不变，摘要变化），``newer_minor_version_package`` 把次版本写成 1.9
（只报警告，不阻断）。
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

SAMPLE_DIR = Path(__file__).resolve().parents[2] / "docs" / "samples" / "bootrec-v1"
FULL_SAMPLE = SAMPLE_DIR / "full.bootrec"


def package_entries(sample: Path = FULL_SAMPLE) -> dict[str, bytes]:
    """读出样例的全部条目，作为改写夹具的起点。"""
    with zipfile.ZipFile(sample) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def write_package(tmp_path: Path, entries: dict[str, bytes], name: str = "fixture.bootrec") -> Path:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for entry, data in entries.items():
            archive.writestr(entry, data)
    return path


def manifest(entries: dict[str, bytes]) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(entries["manifest.json"])
    return payload


def set_manifest(entries: dict[str, bytes], payload: dict[str, Any]) -> None:
    entries["manifest.json"] = json.dumps(payload, ensure_ascii=False).encode("utf-8")


def record_rows(entries: dict[str, bytes]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in entries["records.jsonl"].split(b"\n") if line.strip()]


def valid_package(tmp_path: Path) -> Path:
    """正常包：与仓库样例逐字节相同。"""
    return write_package(tmp_path, package_entries(), "valid.bootrec")


def missing_field_package(tmp_path: Path) -> Path:
    """缺字段包：``manifest.json`` 少了必填的 ``producer`` 段。"""
    entries = package_entries()
    payload = manifest(entries)
    del payload["producer"]
    set_manifest(entries, payload)
    return write_package(tmp_path, entries, "missing-field.bootrec")


def bad_checksum_package(tmp_path: Path) -> Path:
    """坏校验和包：改了 ``records.jsonl`` 却没有更新 manifest 里登记的 sha256。"""
    entries = package_entries()
    rows = record_rows(entries)
    rows[0]["payload"]["severity"] = "critical"
    entries["records.jsonl"] = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
    ).encode("utf-8")
    return write_package(tmp_path, entries, "bad-checksum.bootrec")


def newer_major_version_package(tmp_path: Path) -> Path:
    """版本过高包：``format_version`` 是 2.0，主版本不受支持。"""
    entries = package_entries()
    payload = manifest(entries)
    payload["format_version"] = "2.0"
    set_manifest(entries, payload)
    return write_package(tmp_path, entries, "newer-major.bootrec")


def newer_minor_version_package(tmp_path: Path) -> Path:
    """次版本更高包：``format_version`` 是 1.9，按 1.0 读取，只报警告。"""
    entries = package_entries()
    payload = manifest(entries)
    payload["format_version"] = "1.9"
    set_manifest(entries, payload)
    return write_package(tmp_path, entries, "newer-minor.bootrec")


def reexported_package(tmp_path: Path) -> Path:
    """同一份采集被重新导出：只改 ``producer.version``，记录与实体完全不变。"""
    entries = package_entries()
    payload = manifest(entries)
    payload["producer"]["version"] = "0.2.1"
    set_manifest(entries, payload)
    return write_package(tmp_path, entries, "reexported.bootrec")


def missing_entry_package(tmp_path: Path) -> Path:
    """缺条目包：容器里少了 ``journal.jsonl``（没有数据时必须写成空文件，不能省略）。"""
    entries = package_entries()
    del entries["journal.jsonl"]
    return write_package(tmp_path, entries, "missing-entry.bootrec")


def count_mismatch_package(tmp_path: Path) -> Path:
    """行数不符包：manifest 里登记的记录条数与文件实际行数不一致（文件被截断的样子）。"""
    entries = package_entries()
    payload = manifest(entries)
    payload["counts"]["records"] = len(record_rows(entries)) - 1
    set_manifest(entries, payload)
    return write_package(tmp_path, entries, "count-mismatch.bootrec")
