"use client";

import { LineChart } from "echarts/charts";
import { GridComponent, MarkLineComponent, TooltipComponent } from "echarts/components";
import * as echarts from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { useEffect, useRef } from "react";

import type { MetricItem } from "@/lib/api/client";
import { buildMetricChartOption } from "@/lib/timeline/metric-chart-option";
import type { WindowRange } from "@/lib/timeline/scale";

echarts.use([LineChart, GridComponent, MarkLineComponent, TooltipComponent, CanvasRenderer]);

export type MetricTrackChartProps = {
  points: MetricItem[];
  range: WindowRange;
  cursorNs: number | null;
  unit: string | null;
  label: string;
};

/** 资源轨道：主力曲线用 Apache ECharts 画，横轴范围与其它轨道完全一致。 */
export function MetricTrackChart({ points, range, cursorNs, unit, label }: MetricTrackChartProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);

  useEffect(() => {
    const element = containerRef.current;
    if (!element) return;
    const chart = echarts.init(element, undefined, { renderer: "canvas" });
    chartRef.current = chart;
    let observer: ResizeObserver | null = null;
    if (typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver(() => chart.resize());
      observer.observe(element);
    }
    return () => {
      observer?.disconnect();
      chart.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    chart.setOption(buildMetricChartOption({ points, range, cursorNs, unit }), true);
  }, [points, range, cursorNs, unit]);

  return (
    <div
      ref={containerRef}
      data-testid="metric-chart"
      role="img"
      aria-label={`${label} 资源曲线`}
      className="h-16 w-full"
    />
  );
}
