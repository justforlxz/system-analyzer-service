"""导出 FastAPI 的 OpenAPI 文档，供前端生成 TypeScript 客户端。

用法（在 ``backend`` 目录下）：``uv run python -m system_analyzer.tools.export_openapi``，
默认写到当前目录的 ``openapi.json``，可用第一个参数指定输出路径。
"""

import json
import sys
from pathlib import Path

from system_analyzer.api.app import create_app


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    output = Path(args[0]) if args else Path("openapi.json")
    payload = create_app().openapi()
    output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"OpenAPI 文档已写入 {output}")


if __name__ == "__main__":
    main()
