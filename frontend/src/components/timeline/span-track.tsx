import type { SpanItem } from "@/lib/api/client";
import type { WindowRange } from "@/lib/timeline/scale";
import { clipSpan, spanEndLabel } from "@/lib/timeline/tracks";

const VIEW_WIDTH = 1000;
const VIEW_HEIGHT = 28;
const BAR_TOP = 6;
const BAR_HEIGHT = 16;

const STATE_FILL: Record<string, string> = {
  active: "#0f766e",
  failed: "#dc2626",
  inactive: "#94a3b8",
};

function fillFor(span: SpanItem): string {
  const key = span.state ?? "";
  return STATE_FILL[key] ?? "#475569";
}

/** 生命周期区间轨道：自绘 SVG，横向比例与所有轨道、游标共用同一个窗口。 */
export function SpanTrack({
  spans,
  range,
  testId,
}: {
  spans: SpanItem[];
  range: WindowRange;
  testId?: string;
}) {
  const bars = spans
    .map((span) => ({ span, clip: clipSpan(span, range) }))
    .filter(
      (entry): entry is { span: SpanItem; clip: NonNullable<ReturnType<typeof clipSpan>> } =>
        entry.clip !== null,
    );

  return (
    <svg
      data-testid={testId ?? "span-track"}
      viewBox={`0 0 ${VIEW_WIDTH} ${VIEW_HEIGHT}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={`区间轨道，共 ${bars.length} 段`}
      className="h-7 w-full"
    >
      <line
        x1={0}
        y1={VIEW_HEIGHT / 2}
        x2={VIEW_WIDTH}
        y2={VIEW_HEIGHT / 2}
        stroke="#e2e8f0"
        strokeWidth={1}
        vectorEffect="non-scaling-stroke"
      />
      {bars.map(({ span, clip }, index) => {
        const x = clip.startFraction * VIEW_WIDTH;
        const width = Math.max((clip.endFraction - clip.startFraction) * VIEW_WIDTH, 2);
        const endX = x + width;
        const note = spanEndLabel(span);
        return (
          <g key={`${span.record_id}-${index}`}>
            <rect
              x={x}
              y={BAR_TOP}
              width={width}
              height={BAR_HEIGHT}
              rx={2}
              fill={fillFor(span)}
              opacity={0.85}
            >
              <title>{[span.span_type, span.state, note].filter(Boolean).join(" · ")}</title>
            </rect>
            {clip.openEnd ? (
              <line
                x1={endX}
                y1={BAR_TOP - 2}
                x2={endX}
                y2={BAR_TOP + BAR_HEIGHT + 2}
                stroke="#dc2626"
                strokeWidth={2}
                strokeDasharray="4 3"
                vectorEffect="non-scaling-stroke"
              >
                <title>{note || "结束时刻未知"}</title>
              </line>
            ) : null}
          </g>
        );
      })}
    </svg>
  );
}
