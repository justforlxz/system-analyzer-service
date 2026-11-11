"""把一条 Journal 事件的原始字段换算成它在 boot 时间轴上的位置。

用法（在 ``backend`` 目录下）：

    journalctl -o json -b -n 1 | uv run python -m system_analyzer.tools.journal_time
    journalctl -o export -b -n 200 | uv run python -m system_analyzer.tools.journal_time
    uv run python -m system_analyzer.tools.journal_time <字段文件>

输入取**第一条**事件：``-o json`` 取第一行；``-o export`` 取第一个空行之前那一段。没有参数时
从标准输入读，给了文件路径就读文件。换算规则见 ``docs/time-base.md``，实现见
``system_analyzer.domain.time_base``。

``-o export`` 的两种字段写法都要认，因为它确实会两种都写出来：

- ``字段=值``，一行一个。文本字段都是这种。
- 二进制字段写成「字段名单独一行 + 8 字节原生长度 + 原始字节 + 一个换行」。``SYSLOG_RAW``
  就是这种，值里可能带换行，也可能不是合法 UTF-8。长度用的是原生 ``size_t``，64 位 Linux 上是
  8 字节小端。换算只要 ``__MONOTONIC_TIMESTAMP``、``__REALTIME_TIMESTAMP``、``_BOOT_ID`` 和
  ``_SOURCE_REALTIME_TIMESTAMP``，二进制字段整个跳过，不解码、不保留。

退出码：0 换算成功；1 输入或换算出错（含 ``-o export`` 被截断、时间戳超出 int64 纳秒范围），
错误原因写在标准错误；2 没有输入（打印用法）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from system_analyzer.domain.time_base import JournalEventPosition, journal_event_position

EXPORT_BINARY_LENGTH_SIZE = 8
"""``-o export`` 二进制字段的长度头字节数：原生的 ``size_t``，64 位 Linux 上是 8 字节小端。"""

USAGE = (
    "用法: python -m system_analyzer.tools.journal_time [字段文件]\n"
    "输入一条 Journal 事件的原始字段（journalctl -o json 的一行，或 -o export 的一段），"
    "输出它在 boot 时间轴上的位置。"
)


def parse_event(data: bytes) -> dict[str, str]:
    """从 ``-o json`` 的一行或 ``-o export`` 的一段里取出第一条事件的文本字段。"""
    if not data.strip():
        raise ValueError("输入是空的")
    if data.lstrip().startswith(b"{"):
        return _parse_json_line(data)
    return _parse_export(data)


def _parse_json_line(data: bytes) -> dict[str, str]:
    first_line = data.strip().split(b"\n", 1)[0]
    payload = json.loads(first_line.decode("utf-8"))
    return {str(key): str(value) for key, value in payload.items()}


def _parse_export(data: bytes) -> dict[str, str]:
    """按 ``-o export`` 的两种字段写法逐段扫描，遇到空行就是第一条事件的结尾。"""
    fields: dict[str, str] = {}
    position = 0
    while position < len(data):
        newline = data.find(b"\n", position)
        if newline < 0:
            line, position = data[position:], len(data)
        else:
            line, position = data[position:newline], newline + 1
        if not line:
            if fields:
                break
            continue
        name, separator, value = line.partition(b"=")
        if separator:
            fields[_text(name)] = _text(value)
            continue
        position = _skip_binary_field(data, position, _text(name))
    return fields


def _skip_binary_field(data: bytes, position: int, name: str) -> int:
    """跳过二进制字段，返回它后面那个位置。

    长度头不是文本，所以跳不过去时只报一条可读错误，不能让解码或切片异常冒出去。
    """
    header_end = position + EXPORT_BINARY_LENGTH_SIZE
    if header_end > len(data):
        raise ValueError(f"字段 {name} 是二进制字段，但后面没有完整的长度头，输入被截断")
    size = int.from_bytes(data[position:header_end], "little")
    end = header_end + size
    if end > len(data):
        raise ValueError(
            f"字段 {name} 声明 {size} 字节，输入只剩 {len(data) - header_end} 字节；"
            "输入被截断，或不是 journalctl -o export 的格式"
        )
    if data[end : end + 1] == b"\n":
        end += 1
    return end


def _text(raw: bytes) -> str:
    """文本字段解码。换算用到的字段都是 ASCII；解不出来的字节替换掉，不抛异常。"""
    return raw.decode("utf-8", "replace")


def format_position(position: JournalEventPosition) -> str:
    """换算结果的固定输出格式：键与顺序固定，缺失的读数写 ``null``。"""
    lines = [
        f"boot_id: {position.boot_id}",
        f"clock_domain: {position.clock_domain.value}",
        f"monotonic_ns: {_value(position.monotonic_ns)}",
        f"boot_relative: {_value(position.boot_relative)}",
        f"realtime_ns: {_value(position.realtime_ns)}",
        f"realtime_utc: {_value(position.realtime_utc)}",
        f"source_realtime_ns: {_value(position.source_realtime_ns)}",
        f"boot_start_realtime_ns: {_value(position.boot_start_realtime_ns)}",
    ]
    return "\n".join(lines)


def _value(value: object) -> str:
    return "null" if value is None else str(value)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) > 1:
        print(USAGE, file=sys.stderr)
        return 2

    if args:
        try:
            data = Path(args[0]).read_bytes()
        except OSError as error:
            print(f"无法读取 {args[0]}：{error}", file=sys.stderr)
            return 1
    else:
        data = sys.stdin.buffer.read()

    if not data.strip():
        print(USAGE, file=sys.stderr)
        return 2

    # 格式化也放在 try 里：realtime_utc、boot_relative 是惰性属性，取值超出 datetime 能表示的范围
    # 时异常发生在格式化阶段而不是换算阶段。换算是纯函数，这一层只是保证任何输入都落到
    # 「换算失败：<原因>」，不会变成 traceback。
    try:
        output = format_position(journal_event_position(parse_event(data)))
    except (ValueError, OverflowError, OSError) as error:
        print(f"换算失败：{error}", file=sys.stderr)
        return 1

    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
