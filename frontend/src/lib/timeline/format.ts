/** 时间与指标数值的显示格式。
 *
 * 所有数值都来自后端字段：`monotonic_ns` 原样显示整数，`realtime_ns` 按 Unix 纪元
 * 显示墙钟文字。前端不重算时间，只决定怎么写成给人看的文字。
 */

const HTML_ESCAPES: Record<string, string> = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};

/** 把文字转义成可以安全放进 HTML 的片段。
 *
 * 指标单位与指标名来自导入的 `.bootrec`，导入校验只要求非空，不限制字符集；ECharts 的
 * `tooltip.formatter` 返回的字符串按 HTML 插入，因此拼进去之前必须转义。
 */
export function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (character) => HTML_ESCAPES[character] ?? character);
}

/** 纳秒整数的原样写法，用于「界面值 / 后端值」逐项对照。 */
export function formatNsExact(ns: number): string {
  return String(Math.round(ns));
}

const BOOT_OFFSET_LIMIT_NS = 60_000_000_000;

/** boot 内位置的相对写法：`T+1.200 s`；超过一分钟再补上分钟。 */
export function formatBootOffset(ns: number): string {
  const sign = ns < 0 ? "-" : "+";
  const abs = Math.abs(ns);
  const totalSeconds = abs / 1e9;
  if (abs >= BOOT_OFFSET_LIMIT_NS) {
    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds - minutes * 60;
    return `T${sign}${minutes} min ${seconds.toFixed(3)} s`;
  }
  return `T${sign}${totalSeconds.toFixed(3)} s`;
}

/** 时长写法：小于 1 微秒用纳秒，小于 1 毫秒用微秒，小于 1 秒用毫秒，其余用秒。 */
export function formatDuration(ns: number): string {
  const abs = Math.abs(ns);
  const sign = ns < 0 ? "-" : "";
  if (abs < 1_000) return `${sign}${abs} ns`;
  if (abs < 1_000_000) return `${sign}${(abs / 1_000).toFixed(1)} μs`;
  if (abs < 1_000_000_000) return `${sign}${(abs / 1_000_000).toFixed(1)} ms`;
  return `${sign}${(abs / 1e9).toFixed(3)} s`;
}

/** 墙钟读数按 UTC 写成 `YYYY-MM-DD HH:MM:SS.mmm`。
 *
 * 用 UTC 而不是本地时区：墙钟读数来自目标机器，显示成浏览器本地时区会把
 * 「目标机器几点」和「我这里几点」混在一起。毫秒已经远超 4.2 的精度需求。
 */
export function formatWallClockUtc(realtimeNs: number): string {
  const date = new Date(Math.round(realtimeNs / 1e6));
  if (Number.isNaN(date.getTime())) return "-";
  const iso = date.toISOString();
  return `${iso.slice(0, 10)} ${iso.slice(11, 23)} UTC`;
}

const BYTE_UNITS = ["B", "KiB", "MiB", "GiB", "TiB"];

/** 二进制字节单位；`unit` 是 `bytes` 或 `bytes_per_second` 时使用。 */
export function formatBytes(value: number, perSecond = false): string {
  if (!Number.isFinite(value)) return "-";
  const sign = value < 0 ? "-" : "";
  let scaled = Math.abs(value);
  let index = 0;
  while (scaled >= 1024 && index < BYTE_UNITS.length - 1) {
    scaled /= 1024;
    index += 1;
  }
  const digits = index === 0 ? 0 : 2;
  return `${sign}${scaled.toFixed(digits)} ${BYTE_UNITS[index]}${perSecond ? "/s" : ""}`;
}

/** 指标值的显示写法，按后端给的 `unit` 选择格式，缺省时退化成「值 + 单位」。 */
export function formatMetricValue(value: number, unit: string | null): string {
  if (!Number.isFinite(value)) return "-";
  if (unit === "percent") return `${value.toFixed(1)} %`;
  if (unit === "bytes") return formatBytes(value);
  if (unit === "bytes_per_second") return formatBytes(value, true);
  if (unit && unit.length > 0) return `${value} ${unit}`;
  return String(value);
}
