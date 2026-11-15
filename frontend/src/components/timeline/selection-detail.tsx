"use client";

import { useEffect, type ReactNode } from "react";

import type { EventItem, SpanItem } from "@/lib/api/client";
import {
  formatBootOffset,
  formatDuration,
  formatNsExact,
  formatWallClockUtc,
} from "@/lib/timeline/format";
import { spanEndLabel } from "@/lib/timeline/tracks";

/** 时间线上当前选中的条目：事件刻度或生命周期区间。 */
export type TimelineSelection =
  { kind: "event"; item: EventItem } | { kind: "span"; item: SpanItem };

function Row({
  label,
  testId,
  mono = false,
  children,
}: {
  label: string;
  testId: string;
  mono?: boolean;
  children: ReactNode;
}) {
  return (
    <div>
      <dt className="text-slate-500">{label}</dt>
      <dd
        className={`mt-0.5 break-words text-slate-800 ${mono ? "font-mono text-[11px]" : ""}`}
        data-testid={testId}
      >
        {children}
      </dd>
    </div>
  );
}

function instantText(ns: number, realtime: number | null): string {
  const parts = [`${formatBootOffset(ns)} · ${formatNsExact(ns)} ns`];
  if (realtime !== null) parts.push(formatWallClockUtc(realtime));
  return parts.join(" · ");
}

function EventDetail({ event }: { event: EventItem }) {
  return (
    <dl className="grid grid-cols-1 gap-x-6 gap-y-2 px-4 py-3 text-xs sm:grid-cols-2">
      <Row label="事件类型" testId="detail-event-type">
        {event.event_type}
      </Row>
      <Row label="严重级别" testId="detail-severity">
        {event.severity}
      </Row>
      <Row label="消息" testId="detail-message">
        {event.message ?? "无"}
      </Row>
      <Row label="实体" testId="detail-entity">
        {event.entity ?? "未标注"}
      </Row>
      <Row label="时刻（boot 内偏移 · 纳秒）" testId="detail-monotonic-ns" mono>
        {event.monotonic_ns === null
          ? "无（不在 boot 时间轴上）"
          : instantText(event.monotonic_ns, null)}
      </Row>
      <Row label="墙钟时刻（realtime_ns）" testId="detail-realtime-ns" mono>
        {event.realtime_ns === null
          ? "无"
          : `${formatWallClockUtc(event.realtime_ns)} · ${formatNsExact(event.realtime_ns)} ns`}
      </Row>
      <Row label="记录号（record_id）" testId="detail-record-id" mono>
        {event.record_id}
      </Row>
      <Row label="采集号（collection_id）" testId="detail-collection-id" mono>
        {event.collection_id}
      </Row>
    </dl>
  );
}

function SpanDetail({ span }: { span: SpanItem }) {
  const start = span.monotonic_ns;
  const end = span.end_monotonic_ns;
  // 后端会给未闭合的区间补一个窗口边缘作为绘制用的结束位置，并把 `end_is_declared`
  // 置为 false。那个值不是记录声明的结束时刻，详情只能写「未闭合」，不能当作真实结束。
  const endDeclared = span.end_is_declared && end !== null;
  const duration =
    start !== null && endDeclared && end !== null ? formatDuration(end - start) : "未知";
  return (
    <dl className="grid grid-cols-1 gap-x-6 gap-y-2 px-4 py-3 text-xs sm:grid-cols-2">
      <Row label="区间类型（span_type）" testId="detail-span-type">
        {span.span_type}
      </Row>
      <Row label="状态（state）" testId="detail-state">
        {span.state ?? "无"}
      </Row>
      <Row label="开始时刻（boot 内偏移 · 纳秒）" testId="detail-start" mono>
        {start === null ? "无（不在 boot 时间轴上）" : instantText(start, span.realtime_ns)}
      </Row>
      <Row label="结束时刻（boot 内偏移 · 纳秒）" testId="detail-end" mono>
        {!endDeclared
          ? spanEndLabel(span) || "结束时刻未知"
          : instantText(end as number, span.end_realtime_ns)}
      </Row>
      <Row label="持续时长" testId="detail-duration">
        {duration}
      </Row>
      <Row label="实体" testId="detail-entity">
        {span.entity ?? "未标注"}
      </Row>
      <Row label="记录号（record_id）" testId="detail-record-id" mono>
        {span.record_id}
      </Row>
      <Row label="采集号（collection_id）" testId="detail-collection-id" mono>
        {span.collection_id}
      </Row>
    </dl>
  );
}

/** 选中条目的详情面板。固定在轨道下方，不遮挡任何轨道；窄屏时单列排布。 */
export function SelectionDetail({
  selection,
  onClear,
}: {
  selection: TimelineSelection;
  onClear: () => void;
}) {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClear();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClear]);

  return (
    <section
      className="rounded-lg border border-slate-300 bg-white shadow-sm"
      data-testid="selection-detail"
    >
      <div className="flex items-center justify-between border-b border-slate-100 px-4 py-2">
        <h2 className="text-xs font-medium text-slate-700">
          选中：{selection.kind === "event" ? "事件" : "生命周期区间"}
        </h2>
        <button
          type="button"
          className="rounded px-2 py-0.5 text-xs text-slate-500 hover:bg-slate-100 hover:text-slate-800"
          onClick={onClear}
          data-testid="selection-close"
        >
          关闭（Esc）
        </button>
      </div>
      {selection.kind === "event" ? (
        <EventDetail event={selection.item} />
      ) : (
        <SpanDetail span={selection.item} />
      )}
    </section>
  );
}
