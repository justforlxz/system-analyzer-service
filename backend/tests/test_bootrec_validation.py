"""bootrec v1 校验工具的测试：合法样例通过，改坏的样例被拒绝。

样例取自仓库里的 ``docs/samples/bootrec-v1/``：这是交给下游（1.1、1.3、1.4、1.5）的
共同夹具，所以既验证「它们本身合法」，也验证「改坏之后一定判不通过」。
"""

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from system_analyzer.domain.bootrec import (
    DATA_ENTRIES,
    compute_record_id,
    count_jsonl_lines,
    jsonl_lines,
    sha256_hex,
    validate_bootrec,
)
from system_analyzer.tools import make_bootrec_samples
from system_analyzer.tools import validate_bootrec as validate_cli

SAMPLE_DIR = Path(__file__).resolve().parents[2] / "docs" / "samples" / "bootrec-v1"
FULL = SAMPLE_DIR / "full.bootrec"
JOURNAL_ONLY = SAMPLE_DIR / "journal-only.bootrec"
RESOURCES_MISSING = SAMPLE_DIR / "resources-missing.bootrec"


# ---------------------------------------------------------------------------
# 改写样例的小工具
# ---------------------------------------------------------------------------


def _entries(sample: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(sample) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _write(tmp_path: Path, entries: dict[str, bytes], name: str = "tampered.bootrec") -> Path:
    path = tmp_path / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for entry, data in entries.items():
            archive.writestr(entry, data)
    return path


def _rows(entries: dict[str, bytes], name: str) -> list[dict[str, Any]]:
    """按 `\n` 切行：JSONL 的行分隔只有 `\n`，字符串里可以出现 U+2028 等字符。"""
    return [json.loads(line.decode("utf-8")) for line in entries[name].split(b"\n") if line.strip()]


def _set_rows(entries: dict[str, bytes], name: str, rows: list[dict[str, Any]]) -> None:
    entries[name] = "".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows
    ).encode("utf-8")


def _refresh_manifest(entries: dict[str, bytes]) -> None:
    """重算校验和与行数，让「改坏数据」只留下数据本身的问题。"""
    manifest = json.loads(entries["manifest.json"])
    manifest["entries"] = {
        entry: {"sha256": sha256_hex(entries[entry]), "size_bytes": len(entries[entry])}
        for entry in DATA_ENTRIES
    }
    manifest["counts"] = {
        "entities": count_jsonl_lines(entries["entities.jsonl"]),
        "records": count_jsonl_lines(entries["records.jsonl"]),
        "journal_entries": count_jsonl_lines(entries["journal.jsonl"]),
    }
    entries["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")


def _reidentify(record: dict[str, Any]) -> dict[str, Any]:
    """内容改动后重算 record_id，排除 `record_id 不符` 这个干扰项。"""
    body = {key: value for key, value in record.items() if key != "record_id"}
    return {**body, "record_id": compute_record_id(body)}


def _codes(report: Any) -> set[str]:
    return {issue.code for issue in report.issues}


# ---------------------------------------------------------------------------
# 合法样例
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sample", [FULL, JOURNAL_ONLY, RESOURCES_MISSING])
def test_committed_sample_is_valid(sample: Path) -> None:
    report = validate_bootrec(sample)

    assert report.ok, report.format_summary()
    assert report.format_version == "1.0"


def test_full_sample_contains_every_record_kind() -> None:
    kinds = {row["kind"] for row in _rows(_entries(FULL), "records.jsonl")}

    assert kinds == {"event", "span", "metric", "profile", "annotation", "artifact"}


def test_journal_only_sample_has_no_resource_data() -> None:
    report = validate_bootrec(JOURNAL_ONLY)

    assert report.ok, report.format_summary()
    assert report.resources_available is False
    assert report.metric_record_count == 0
    assert report.unavailable_streams == []
    assert report.journal_entry_count == 4
    records = _rows(_entries(JOURNAL_ONLY), "records.jsonl")
    assert all(row["kind"] != "metric" for row in records)
    assert "资源数据: 无" in report.format_summary()


def test_resource_missing_sample_marks_streams_unavailable() -> None:
    report = validate_bootrec(RESOURCES_MISSING)

    assert report.ok, report.format_summary()
    assert report.metric_record_count == 3
    assert report.unavailable_streams == [
        "metric:disk.read_bytes_per_second",
        "metric:memory.used",
    ]
    streams = {
        row["entity_id"]: row
        for row in _rows(_entries(RESOURCES_MISSING), "entities.jsonl")
        if row["entity_type"] == "metric_stream"
    }
    assert streams["metric:memory.used"]["available"] is False
    assert streams["metric:memory.used"]["unavailable_reason"]


def test_samples_match_generator() -> None:
    """仓库里的样例必须就是生成器的输出，改动样例却不改生成器会在这里失败。"""
    assert make_bootrec_samples.main(["--check"]) == 0


# ---------------------------------------------------------------------------
# 改坏之后必须判不通过
# ---------------------------------------------------------------------------


def test_tampered_record_breaks_checksum(tmp_path: Path) -> None:
    entries = _entries(FULL)
    rows = _rows(entries, "records.jsonl")
    rows[0]["payload"]["severity"] = "critical"
    _set_rows(entries, "records.jsonl", rows)

    report = validate_bootrec(_write(tmp_path, entries))

    assert not report.ok
    assert "E_SHA256_MISMATCH" in _codes(report)


def test_tampered_record_with_recomputed_checksum_breaks_record_id(tmp_path: Path) -> None:
    entries = _entries(FULL)
    rows = _rows(entries, "records.jsonl")
    rows[0]["payload"]["message"] = "被改过的消息文本"
    _set_rows(entries, "records.jsonl", rows)
    _refresh_manifest(entries)

    report = validate_bootrec(_write(tmp_path, entries))

    assert not report.ok
    assert "E_RECORD_ID_MISMATCH" in _codes(report)


def test_record_id_is_stable_for_same_content() -> None:
    record = {"kind": "event", "payload": {"event_type": "x"}, "stream_id": "s"}
    reordered = {"stream_id": "s", "payload": {"event_type": "x"}, "kind": "event"}

    assert compute_record_id(record) == compute_record_id(reordered)
    assert compute_record_id(record) != compute_record_id({**record, "stream_id": "other"})


def test_missing_payload_field_is_rejected(tmp_path: Path) -> None:
    entries = _entries(RESOURCES_MISSING)
    rows = [_reidentify(row) for row in _rows(entries, "records.jsonl")]
    metric = next(row for row in rows if row["kind"] == "metric")
    del metric["payload"]["unit"]
    metric.update(_reidentify(metric))
    _set_rows(entries, "records.jsonl", rows)
    _refresh_manifest(entries)

    report = validate_bootrec(_write(tmp_path, entries))

    assert not report.ok
    assert "E_PAYLOAD_FIELD" in _codes(report)


def test_unavailable_stream_with_data_is_rejected(tmp_path: Path) -> None:
    entries = _entries(RESOURCES_MISSING)
    rows = _rows(entries, "records.jsonl")
    filled = dict(next(row for row in rows if row["kind"] == "metric"))
    filled["entity"] = "metric:memory.used"
    filled["payload"] = {"name": "memory.used", "unit": "bytes", "value": 0.0}
    rows.append(_reidentify(filled))
    _set_rows(entries, "records.jsonl", rows)
    _refresh_manifest(entries)

    report = validate_bootrec(_write(tmp_path, entries))

    assert not report.ok
    assert "E_UNAVAILABLE_STREAM_HAS_DATA" in _codes(report)


def test_journal_flag_mismatch_is_rejected(tmp_path: Path) -> None:
    entries = _entries(FULL)
    manifest = json.loads(entries["manifest.json"])
    manifest["journal"]["included"] = False
    entries["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")

    report = validate_bootrec(_write(tmp_path, entries))

    assert not report.ok
    assert "E_JOURNAL_FLAG" in _codes(report)


def test_missing_artifact_is_rejected(tmp_path: Path) -> None:
    entries = _entries(FULL)
    del entries["artifacts/perf.data"]

    report = validate_bootrec(_write(tmp_path, entries))

    assert not report.ok
    assert "E_ARTIFACT_MISSING" in _codes(report)


def test_tampered_artifact_is_rejected(tmp_path: Path) -> None:
    entries = _entries(FULL)
    entries["artifacts/perf.data"] = b"tampered perf data\n"

    report = validate_bootrec(_write(tmp_path, entries))

    assert not report.ok
    assert "E_ARTIFACT_SIZE" in _codes(report)
    assert "E_ARTIFACT_CHECKSUM" in _codes(report)


def test_unsupported_major_version_is_rejected(tmp_path: Path) -> None:
    entries = _entries(FULL)
    manifest = json.loads(entries["manifest.json"])
    manifest["format_version"] = "2.0"
    entries["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")

    report = validate_bootrec(_write(tmp_path, entries))

    assert not report.ok
    assert "E_UNSUPPORTED_VERSION" in _codes(report)


def test_newer_minor_version_only_warns(tmp_path: Path) -> None:
    entries = _entries(FULL)
    manifest = json.loads(entries["manifest.json"])
    manifest["format_version"] = "1.9"
    entries["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")

    report = validate_bootrec(_write(tmp_path, entries))

    assert report.ok, report.format_summary()
    assert "E_NEWER_MINOR_VERSION" in _codes(report)


def test_wrong_record_entity_is_rejected(tmp_path: Path) -> None:
    entries = _entries(FULL)
    rows = _rows(entries, "records.jsonl")
    metric = next(row for row in rows if row["kind"] == "metric")
    metric["entity"] = "service:nginx.service"
    metric.update(_reidentify(metric))
    _set_rows(entries, "records.jsonl", rows)
    _refresh_manifest(entries)

    report = validate_bootrec(_write(tmp_path, entries))

    assert not report.ok
    assert "E_ENTITY_TYPE_MISMATCH" in _codes(report)


def test_count_mismatch_is_rejected(tmp_path: Path) -> None:
    entries = _entries(FULL)
    manifest = json.loads(entries["manifest.json"])
    manifest["counts"]["records"] += 1
    entries["manifest.json"] = json.dumps(manifest, ensure_ascii=False).encode("utf-8")

    report = validate_bootrec(_write(tmp_path, entries))

    assert not report.ok
    assert "E_COUNT_MISMATCH" in _codes(report)


def test_non_utf8_bytes_are_reported_not_raised(tmp_path: Path) -> None:
    """坏字节输入必须变成校验错误，不能把 UnicodeDecodeError 抛给调用方。"""
    entries = _entries(JOURNAL_ONLY)
    entries["journal.jsonl"] = (
        b'{"boot_id":"x","realtime_ns":1,"priority":6,"message":"\xff\xfe"}\n'
    )
    _refresh_manifest(entries)

    report = validate_bootrec(_write(tmp_path, entries))

    assert not report.ok
    assert "E_JSONL_ENCODING" in _codes(report)
    assert "E_JSONL_SYNTAX" not in _codes(report)


def test_line_separator_inside_string_is_not_split(tmp_path: Path) -> None:
    """JSON 字符串里的 U+2028 等字符不是行分隔符，不能把一条记录切成两段。"""
    entries = _entries(FULL)
    rows = [_reidentify(row) for row in _rows(entries, "records.jsonl")]
    rows[0]["payload"]["message"] = "第一段\u2028第二段\u2029第三段\u0085结尾"
    rows[0] = _reidentify(rows[0])
    _set_rows(entries, "records.jsonl", rows)
    _refresh_manifest(entries)

    report = validate_bootrec(_write(tmp_path, entries))

    assert report.ok, report.format_summary()
    assert report.record_count == len(rows)


def test_directory_input_is_reported_not_raised(tmp_path: Path) -> None:
    """传目录进去只是校验不通过，不能抛 IsADirectoryError。"""
    report = validate_bootrec(tmp_path)

    assert not report.ok
    assert _codes(report) == {"E_FILE_UNREADABLE"}


def test_invalid_record_id_format_is_reported(tmp_path: Path) -> None:
    entries = _entries(RESOURCES_MISSING)
    rows = _rows(entries, "records.jsonl")
    rows[0]["record_id"] = rows[0]["record_id"].upper()
    _set_rows(entries, "records.jsonl", rows)
    _refresh_manifest(entries)

    report = validate_bootrec(_write(tmp_path, entries))

    assert not report.ok
    assert "E_RECORD_ID_FORMAT" in _codes(report)


def test_jsonl_lines_splits_only_on_newline() -> None:
    blob = b'{"a":"x\xe2\x80\xa8y"}\n{"b":1}\n\n'
    lines = jsonl_lines(blob)

    assert [number for number, _ in lines] == [1, 2]


def test_not_a_zip_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "broken.bootrec"
    path.write_bytes(b"not a zip at all")

    report = validate_bootrec(path)

    assert not report.ok
    assert _codes(report) == {"E_NOT_A_ZIP"}


def test_missing_manifest_is_rejected(tmp_path: Path) -> None:
    entries = _entries(FULL)
    del entries["manifest.json"]

    report = validate_bootrec(_write(tmp_path, entries))

    assert not report.ok
    assert "E_MISSING_ENTRY" in _codes(report)


def test_missing_file_is_reported_not_raised(tmp_path: Path) -> None:
    report = validate_bootrec(tmp_path / "nope.bootrec")

    assert not report.ok
    assert _codes(report) == {"E_FILE_NOT_FOUND"}


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def test_cli_passes_on_valid_sample(capsys: pytest.CaptureFixture[str]) -> None:
    assert validate_cli.main([str(FULL)]) == 0
    assert "结果: 通过" in capsys.readouterr().out


def test_cli_fails_on_tampered_sample(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    entries = _entries(FULL)
    rows = _rows(entries, "records.jsonl")
    rows[0]["payload"]["severity"] = "critical"
    _set_rows(entries, "records.jsonl", rows)
    path = _write(tmp_path, entries)

    assert validate_cli.main([str(path)]) == 1
    assert "结果: 不通过" in capsys.readouterr().out


def test_cli_reports_usage_without_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    assert validate_cli.main([]) == 2
    assert "用法" in capsys.readouterr().err
