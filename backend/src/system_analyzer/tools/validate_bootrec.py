"""校验 ``.bootrec`` 文件是否符合 bootrec v1 规范。

用法（在 ``backend`` 目录下）：

    uv run python -m system_analyzer.tools.validate_bootrec <文件> [<文件> ...]

全部通过时退出码 0，任何一个文件有 ``error`` 时退出码 1。``warning`` 只打印，不影响退出码。
"""

import sys
from pathlib import Path

from system_analyzer.domain.bootrec import validate_bootrec


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print(
            "用法: python -m system_analyzer.tools.validate_bootrec <文件> [<文件> ...]",
            file=sys.stderr,
        )
        return 2

    failed = 0
    for position, name in enumerate(args):
        if position:
            print()
        report = validate_bootrec(Path(name))
        print(report.format_summary())
        if not report.ok:
            failed += 1

    if len(args) > 1:
        print()
        print(f"合计 {len(args)} 个文件，{len(args) - failed} 个通过，{failed} 个不通过")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
