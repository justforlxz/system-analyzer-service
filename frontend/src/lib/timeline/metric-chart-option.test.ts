import { describe, expect, it } from "vitest";

import { buildMetricChartOption } from "@/lib/timeline/metric-chart-option";
import { AXIS_START, makeMetric } from "@/test-fixtures/timeline";

const range = { startNs: AXIS_START, endNs: 5_000_000_000 };

const points = [
  makeMetric({ monotonic_ns: 2_000_000_000, value: 12.5 }),
  makeMetric({ monotonic_ns: 3_000_000_000, value: 30.25, record_id: "r2" }),
];

type Option = {
  xAxis: { min: number; max: number; show: boolean };
  yAxis: { show: boolean };
  grid: { left: number; right: number };
  series: {
    type: string;
    data: [number, number][];
    markLine?: { data: { xAxis: number }[] };
  }[];
  tooltip: { formatter: (params: unknown) => string };
};

function optionOf(cursorNs: number | null): Option {
  return buildMetricChartOption({ points, range, cursorNs, unit: "percent" }) as unknown as Option;
}

describe("buildMetricChartOption", () => {
  it("横轴范围直接用后端给的窗口值", () => {
    const option = optionOf(null);
    expect(option.xAxis.min).toBe(range.startNs);
    expect(option.xAxis.max).toBe(range.endNs);
    expect(option.xAxis.show).toBe(false);
    expect(option.yAxis.show).toBe(false);
  });

  it("绘图区左右不留内边距，保证与其它轨道和游标的横坐标一致", () => {
    const option = optionOf(null);
    expect(option.grid.left).toBe(0);
    expect(option.grid.right).toBe(0);
  });

  it("采样点按 [monotonic_ns, value] 传给折线系列", () => {
    const option = optionOf(null);
    expect(option.series[0].type).toBe("line");
    expect(option.series[0].data).toEqual([
      [2_000_000_000, 12.5],
      [3_000_000_000, 30.25],
    ]);
  });

  it("游标在窗口内时画一条 markLine", () => {
    const option = optionOf(3_000_000_000);
    expect(option.series[0].markLine?.data).toEqual([{ xAxis: 3_000_000_000 }]);
  });

  it("游标在窗口外时不画 markLine", () => {
    expect(optionOf(range.endNs + 1).series[0].markLine).toBeUndefined();
    expect(optionOf(null).series[0].markLine).toBeUndefined();
  });

  it("提示框把时刻写成 boot 内偏移、数值按单位格式化", () => {
    const option = optionOf(null);
    expect(option.tooltip.formatter([{ data: [2_000_000_000, 12.5] }])).toBe(
      "T+2.000 s<br/>12.5 %",
    );
  });

  it("悬浮提示转义单位里的 HTML，不让导入数据变成标签", () => {
    const option = buildMetricChartOption({
      points,
      range,
      cursorNs: null,
      unit: '<img src=x onerror="alert(1)">',
    }) as unknown as Option;
    const html = option.tooltip.formatter([{ data: [2_000_000_000, 12.5] }]);
    expect(html).not.toContain("<img");
    expect(html).toContain("&lt;img");
    expect(html).toContain("12.5 &lt;img");
  });

  it("没有采样点时数据为空数组，不补 0", () => {
    const option = buildMetricChartOption({
      points: [],
      range,
      cursorNs: null,
      unit: "percent",
    }) as unknown as Option;
    expect(option.series[0].data).toEqual([]);
  });
});
