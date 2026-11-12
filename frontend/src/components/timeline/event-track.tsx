import type { EventItem } from "@/lib/api/client";
import { formatBootOffset, formatWallClockUtc } from "@/lib/timeline/format";
import type { WindowRange } from "@/lib/timeline/scale";
import { nsToFraction } from "@/lib/timeline/scale";

const VIEW_WIDTH = 1000;
const VIEW_HEIGHT = 22;
const BAR_WIDTH = 4;

const SEVERITY_FILL: Record<string, string> = {
  emergency: "#7f1d1d",
  alert: "#991b1b",
  critical: "#b91c1c",
  error: "#dc2626",
  warning: "#d97706",
  notice: "#0284c7",
  info: "#0ea5e9",
  debug: "#94a3b8",
};

function fillFor(event: EventItem): string {
  return SEVERITY_FILL[event.severity] ?? "#475569";
}

/** 事件轨道：自绘 SVG，事件按 `monotonic_ns` 落在同一条时间轴上。 */
export function EventTrack({
  events,
  range,
  testId,
}: {
  events: EventItem[];
  range: WindowRange;
  testId?: string;
}) {
  const marks = events
    .filter((event) => event.monotonic_ns !== null)
    .map((event) => ({
      event,
      x: Math.min(Math.max(nsToFraction(event.monotonic_ns as number, range), 0), 1) * VIEW_WIDTH,
    }));

  return (
    <svg
      data-testid={testId ?? "event-track"}
      viewBox={`0 0 ${VIEW_WIDTH} ${VIEW_HEIGHT}`}
      preserveAspectRatio="none"
      role="img"
      aria-label={`事件轨道，共 ${marks.length} 个事件`}
      className="h-6 w-full"
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
      {marks.map(({ event, x }, index) => (
        <g key={`${event.record_id}-${index}`}>
          <rect
            x={x - BAR_WIDTH / 2}
            y={4}
            width={BAR_WIDTH}
            height={VIEW_HEIGHT - 8}
            fill={fillFor(event)}
          >
            <title>
              {[
                formatBootOffset(event.monotonic_ns as number),
                event.realtime_ns !== null ? formatWallClockUtc(event.realtime_ns) : null,
                event.event_type,
                event.message,
              ]
                .filter(Boolean)
                .join(" · ")}
            </title>
          </rect>
        </g>
      ))}
    </svg>
  );
}
