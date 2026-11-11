"""时间基准换算的测试：单位换算、Boot ID 归一化、真实 Journal 事件的定位。

这里的期望值全部来自 ``docs/time-base.md`` 第 5 节那条真实事件。文档里的换算输出块由
``test_cli_output_matches_document`` 直接读出来比对，所以文档与工具不可能各自漂移。
"""

import dataclasses
import io
import json
import types
from collections.abc import Mapping
from pathlib import Path

import pytest

from system_analyzer.domain.bootrec import ClockDomain
from system_analyzer.domain.time_base import (
    JournalEventPosition,
    format_boot_relative,
    journal_event_position,
    micros_to_ns,
    normalize_boot_id,
)
from system_analyzer.tools import journal_time as journal_time_cli

DOC = Path(__file__).resolve().parents[2] / "docs" / "time-base.md"

# docs/time-base.md 第 5 节那条真实事件（验证机 10.20.50.152，2026-11-11 采集）的原始字段
REAL_EVENT = {
    "_BOOT_ID": "abc7451f65c84be8ab014b9a1e49a8b9",
    "_MACHINE_ID": "dda4d4909d9a425db501d790e8ee6555",
    "_PID": "3439748",
    "_SYSTEMD_UNIT": "session-674.scope",
    "_SYSTEMD_INVOCATION_ID": "39b369d1e917432bb0a9bb9d4df7d03e",
    "_SYSTEMD_CGROUP": "/user.slice/user-1000.slice/session-674.scope",
    "SYSLOG_IDENTIFIER": "sudo",
    "PRIORITY": "5",
    "MESSAGE": "     uos : PWD=/home/uos ; USER=root ; COMMAND=/usr/bin/id -u root",
    "_SOURCE_REALTIME_TIMESTAMP": "1794367420210120",
    "__REALTIME_TIMESTAMP": "1794367420210127",
    "__MONOTONIC_TIMESTAMP": "175830828563",
}

DOC_HEADING = "**换算结果（`python -m system_analyzer.tools.journal_time` 的实际输出）**"


def _documented_output() -> str:
    """取出 docs/time-base.md 里紧跟换算命令的那段输出，去掉首尾空行。"""
    text = DOC.read_text(encoding="utf-8")
    start = text.index(DOC_HEADING) + len(DOC_HEADING)
    fence_start = text.index("```text", start) + len("```text")
    fence_end = text.index("```", fence_start)
    return text[fence_start:fence_end].strip("\n")


# ---------------------------------------------------------------------------
# 微秒与纳秒
# ---------------------------------------------------------------------------


def test_micros_to_ns_multiplies_by_one_thousand() -> None:
    assert micros_to_ns(175830828563) == 175_830_828_563_000
    assert micros_to_ns(0) == 0


def test_micros_to_ns_keeps_microsecond_resolution() -> None:
    """换算后低三位恒为 0：Journal 只有微秒精度，不能凭空补出纳秒。"""
    assert micros_to_ns(1) == 1_000
    assert micros_to_ns(1_794_367_420_210_127) % 1_000 == 0


def test_micros_to_ns_rejects_negative() -> None:
    with pytest.raises(ValueError):
        micros_to_ns(-1)


# ---------------------------------------------------------------------------
# Boot ID：journald 的 32 位十六进制与 /proc 的 UUID 形式
# ---------------------------------------------------------------------------


def test_normalize_boot_id_converts_journal_form() -> None:
    assert (
        normalize_boot_id("abc7451f65c84be8ab014b9a1e49a8b9")
        == "abc7451f-65c8-4be8-ab01-4b9a1e49a8b9"
    )


def test_normalize_boot_id_keeps_proc_form_unchanged() -> None:
    proc_form = "abc7451f-65c8-4be8-ab01-4b9a1e49a8b9"
    assert normalize_boot_id(proc_form) == proc_form


def test_normalize_boot_id_lowercases() -> None:
    assert (
        normalize_boot_id("ABC7451F65C84BE8AB014B9A1E49A8B9")
        == "abc7451f-65c8-4be8-ab01-4b9a1e49a8b9"
    )


@pytest.mark.parametrize(
    "raw", ["", "abc7451f", "abc7451f65c84be8ab014b9a1e49a8b", "zzz7451f65c84be8ab014b9a1e49a8b9"]
)
def test_normalize_boot_id_rejects_other_forms(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_boot_id(raw)


# ---------------------------------------------------------------------------
# 真实事件在 boot 时间轴上的位置
# ---------------------------------------------------------------------------


def test_real_event_position() -> None:
    position = journal_event_position(REAL_EVENT)

    assert position.boot_id == "abc7451f-65c8-4be8-ab01-4b9a1e49a8b9"
    assert position.clock_domain is ClockDomain.BOOT_MONOTONIC
    assert position.monotonic_ns == 175_830_828_563_000
    assert position.realtime_ns == 1_794_367_420_210_127_000
    assert position.source_realtime_ns == 1_794_367_420_210_120_000
    # 启动墙钟 = realtime - monotonic，与 uptime -s 的 2026-11-09 10:33:09 CST 一致
    assert position.boot_start_realtime_ns == 1_794_191_589_381_564_000


def test_boot_relative_matches_short_monotonic_output() -> None:
    """journalctl -o short-monotonic 打印 [175830.828563]，即 2 天 0 小时 50 分 30.828563 秒。"""
    assert format_boot_relative(175_830_828_563_000) == "2d 00:50:30.828563000"
    assert format_boot_relative(0) == "0d 00:00:00.000000000"
    assert format_boot_relative(1_200_000_000) == "0d 00:00:01.200000000"


def test_realtime_utc_matches_syslog_timestamp() -> None:
    """SYSLOG_TIMESTAMP 是 CST 的 11:23:40，对应 UTC 03:23:40.210127。"""
    position = journal_event_position(REAL_EVENT)

    assert position.realtime_utc == "2026-11-11T03:23:40.210127+00:00"


def test_position_without_monotonic_falls_back_to_wall_clock() -> None:
    fields = {k: v for k, v in REAL_EVENT.items() if k != "__MONOTONIC_TIMESTAMP"}

    position = journal_event_position(fields)

    assert position.clock_domain is ClockDomain.WALL_REALTIME
    assert position.monotonic_ns is None
    assert position.realtime_ns == 1_794_367_420_210_127_000
    assert position.boot_start_realtime_ns is None


def test_source_timestamp_is_recorded_but_never_used_as_realtime() -> None:
    fields = {k: v for k, v in REAL_EVENT.items() if k != "__REALTIME_TIMESTAMP"}

    position = journal_event_position(fields)

    assert position.realtime_ns is None
    assert position.source_realtime_ns == 1_794_367_420_210_120_000
    assert position.clock_domain is ClockDomain.BOOT_MONOTONIC
    assert position.boot_start_realtime_ns is None


def test_position_without_any_timestamp_is_rejected() -> None:
    fields = {
        k: v
        for k, v in REAL_EVENT.items()
        if k not in ("__MONOTONIC_TIMESTAMP", "__REALTIME_TIMESTAMP")
    }

    with pytest.raises(ValueError, match="时间"):
        journal_event_position(fields)


def test_missing_boot_id_is_rejected() -> None:
    fields = {k: v for k, v in REAL_EVENT.items() if k != "_BOOT_ID"}

    with pytest.raises(ValueError, match="_BOOT_ID"):
        journal_event_position(fields)


def test_position_is_frozen() -> None:
    position = journal_event_position(REAL_EVENT)

    assert isinstance(position, JournalEventPosition)
    with pytest.raises(dataclasses.FrozenInstanceError):
        position.monotonic_ns = 1  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 命令行：读一条事件的原始字段，打印它在 boot 时间轴上的位置
# ---------------------------------------------------------------------------


def _export_bytes(fields: Mapping[str, str]) -> bytes:
    return "".join(f"{key}={value}\n" for key, value in fields.items()).encode("utf-8")


def _binary_field(name: str, value: bytes) -> bytes:
    """按 ``journalctl -o export`` 的写法拼一个二进制字段。

    写法是「字段名单独一行 + 8 字节原生长度 + 原始字节 + 一个换行」，长度头不是文本，
    值里可以有换行与非 UTF-8 字节 —— 这正是验证机上 ``SYSLOG_RAW`` 的样子。
    """
    return name.encode() + b"\n" + len(value).to_bytes(8, "little") + value + b"\n"


def _stdin(monkeypatch: pytest.MonkeyPatch, data: bytes) -> None:
    """工具读 ``sys.stdin.buffer``，不解码整份输入，所以这里给字节流。"""
    monkeypatch.setattr("sys.stdin", types.SimpleNamespace(buffer=io.BytesIO(data)))


def test_cli_output_matches_document(capsys: pytest.CaptureFixture[str], monkeypatch) -> None:
    """工具输出必须与 docs/time-base.md 里贴的那段逐字节一致。"""
    _stdin(monkeypatch, _export_bytes(REAL_EVENT))

    assert journal_time_cli.main([]) == 0

    assert capsys.readouterr().out == _documented_output() + "\n"


def test_cli_reads_journalctl_json(capsys: pytest.CaptureFixture[str], monkeypatch) -> None:
    _stdin(monkeypatch, (json.dumps(REAL_EVENT) + "\n").encode())

    assert journal_time_cli.main([]) == 0

    assert "monotonic_ns: 175830828563000" in capsys.readouterr().out


def test_cli_reads_file_argument(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "entry.txt"
    path.write_bytes(_export_bytes(REAL_EVENT))

    assert journal_time_cli.main([str(path)]) == 0

    assert "boot_id: abc7451f-65c8-4be8-ab01-4b9a1e49a8b9" in capsys.readouterr().out


def test_cli_prints_null_for_missing_monotonic(
    capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    fields = {k: v for k, v in REAL_EVENT.items() if k != "__MONOTONIC_TIMESTAMP"}
    _stdin(monkeypatch, _export_bytes(fields))

    assert journal_time_cli.main([]) == 0

    out = capsys.readouterr().out
    assert "clock_domain: wall_realtime" in out
    assert "monotonic_ns: null" in out


def test_cli_reports_usage_when_nothing_is_piped(
    capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    _stdin(monkeypatch, b"")

    assert journal_time_cli.main([]) == 2

    assert "用法" in capsys.readouterr().err


def test_cli_reports_unreadable_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert journal_time_cli.main([str(tmp_path / "nope.txt")]) == 1

    assert "无法读取" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 真实 -o export 输入：二进制字段不能让工具崩掉（功能检测 F1）
# ---------------------------------------------------------------------------


# 验证机上 SYSLOG_RAW 的真实样子：`<30>` 开头、末尾带换行、中间可能有非 UTF-8 字节。
# 0xb7 就是让旧实现抛 UnicodeDecodeError 的那个字节。
REAL_SYSLOG_RAW = b"<30>Nov 11 14:10:23 host prog[1]: \xb7 invalid utf-8 byte, \nand a newline\n"


def test_cli_skips_binary_export_field_between_timestamps(
    capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    """二进制字段夹在两个时间戳之间时，后面的字段仍然要被读到。"""
    data = (
        b"__REALTIME_TIMESTAMP=1794367420210127\n"
        b"__MONOTONIC_TIMESTAMP=175830828563\n"
        + _binary_field("SYSLOG_RAW", REAL_SYSLOG_RAW)
        + b"_BOOT_ID=abc7451f65c84be8ab014b9a1e49a8b9\n"
        + _binary_field("SYSLOG_RAW", REAL_SYSLOG_RAW)
        + b"\n"
    )
    _stdin(monkeypatch, data)

    assert journal_time_cli.main([]) == 0

    out = capsys.readouterr().out
    assert "monotonic_ns: 175830828563000" in out
    assert "realtime_ns: 1794367420210127000" in out
    assert "boot_id: abc7451f-65c8-4be8-ab01-4b9a1e49a8b9" in out


def test_cli_skips_binary_field_before_timestamps(
    capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    """二进制字段在最前面时，扫描要继续走到后面的时间戳。"""
    data = (
        _binary_field("SYSLOG_RAW", REAL_SYSLOG_RAW) + b"__REALTIME_TIMESTAMP=1794367420210127\n"
        b"__MONOTONIC_TIMESTAMP=175830828563\n"
        b"_BOOT_ID=abc7451f65c84be8ab014b9a1e49a8b9\n"
        b"\n"
        b"__REALTIME_TIMESTAMP=1\n"
        b"__MONOTONIC_TIMESTAMP=1\n"
        b"_BOOT_ID=ffffffffffffffffffffffffffffffff\n"
    )
    _stdin(monkeypatch, data)

    assert journal_time_cli.main([]) == 0

    out = capsys.readouterr().out
    assert "monotonic_ns: 175830828563000" in out
    assert "boot_id: abc7451f-65c8-4be8-ab01-4b9a1e49a8b9" in out


def test_cli_reads_only_first_event_of_export_dump(
    capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    second = _export_bytes(REAL_EVENT).replace(b"1794367420210127", b"1794367420210128")
    _stdin(monkeypatch, _export_bytes(REAL_EVENT) + b"\n" + second)

    assert journal_time_cli.main([]) == 0

    assert "realtime_ns: 1794367420210127000" in capsys.readouterr().out


def test_cli_reports_truncated_binary_field(
    capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    truncated = b"_BOOT_ID=abc7451f65c84be8ab014b9a1e49a8b9\nSYSLOG_RAW\n" + (999).to_bytes(
        8, "little"
    )
    _stdin(monkeypatch, truncated)

    assert journal_time_cli.main([]) == 1

    err = capsys.readouterr().err
    assert "换算失败" in err
    assert "SYSLOG_RAW" in err


def test_cli_reports_json_number_fields(capsys: pytest.CaptureFixture[str], monkeypatch) -> None:
    """``-o json`` 的字段都是字符串；万一不是，也按文本处理而不是崩掉。"""
    payload = {**REAL_EVENT, "__MONOTONIC_TIMESTAMP": 175830828563}
    _stdin(monkeypatch, (json.dumps(payload) + "\n").encode())

    assert journal_time_cli.main([]) == 0

    assert "monotonic_ns: 175830828563000" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 超出可表示范围的时间戳：可读报错，不能是 traceback（功能检测 F2）
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "realtime_micros",
    [
        # 换算成纳秒后刚好越过 int64 上限（`2**63 - 1` 纳秒约等于 2262 年）
        "9223372036854776",
        # int64 的「未知」哨兵值，换算成纳秒后约是 294247 年，旧实现在格式化阶段抛 ValueError
        "9223372036854775807",
        # 大到 `datetime` 只能报 OSError
        "1000000000000000000000000",
    ],
)
def test_cli_reports_out_of_range_timestamp(
    realtime_micros: str, capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    data = (
        b"_BOOT_ID=abc7451f65c84be8ab014b9a1e49a8b9\n"
        + f"__REALTIME_TIMESTAMP={realtime_micros}\n".encode()
        + b"__MONOTONIC_TIMESTAMP=1000\n"
    )
    _stdin(monkeypatch, data)

    assert journal_time_cli.main([]) == 1

    err = capsys.readouterr().err
    assert err.startswith("换算失败：")
