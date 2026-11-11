#!/usr/bin/env bash
# 本地门禁：与 CI 执行同一组检查。
#
#   scripts/gate.sh                     前后端静态检查、单元测试、前端构建
#   scripts/gate.sh --with-integration  额外执行需要 PostgreSQL 的集成测试
#
# 集成测试需要先执行 scripts/dev-db.sh start 与后端 alembic upgrade head。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WITH_INTEGRATION=0

for arg in "$@"; do
    case "$arg" in
        --with-integration) WITH_INTEGRATION=1 ;;
        *)
            echo "未知参数: $arg" >&2
            exit 2
            ;;
    esac
done

step() {
    echo
    echo "==> $1"
}

step "后端：安装依赖（uv sync --frozen）"
(cd "$ROOT/backend" && uv sync --frozen)

step "后端：格式检查（ruff format --check）"
(cd "$ROOT/backend" && uv run ruff format --check .)

step "后端：静态检查（ruff check）"
(cd "$ROOT/backend" && uv run ruff check .)

step "后端：单元测试（pytest）"
(cd "$ROOT/backend" && uv run pytest)

if [ "$WITH_INTEGRATION" = "1" ]; then
    step "后端：集成测试（pytest -m integration）"
    # 测试库地址由后端读取（环境变量或 backend/.env），缺配置时 pytest 直接报错退出
    (cd "$ROOT/backend" && uv run pytest -m integration)
fi

step "前端：安装依赖（npm ci）"
(cd "$ROOT/frontend" && npm ci)

step "前端：静态检查（eslint）"
(cd "$ROOT/frontend" && npm run lint)

step "前端：类型检查（tsc --noEmit）"
(cd "$ROOT/frontend" && npm run typecheck)

step "前端：格式检查（prettier --check）"
(cd "$ROOT/frontend" && npm run format:check)

step "前端：单元测试（vitest）"
(cd "$ROOT/frontend" && npm run test)

step "前端：构建（next build）"
(cd "$ROOT/frontend" && npm run build)

step "门禁通过"
