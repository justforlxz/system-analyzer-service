/** 把时间线与服务诊断的游标写进地址栏，并解析地址栏里的 `at_ns`。
 *
 * 只改地址栏，不触发路由跳转：`history.replaceState` 是浅层更新，不会让 Server Component
 * 重新取数，也不会重置当前的窗口与轨道。
 */

export type CursorUrlOptions = {
  /** 当前游标；`null` 表示没有可停靠时刻，从地址栏删掉 `at_ns`。 */
  atNs: number | null;
  /** 当前服务实体；不传表示保持地址栏里的 `entity` 不变。 */
  entity?: string | null;
};

/** 解析 `at_ns` 参数：只接受非负安全整数，其余（含空串、负数、小数）按未提供处理。 */
export function parseCursorNs(raw: string | null): number | null {
  if (raw === null) return null;
  const trimmed = raw.trim();
  if (trimmed === "") return null;
  const parsed = Number(trimmed);
  return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : null;
}

function currentLocation(): string {
  return `${window.location.pathname}${window.location.search}${window.location.hash}`;
}

/** 把游标（和可选的实体）写进当前地址栏，保留 `machine_id`、`boot_id` 等已有参数。 */
export function writeCursorToUrl({ atNs, entity }: CursorUrlOptions): void {
  if (typeof window === "undefined") return;
  const url = new URL(window.location.href);
  if (atNs === null) {
    url.searchParams.delete("at_ns");
  } else {
    url.searchParams.set("at_ns", String(atNs));
  }
  if (entity !== undefined) {
    if (entity === null || entity === "") {
      url.searchParams.delete("entity");
    } else {
      url.searchParams.set("entity", entity);
    }
  }
  const next = `${url.pathname}${url.search}${url.hash}`;
  if (next === currentLocation()) return;
  window.history.replaceState(window.history.state, "", next);
}
