/** 时间轴窗口的像素映射与缩放、平移。
 *
 * 这里只做「纳秒区间 ↔ 画布比例」的换算和窗口的边界夹紧；时间语义（轨道、条目、
 * 开放区间收尾、停止点）全部由后端决定，前端不重算。
 */

export type WindowRange = { startNs: number; endNs: number };

/** 允许缩到的最小跨度：1 毫秒。再小没有观察意义，也会放大 int64 在 JSON 里的表示误差。 */
export const MIN_WINDOW_NS = 1_000_000;

/** 首屏默认窗口：轴末尾 60 秒。真实采集的 Journal 可能覆盖数天，一次取整条轴会把几十万条
 * 记录搬进浏览器；默认停在采集窗口附近，用户仍可缩放或点「全览」查看整条轴。 */
export const DEFAULT_INITIAL_WINDOW_NS = 60_000_000_000;

/** 窗口的实际跨度。起点与终点相同时返回 1，避免除零。 */
export function windowSpan(range: WindowRange): number {
  return Math.max(1, range.endNs - range.startNs);
}

/** 时刻在窗口中的位置，0 表示窗口起点，1 表示窗口终点。不做夹紧，调用方按需处理。 */
export function nsToFraction(ns: number, range: WindowRange): number {
  return (ns - range.startNs) / windowSpan(range);
}

/** 位置比例换算成时刻，四舍五入到整纳秒。 */
export function fractionToNs(fraction: number, range: WindowRange): number {
  return Math.round(range.startNs + fraction * windowSpan(range));
}

/** 时刻是否落在窗口里（含两端）。 */
export function isInsideWindow(ns: number, range: WindowRange): boolean {
  return ns >= range.startNs && ns <= range.endNs;
}

/** 把窗口夹进轴的起止范围，跨度不小于 `minSpanNs`、不大于整条轴。 */
export function clampWindow(
  startNs: number,
  endNs: number,
  axis: WindowRange,
  minSpanNs: number = MIN_WINDOW_NS,
): WindowRange {
  const axisSpan = axis.endNs - axis.startNs;
  if (axisSpan <= 0) return { startNs: axis.startNs, endNs: axis.startNs };
  const minSpan = Math.min(Math.max(Math.round(minSpanNs), 1), axisSpan);
  const span = Math.min(Math.max(Math.round(endNs - startNs), minSpan), axisSpan);
  let start = Math.round(startNs);
  let end = start + span;
  if (start < axis.startNs) {
    start = axis.startNs;
    end = start + span;
  }
  if (end > axis.endNs) {
    end = axis.endNs;
    start = end - span;
  }
  return { startNs: start, endNs: end };
}

/** 按 `factor` 缩放窗口，锚点位置在缩放前后保持不动（再夹进轴的范围）。 */
export function zoomWindow(
  range: WindowRange,
  factor: number,
  anchorNs: number,
  axis: WindowRange,
  minSpanNs: number = MIN_WINDOW_NS,
): WindowRange {
  const axisSpan = axis.endNs - axis.startNs;
  if (axisSpan <= 0) return { startNs: axis.startNs, endNs: axis.endNs };
  const span = windowSpan(range);
  const minSpan = Math.min(Math.max(Math.round(minSpanNs), 1), axisSpan);
  const targetSpan = Math.min(Math.max(Math.round(span * factor), minSpan), axisSpan);
  const anchorFraction = Math.min(Math.max((anchorNs - range.startNs) / span, 0), 1);
  const startNs = Math.round(anchorNs - targetSpan * anchorFraction);
  return clampWindow(startNs, startNs + targetSpan, axis, minSpan);
}

/** 窗口整体平移 `deltaNs`（正数向右），超出轴时贴边。 */
export function panWindow(
  range: WindowRange,
  deltaNs: number,
  axis: WindowRange,
  minSpanNs: number = MIN_WINDOW_NS,
): WindowRange {
  const startNs = Math.round(range.startNs + deltaNs);
  return clampWindow(startNs, startNs + windowSpan(range), axis, minSpanNs);
}

/** 保持跨度不变，把窗口移到以 `centerNs` 为中心的位置。 */
export function windowAround(
  centerNs: number,
  spanNs: number,
  axis: WindowRange,
  minSpanNs: number = MIN_WINDOW_NS,
): WindowRange {
  const startNs = Math.round(centerNs - spanNs / 2);
  return clampWindow(startNs, startNs + spanNs, axis, minSpanNs);
}

/** 取轴末尾的一段窗口；轴跨度不足时直接返回整条轴。 */
export function tailWindow(
  axis: WindowRange,
  spanNs: number = DEFAULT_INITIAL_WINDOW_NS,
): WindowRange {
  const startNs = Math.max(axis.startNs, axis.endNs - Math.max(1, Math.round(spanNs)));
  return { startNs, endNs: axis.endNs };
}
