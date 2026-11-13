#!/usr/bin/env bash
# 一条命令跑完 1.0 的验收：数据库、后端单元测试、集成与验收用例、前端检查与构建、界面回放。
#
#   scripts/acceptance.sh                  默认流程
#   scripts/acceptance.sh --skip-e2e       不跑 Playwright 界面回放
#   scripts/acceptance.sh --with-real-machine
#                                          追加验证机上的 LightDM 自动登录与配置恢复，
#                                          需要 SAS_REAL_MACHINE_PASSWORD 等环境变量
#
# 默认使用本地 Podman PostgreSQL（scripts/dev-db.sh）。如果已有可用的测试库，导出
# SAS_TEST_DATABASE_URL / SAS_DATABASE_URL 后脚本直接使用它们，不再启动容器。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WITH_REAL_MACHINE=0
SKIP_E2E=0

for arg in "$@"; do
    case "$arg" in
        --with-real-machine) WITH_REAL_MACHINE=1 ;;
        --skip-e2e) SKIP_E2E=1 ;;
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

if [ -z "${SAS_TEST_DATABASE_URL:-}" ]; then
    step "启动本地 PostgreSQL"
    "$ROOT/scripts/dev-db.sh" start
    export SAS_TEST_DATABASE_URL="postgresql+asyncpg://sas:sas@127.0.0.1:55432/system_analyzer"
fi
export SAS_DATABASE_URL="${SAS_DATABASE_URL:-$SAS_TEST_DATABASE_URL}"

step "后端：安装依赖"
(cd "$ROOT/backend" && uv sync --frozen)

step "后端：数据库迁移"
(cd "$ROOT/backend" && uv run alembic upgrade head)

step "后端：迁移只有一个 head"
HEADS="$(cd "$ROOT/backend" && uv run alembic heads)"
echo "$HEADS"
if [ "$(printf '%s\n' "$HEADS" | grep -c '(head)')" -ne 1 ]; then
    echo "迁移存在多个 head，先合并再验收" >&2
    exit 1
fi

step "后端：静态检查与单元测试"
(cd "$ROOT/backend" && uv run ruff format --check . && uv run ruff check . && uv run pytest)

step "后端：集成测试（真实 PostgreSQL）"
(cd "$ROOT/backend" && uv run pytest -m integration -q)

step "后端：验收用例"
(cd "$ROOT/backend" && uv run pytest -m acceptance -q)

step "前端：安装依赖"
(cd "$ROOT/frontend" && npm ci)

step "前端：静态检查、类型检查与单元测试"
(cd "$ROOT/frontend" && npm run lint && npm run typecheck && npm run format:check && npm run test)

step "前端：构建"
(cd "$ROOT/frontend" && npm run build)

if [ "$SKIP_E2E" = "0" ]; then
    step "前端：界面回放（Playwright）"
    (cd "$ROOT/frontend" && npx playwright install chromium >/dev/null && npm run test:e2e)
fi

if [ "$WITH_REAL_MACHINE" = "1" ]; then
    step "验证机：LightDM 自动登录与配置恢复"
    (cd "$ROOT/backend" && uv run pytest -m real_machine -s -q)
fi

step "验收全部通过"
