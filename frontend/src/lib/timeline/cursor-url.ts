/** 时间线地址栏参数的解析与写入：游标 `at_ns` 与窗口 `window_start_ns` / `window_end_ns`。
 *
 * 只改地址栏，不触发路由跳转：`history.replaceState` 是浅层更新，不会让 Server Component
 * 重新取数，也不会重置当前的窗口与轨道。
 */

import { tailWindow, windowForCursor, type WindowRange } from "@/lib/timeline/scale";

export type CursorUrlOptions = {
  /** 当前游标；`null` 表示没有可停靠时刻，从地址栏删掉 `at_ns`。 */
  atNs: number | null;
  /** 当前服务实体；不传表示保持地址栏里的 `entity` 不变。 */
  entity?: string | null;
  /** 当前窗口；不传表示保持地址栏里的窗口参数不变，`null` 表示删掉两个窗口参数。 */
  window?: WindowRange | null;
};

/** 解析 `at_ns` 参数：只接受非负安全整数，其余（含空串、负数、小数）按未提供处理。 */
export function parseCursorNs(raw: string | null): number | null {
  if (raw === null) return null;
  const trimmed = raw.trim();
  if (trimmed === "") return null;
  const parsed = Number(trimmed);
  return Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : null;
}

/** 解析地址栏里的窗口参数。只有两个参数都合法、起点小于终点且完整落在本次启动的轴内
 * 时才采用；否则返回 `null`，调用方回落到原有的按游标选窗口规则。 */
export function parseWindowParams(
  startRaw: string | null,
  endRaw: string | null,
  axis: WindowRange,
): WindowRange | null {
  const startNs = parseCursorNs(startRaw);
  const endNs = parseCursorNs(endRaw);
  if (startNs === null || endNs === null) return null;
  if (startNs >= endNs) return null;
  if (startNs < axis.startNs || endNs > axis.endNs) return null;
  return { startNs, endNs };
}

/** 决定时间线首屏窗口：地址栏给了合法窗口就直接采用；否则没有 `at_ns` 时用尾窗口，
 * 有 `at_ns` 时用以它为中心的窗口（与既有规则一致）。 */
export function resolveTimelineWindow({
  axis,
  atNs,
  windowStartRaw,
  windowEndRaw,
}: {
  axis: WindowRange;
  atNs: number | null;
  windowStartRaw: string | null;
  windowEndRaw: string | null;
}): WindowRange {
  const fromUrl = parseWindowParams(windowStartRaw, windowEndRaw, axis);
  if (fromUrl !== null) return fromUrl;
  return atNs === null ? tailWindow(axis) : windowForCursor(atNs, axis);
}

function currentLocation(): string {
  return `${window.location.pathname}${window.location.search}${window.location.hash}`;
}

/** 把游标、可选的实体与窗口写进当前地址栏，保留 `machine_id`、`boot_id` 等已有参数。 */
export function writeCursorToUrl({ atNs, entity, window: windowRange }: CursorUrlOptions): void {
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
  if (windowRange !== undefined) {
    if (windowRange === null) {
      url.searchParams.delete("window_start_ns");
      url.searchParams.delete("window_end_ns");
    } else {
      url.searchParams.set("window_start_ns", String(windowRange.startNs));
      url.searchParams.set("window_end_ns", String(windowRange.endNs));
    }
  }
  const next = `${url.pathname}${url.search}${url.hash}`;
  if (next === currentLocation()) return;
  window.history.replaceState(window.history.state, "", next);
}
