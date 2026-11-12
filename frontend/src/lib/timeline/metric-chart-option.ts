import type { MetricItem } from "@/lib/api/client";
import { escapeHtml, formatBootOffset, formatMetricValue } from "@/lib/timeline/format";
import type { WindowRange } from "@/lib/timeline/scale";
import { isInsideWindow } from "@/lib/timeline/scale";

export type MetricChartInput = {
  points: readonly MetricItem[];
  range: WindowRange;
  cursorNs: number | null;
  unit: string | null;
};

/** 资源曲线用 ECharts 的配置。
 *
 * 坐标轴范围直接用后端给的窗口（`window_start_ns` / `window_end_ns`），隐藏坐标轴文字：
 * Y 轴的量纲与当前读数写在轨道标签里，这样所有轨道的绘图区宽度一致，共用的时间游标
 * 才能一条直线贯穿到底。
 */
export function buildMetricChartOption(input: MetricChartInput): Record<string, unknown> {
  const { points, range, cursorNs, unit } = input;
  const data = points
    .filter((point) => point.monotonic_ns !== null)
    .map((point) => [point.monotonic_ns as number, point.value]);

  const markLine =
    cursorNs !== null && isInsideWindow(cursorNs, range)
      ? {
          silent: true,
          symbol: "none",
          animation: false,
          label: { show: false },
          lineStyle: { color: "#0f172a", width: 1, type: "solid" },
          data: [{ xAxis: cursorNs }],
        }
      : undefined;

  return {
    animation: false,
    grid: { left: 0, right: 0, top: 4, bottom: 4, containLabel: false },
    xAxis: {
      type: "value",
      min: range.startNs,
      max: range.endNs,
      show: false,
    },
    yAxis: {
      type: "value",
      scale: true,
      show: false,
    },
    tooltip: {
      trigger: "axis",
      confine: true,
      formatter: (params: unknown) => {
        const list = Array.isArray(params) ? params : [params];
        const first = list[0] as { data?: [number, number] } | undefined;
        const pair = first?.data;
        if (!Array.isArray(pair)) return "";
        // ECharts 把 formatter 的返回值当 HTML 插入；unit 来自导入数据，必须转义
        return `${escapeHtml(formatBootOffset(pair[0]))}<br/>${escapeHtml(
          formatMetricValue(pair[1], unit),
        )}`;
      },
    },
    series: [
      {
        type: "line",
        step: false,
        smooth: false,
        showSymbol: true,
        symbolSize: 4,
        lineStyle: { width: 1.5, color: "#0f766e" },
        itemStyle: { color: "#0f766e" },
        areaStyle: { color: "rgba(15, 118, 110, 0.12)" },
        data,
        markLine,
      },
    ],
  };
}
