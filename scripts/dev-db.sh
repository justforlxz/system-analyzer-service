#!/usr/bin/env bash
# 用 Podman 起本地 PostgreSQL，供开发与集成测试使用。
#
#   scripts/dev-db.sh start   启动容器（默认端口 55432）
#   scripts/dev-db.sh stop    停止并删除容器
#   scripts/dev-db.sh reset   删掉容器重建（数据不保留，仅本地开发用）
#
# 这里出现的口令只属于本机开发容器，不是任何环境的真实凭据；
# 真实环境的口令通过环境变量注入，不写进仓库。
set -euo pipefail

CONTAINER_NAME="${SAS_DB_CONTAINER:-sas-postgres}"
PORT="${SAS_DB_PORT:-55432}"
IMAGE="${SAS_DB_IMAGE:-docker.io/library/postgres:17-alpine}"
DB_USER="${SAS_DB_USER:-sas}"
DB_NAME="${SAS_DB_NAME:-system_analyzer}"
DB_PASSWORD="${SAS_DB_PASSWORD:-sas}"

usage() {
    echo "用法: $0 {start|stop|reset}" >&2
    exit 2
}

wait_ready() {
    echo "等待 PostgreSQL 就绪..."
    for _ in $(seq 1 30); do
        # pg_isready 在初始化阶段的临时实例上也会返回就绪，因此再真实连一次库
        if podman exec "$CONTAINER_NAME" pg_isready -U "$DB_USER" >/dev/null 2>&1 &&
            podman exec "$CONTAINER_NAME" psql -U "$DB_USER" -d "$DB_NAME" -c 'SELECT 1' >/dev/null 2>&1; then
            echo "PostgreSQL 已就绪：127.0.0.1:${PORT}/${DB_NAME}"
            return 0
        fi
        sleep 1
    done
    echo "PostgreSQL 启动超时" >&2
    return 1
}

start() {
    if podman container exists "$CONTAINER_NAME"; then
        podman start "$CONTAINER_NAME" >/dev/null
    else
        podman run -d \
            --name "$CONTAINER_NAME" \
            -e POSTGRES_USER="$DB_USER" \
            -e POSTGRES_PASSWORD="$DB_PASSWORD" \
            -e POSTGRES_DB="$DB_NAME" \
            -p "${PORT}:5432" \
            "$IMAGE" >/dev/null
    fi
    wait_ready
}

stop() {
    podman rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
    echo "已删除容器 ${CONTAINER_NAME}"
}

case "${1:-start}" in
    start) start ;;
    stop) stop ;;
    reset)
        stop
        start
        ;;
    *) usage ;;
esac
