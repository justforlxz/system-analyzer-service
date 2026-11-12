"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";

import { MetricTrackChart } from "@/components/timeline/metric-track-chart";
import { Button } from "@/components/ui/button";
import {
  describeError,
  fetchCursor,
  fetchServiceDiagnostics,
  type DiagnosticAnnotation,
  type DiagnosticFact,
  type DiagnosticJournalEntry,
  type DiagnosticLifecycleSpan,
  type DiagnosticResourceSeries,
  type MachineResponse,
  type MetricItem,
  type ServiceDiagnosticsResponse,
  type ServiceSummary,
} from "@/lib/api/client";
import {
  formatBootOffset,
  formatDuration,
  formatMetricValue,
  formatNsExact,
  formatWallClockUtc,
} from "@/lib/timeline/format";
import type { WindowRange } from "@/lib/timeline/scale";

const FETCH_DELAY_MS = 120;

const STATE_LABELS: Record<string, string> = {
  activating: "启动中",
  active: "运行中",
  inactive: "已停止",
  failed: "启动失败",
  unknown: "无法确定",
};

type DiagnosticsViewProps = {
  machine: MachineResponse;
  bootId: string;
  services: ServiceSummary[];
  initialDiagnostics: ServiceDiagnosticsResponse;
  axis: WindowRange | null;
};

function stateLabel(state: string): string {
  const label = STATE_LABELS[state];
  return label ? `${label}（${state}）` : state;
}

function factValue(fact: DiagnosticFact | undefined): string {
  if (!fact || fact.value === null || fact.value === undefined) return "无法确定";
  return fact.value;
}

function spanEndLabel(span: DiagnosticLifecycleSpan): string {
  if (span.end_monotonic_ns !== null) return formatBootOffset(span.end_monotonic_ns);
  if (span.end_is_declared) return "结束时刻未知（只有墙钟收尾）";
  return "未闭合（采集结束时仍在进行）";
}

function spanIsActiveAt(span: DiagnosticLifecycleSpan, atNs: number): boolean {
  if (span.monotonic_ns === null) return false;
  if (span.monotonic_ns > atNs) return false;
  return span.end_monotonic_ns === null || atNs < span.end_monotonic_ns;
}

function axisForResources(
  axis: WindowRange | null,
  series: DiagnosticResourceSeries[],
): WindowRange {
  if (axis !== null) return axis;
  const moments = series.flatMap((item) => item.points.map((point) => point.monotonic_ns));
  if (moments.length === 0) return { startNs: 0, endNs: 1 };
  const startNs = Math.min(...moments);
  const endNs = Math.max(...moments);
  return startNs === endNs ? { startNs, endNs: startNs + 1 } : { startNs, endNs };
}

function seriesPoints(series: DiagnosticResourceSeries): MetricItem[] {
  return series.points.map((point) => ({
    type: "metric",
    record_id: `${series.entity}:${point.monotonic_ns}`,
    collection_id: "diagnostics",
    monotonic_ns: point.monotonic_ns,
    realtime_ns: null,
    name: series.name,
    unit: series.unit,
    value: point.value,
    entity: series.entity,
    source_plugin: "diagnostics",
    stream_id: series.entity,
  }));
}

type IdentityFactProps = { name: string; label: string; fact: DiagnosticFact | undefined };

function IdentityFact({ name, label, fact }: IdentityFactProps) {
  const missing = !fact || fact.value === null || fact.value === undefined;
  return (
    <div
      className="rounded-lg border border-slate-200 bg-white px-3 py-2"
      data-testid="identity-fact"
      data-field={name}
      data-missing={missing ? "true" : "false"}
    >
      <dt className="text-[11px] uppercase tracking-wide text-slate-500">{label}</dt>
      <dd className="mt-0.5 font-mono text-sm text-slate-900">{factValue(fact)}</dd>
      {fact?.note ? <p className="mt-1 text-[11px] text-amber-700">{fact.note}</p> : null}
      {fact?.record_id ? (
        <p className="mt-1 truncate font-mono text-[11px] text-slate-500">
          来源记录 {fact.record_id}
        </p>
      ) : null}
    </div>
  );
}

function JournalRow({
  entry,
  highlighted,
}: {
  entry: DiagnosticJournalEntry;
  highlighted: boolean;
}) {
  return (
    <li
      id={`journal-${entry.record_id}`}
      data-testid="journal-entry"
      data-record-id={entry.record_id}
      data-highlighted={highlighted ? "true" : "false"}
      className={
        highlighted
          ? "rounded-md border border-amber-300 bg-amber-50 px-3 py-2"
          : "rounded-md border border-slate-200 bg-white px-3 py-2"
      }
    >
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-[11px] text-slate-500">
        <span className="font-mono">{formatBootOffset(entry.monotonic_ns)}</span>
        <span className="font-mono">{formatNsExact(entry.monotonic_ns)}</span>
        {entry.realtime_ns !== null ? (
          <span className="font-mono">{formatWallClockUtc(entry.realtime_ns)}</span>
        ) : null}
        {entry.identifier ? <span>{entry.identifier}</span> : null}
        {entry.unit ? <span>{entry.unit}</span> : null}
        {entry.priority ? <span>优先级 {entry.priority}</span> : null}
        {entry.from_evidence ? (
          <span className="rounded bg-amber-100 px-1.5 py-0.5 text-amber-800">结论证据</span>
        ) : null}
      </div>
      <p className="mt-1 break-words font-mono text-xs text-slate-800">
        {entry.message ?? "（这条记录没有消息正文）"}
      </p>
      <p className="mt-1 truncate font-mono text-[10px] text-slate-400">{entry.record_id}</p>
    </li>
  );
}

function AnnotationCard({
  annotation,
  onJumpToJournal,
  onJumpToMoment,
  onJumpToResources,
}: {
  annotation: DiagnosticAnnotation;
  onJumpToJournal: (recordId: string) => void;
  onJumpToMoment: (moment: number) => void;
  onJumpToResources: (moment: number) => void;
}) {
  const confidence =
    annotation.confidence === null ? "置信度无法确定" : `置信度 ${annotation.confidence}`;
  return (
    <li
      className="rounded-lg border border-slate-200 bg-white px-4 py-3"
      data-testid="annotation-card"
      data-annotation-type={annotation.annotation_type}
    >
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <span className="font-mono text-xs font-medium text-slate-900">
          {annotation.annotation_type}
        </span>
        <span className="text-[11px] text-slate-500">{annotation.analyzer}</span>
        <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-700">
          {annotation.severity}
        </span>
        <span className="text-[11px] text-slate-600">{confidence}</span>
        {annotation.monotonic_ns !== null ? (
          <span className="font-mono text-[11px] text-slate-500">
            {formatBootOffset(annotation.monotonic_ns)}
          </span>
        ) : null}
      </div>
      <p className="mt-1 text-sm text-slate-800">{annotation.message ?? "结论内容无法确定"}</p>
      <div className="mt-2">
        <p className="text-[11px] font-medium text-slate-600">原始证据</p>
        {annotation.evidence.length === 0 ? (
          <p className="mt-1 text-xs text-amber-700">无法确定：该结论没有引用原始记录</p>
        ) : (
          <ul className="mt-1 space-y-1">
            {annotation.evidence.map((evidence) => (
              <li key={evidence.record_id} data-testid="annotation-evidence">
                {evidence.found ? (
                  <button
                    type="button"
                    className="w-full rounded border border-slate-200 bg-slate-50 px-2 py-1 text-left text-xs text-slate-700 hover:border-slate-400"
                    onClick={() => onJumpToJournal(evidence.record_id)}
                  >
                    <span className="font-mono text-[10px] text-slate-500">
                      {evidence.monotonic_ns === null
                        ? evidence.record_id
                        : `${formatBootOffset(evidence.monotonic_ns)} · ${evidence.record_id}`}
                    </span>
                    <span className="mt-0.5 block break-words font-mono text-slate-800">
                      {evidence.message ?? "（证据记录没有消息正文）"}
                    </span>
                  </button>
                ) : (
                  <p className="rounded border border-amber-300 bg-amber-50 px-2 py-1 text-xs text-amber-800">
                    无法确定：记录 <span className="font-mono">{evidence.record_id}</span> 不在库里
                  </p>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
      <div className="mt-2 flex flex-wrap gap-2">
        <Button
          variant="outline"
          size="sm"
          disabled={annotation.monotonic_ns === null}
          onClick={() =>
            annotation.monotonic_ns !== null && onJumpToMoment(annotation.monotonic_ns)
          }
        >
          定位到结论时刻
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={annotation.monotonic_ns === null}
          onClick={() =>
            annotation.monotonic_ns !== null && onJumpToResources(annotation.monotonic_ns)
          }
        >
          查看该时刻资源
        </Button>
      </div>
    </li>
  );
}

/** 服务诊断视图：游标位置决定状态、资源读数与关联 Journal，结论始终附带原始证据。 */
export function DiagnosticsView({
  machine,
  bootId,
  services,
  initialDiagnostics,
  axis,
}: DiagnosticsViewProps) {
  const [entityId, setEntityId] = useState(initialDiagnostics.entity);
  const [diagnostics, setDiagnostics] = useState(initialDiagnostics);
  const [atNs, setAtNs] = useState(initialDiagnostics.at_ns);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [highlightedRecord, setHighlightedRecord] = useState<string | null>(null);
  const [scrollToResources, setScrollToResources] = useState(0);
  const requestRef = useRef(0);
  const sliderValue = axis === null ? atNs : Math.min(Math.max(atNs, axis.startNs), axis.endNs);

  useEffect(() => {
    if (entityId === diagnostics.entity && atNs === diagnostics.at_ns) return;
    const requestId = requestRef.current + 1;
    requestRef.current = requestId;
    const timer = setTimeout(() => {
      setPending(true);
      fetchServiceDiagnostics(machine.machine_id, bootId, entityId, { atNs })
        .then((next) => {
          if (requestId !== requestRef.current) return;
          setDiagnostics(next);
          setAtNs(next.at_ns);
          setError(null);
        })
        .catch((caught) => {
          if (requestId !== requestRef.current) return;
          setError(describeError(caught));
        })
        .finally(() => {
          if (requestId === requestRef.current) setPending(false);
        });
    }, FETCH_DELAY_MS);
    return () => clearTimeout(timer);
  }, [atNs, bootId, diagnostics.at_ns, diagnostics.entity, entityId, machine.machine_id]);

  useEffect(() => {
    if (highlightedRecord === null) return;
    document.getElementById(`journal-${highlightedRecord}`)?.scrollIntoView?.({
      block: "center",
      behavior: "smooth",
    });
  }, [diagnostics.journal.items, highlightedRecord]);

  useEffect(() => {
    if (scrollToResources === 0) return;
    document.getElementById("diagnostic-resources")?.scrollIntoView?.({
      block: "start",
      behavior: "smooth",
    });
  }, [scrollToResources]);

  const stepCursor = async (forward: boolean) => {
    setPending(true);
    try {
      const next = await fetchCursor(machine.machine_id, bootId, { atNs, forward });
      if (next.position_ns !== null) setAtNs(next.position_ns);
      setError(null);
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setPending(false);
    }
  };

  const changeService = (nextEntity: string) => {
    setEntityId(nextEntity);
    setAtNs(diagnostics.at_ns);
    setHighlightedRecord(null);
  };

  const jumpToJournal = (recordId: string) => {
    setHighlightedRecord(recordId);
  };

  const jumpToMoment = (moment: number) => {
    setAtNs(moment);
  };

  const jumpToResources = (moment: number) => {
    setAtNs(moment);
    setScrollToResources((value) => value + 1);
  };

  const resourceAxis = useMemo(
    () => axisForResources(axis, diagnostics.resources.series),
    [axis, diagnostics.resources.series],
  );
  const activeSpanRecord = useMemo(() => {
    const active = diagnostics.lifecycle.find((span) => spanIsActiveAt(span, atNs));
    return active?.record_id ?? null;
  }, [atNs, diagnostics.lifecycle]);

  return (
    <section className="flex flex-col gap-5" data-testid="diagnostics-view">
      <header className="flex flex-col gap-3">
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h1 className="text-xl font-semibold text-slate-900">服务状态与异常诊断</h1>
          <span className="text-sm text-slate-700">{machine.hostname}</span>
          <span className="font-mono text-xs text-slate-500">{machine.machine_id}</span>
          <span className="font-mono text-xs text-slate-500">启动 {bootId}</span>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <label className="text-slate-600" htmlFor="diagnostic-service">
            服务
          </label>
          <select
            id="diagnostic-service"
            data-testid="service-select"
            className="rounded-md border border-slate-300 bg-white px-2 py-1 font-mono text-xs"
            value={entityId}
            onChange={(event) => changeService(event.target.value)}
          >
            {services.map((service) => (
              <option key={service.entity} value={service.entity}>
                {service.unit}
              </option>
            ))}
          </select>
          <Link
            className="underline"
            href={`/timeline?machine_id=${encodeURIComponent(machine.machine_id)}&boot_id=${encodeURIComponent(bootId)}`}
          >
            回到时间线
          </Link>
        </div>
        {axis !== null ? (
          <div className="flex flex-col gap-2 rounded-lg border border-slate-200 bg-white px-4 py-3">
            <div className="flex flex-wrap items-center gap-2">
              <Button variant="outline" size="sm" onClick={() => void stepCursor(false)}>
                上一帧
              </Button>
              <Button variant="outline" size="sm" onClick={() => void stepCursor(true)}>
                下一帧
              </Button>
              <span className="font-mono text-xs text-slate-700" data-testid="diagnostic-cursor">
                游标 {formatBootOffset(atNs)} · {formatNsExact(atNs)}
              </span>
              {pending ? <span className="text-xs text-slate-500">读取中…</span> : null}
            </div>
            <input
              type="range"
              aria-label="时间游标"
              data-testid="cursor-slider"
              className="w-full"
              min={axis.startNs}
              max={axis.endNs}
              step={Math.max(1, Math.round((axis.endNs - axis.startNs) / 4000))}
              value={sliderValue}
              onChange={(event) => setAtNs(Number(event.target.value))}
            />
            <div className="flex justify-between font-mono text-[11px] text-slate-500">
              <span>{formatBootOffset(axis.startNs)}</span>
              <span>{formatBootOffset(axis.endNs)}</span>
            </div>
            <p className="text-[11px] text-slate-500">
              上一帧 / 下一帧使用时间线接口的可停靠时刻；拖动滑块按当前位置重建服务状态。
            </p>
          </div>
        ) : null}
      </header>

      {error ? (
        <p
          className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700"
          data-testid="diagnostic-error"
        >
          {error}
        </p>
      ) : null}

      <section
        className="rounded-lg border border-slate-200 bg-white px-4 py-3"
        data-testid="diagnostic-state"
      >
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h2 className="text-sm font-medium text-slate-900">游标时刻状态</h2>
          <span className="text-sm text-slate-800">{stateLabel(diagnostics.state.state)}</span>
          {diagnostics.state.since_ns !== null ? (
            <span className="font-mono text-[11px] text-slate-500">
              自 {formatBootOffset(diagnostics.state.since_ns)}（
              {formatNsExact(diagnostics.state.since_ns)}）
            </span>
          ) : null}
        </div>
        {diagnostics.state.note ? (
          <p className="mt-1 text-xs text-amber-700">{diagnostics.state.note}</p>
        ) : null}
        {diagnostics.state.stale ? (
          <p className="mt-1 text-xs text-amber-700">
            采集已经结束，游标之后的这段时间没有新的生命周期记录，无法确定当时是否仍是这个状态。
          </p>
        ) : null}
      </section>

      <section className="flex flex-col gap-2" data-testid="diagnostic-identity">
        <h2 className="text-sm font-medium text-slate-900">进程身份</h2>
        <dl className="grid gap-2 sm:grid-cols-3">
          <IdentityFact name="pid" label="PID" fact={diagnostics.identity.pid} />
          <IdentityFact
            name="invocation_id"
            label="Invocation ID"
            fact={diagnostics.identity.invocation_id}
          />
          <IdentityFact name="cgroup" label="cgroup" fact={diagnostics.identity.cgroup} />
        </dl>
      </section>

      {diagnostics.uncertain.length > 0 ? (
        <section
          className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900"
          data-testid="diagnostic-uncertain"
        >
          <h2 className="font-medium">无法确定的信息</h2>
          <ul className="mt-1 list-disc space-y-1 pl-5 text-xs">
            {diagnostics.uncertain.map((item, index) => (
              <li key={`${item.field}-${index}`} data-field={item.field}>
                {item.note}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <section className="flex flex-col gap-2" data-testid="diagnostic-lifecycle">
        <h2 className="text-sm font-medium text-slate-900">服务生命周期</h2>
        {diagnostics.lifecycle.length === 0 ? (
          <p className="rounded-md border border-slate-200 bg-white px-3 py-2 text-xs text-slate-600">
            这次启动没有该服务的生命周期区间，无法确定启动与退出过程。
          </p>
        ) : (
          <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
            <table className="min-w-full text-left text-xs">
              <thead className="bg-slate-50 text-slate-500">
                <tr>
                  <th className="px-3 py-2 font-medium">类型</th>
                  <th className="px-3 py-2 font-medium">状态</th>
                  <th className="px-3 py-2 font-medium">开始</th>
                  <th className="px-3 py-2 font-medium">结束</th>
                  <th className="px-3 py-2 font-medium">时长</th>
                  <th className="px-3 py-2 font-medium">PID / Invocation / cgroup</th>
                </tr>
              </thead>
              <tbody>
                {diagnostics.lifecycle.map((span) => (
                  <tr
                    key={span.record_id}
                    data-testid="lifecycle-span"
                    data-active={span.record_id === activeSpanRecord ? "true" : "false"}
                    className={
                      span.record_id === activeSpanRecord
                        ? "border-t border-amber-200 bg-amber-50"
                        : "border-t border-slate-100"
                    }
                  >
                    <td className="px-3 py-2 font-mono">{span.span_type}</td>
                    <td className="px-3 py-2">{span.state ?? "无法确定"}</td>
                    <td className="px-3 py-2 font-mono">
                      {span.monotonic_ns === null
                        ? "无法确定"
                        : formatBootOffset(span.monotonic_ns)}
                    </td>
                    <td className="px-3 py-2 font-mono">{spanEndLabel(span)}</td>
                    <td className="px-3 py-2 font-mono">
                      {span.monotonic_ns !== null && span.end_monotonic_ns !== null
                        ? formatDuration(span.end_monotonic_ns - span.monotonic_ns)
                        : "无法确定"}
                    </td>
                    <td className="px-3 py-2 font-mono text-[11px] text-slate-600">
                      {[span.pid, span.invocation_id, span.cgroup]
                        .map((value) => value ?? "无法确定")
                        .join(" · ")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="flex flex-col gap-2" data-testid="diagnostic-annotations">
        <h2 className="text-sm font-medium text-slate-900">分析结论与原始证据</h2>
        {diagnostics.annotations.length === 0 ? (
          <p className="rounded-md border border-slate-200 bg-white px-3 py-2 text-xs text-slate-600">
            这次启动没有该服务的分析结论，无法确定是否存在启动失败或 OOM。
          </p>
        ) : (
          <ul className="flex flex-col gap-2">
            {diagnostics.annotations.map((annotation) => (
              <AnnotationCard
                key={annotation.record_id}
                annotation={annotation}
                onJumpToJournal={jumpToJournal}
                onJumpToMoment={jumpToMoment}
                onJumpToResources={jumpToResources}
              />
            ))}
          </ul>
        )}
      </section>

      <section
        className="flex flex-col gap-2"
        id="diagnostic-journal"
        data-testid="diagnostic-journal"
      >
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h2 className="text-sm font-medium text-slate-900">关联 Journal</h2>
          <span className="font-mono text-[11px] text-slate-500">
            窗口 {formatBootOffset(diagnostics.journal.window_start_ns)} →{" "}
            {formatBootOffset(diagnostics.journal.window_end_ns)} · {diagnostics.journal.total} 条
          </span>
        </div>
        {diagnostics.journal.items.length === 0 ? (
          <p className="rounded-md border border-slate-200 bg-white px-3 py-2 text-xs text-slate-600">
            该窗口内没有关联 Journal，无法确定更多信息。
          </p>
        ) : (
          <ul className="flex flex-col gap-1">
            {diagnostics.journal.items.map((entry) => (
              <JournalRow
                key={entry.record_id}
                entry={entry}
                highlighted={entry.record_id === highlightedRecord}
              />
            ))}
          </ul>
        )}
      </section>

      <section
        className="flex flex-col gap-2"
        id="diagnostic-resources"
        data-testid="diagnostic-resources"
      >
        <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
          <h2 className="text-sm font-medium text-slate-900">游标时刻资源使用</h2>
          <span className="font-mono text-[11px] text-slate-500">
            at_ns {formatNsExact(diagnostics.resources.at_ns)}
          </span>
        </div>
        {diagnostics.resources.latest.length > 0 ? (
          <ul className="grid gap-2 sm:grid-cols-3">
            {diagnostics.resources.latest.map((item) => (
              <li
                key={item.entity}
                className="rounded-lg border border-slate-200 bg-white px-3 py-2"
                data-testid="resource-latest"
                data-name={item.name}
              >
                <p className="text-[11px] text-slate-500">{item.name}</p>
                <p className="mt-0.5 font-mono text-sm text-slate-900">
                  {formatMetricValue(item.value, item.unit)}
                </p>
                <p className="mt-1 font-mono text-[10px] text-slate-500">
                  {formatBootOffset(item.monotonic_ns)} · {item.record_id}
                </p>
              </li>
            ))}
          </ul>
        ) : null}
        {diagnostics.resources.series.length > 0 ? (
          <div className="flex flex-col gap-2">
            {diagnostics.resources.series.map((series) => (
              <div
                key={series.entity}
                className="rounded-lg border border-slate-200 bg-white px-3 py-2"
                data-testid="resource-series"
              >
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <span className="text-xs font-medium text-slate-800">{series.name}</span>
                  <span className="font-mono text-[10px] text-slate-500">
                    {series.unit} · {series.points.length} 个采样点
                  </span>
                </div>
                <MetricTrackChart
                  points={seriesPoints(series)}
                  range={resourceAxis}
                  cursorNs={atNs}
                  unit={series.unit}
                  label={`${series.name}（${series.unit}）`}
                />
              </div>
            ))}
          </div>
        ) : null}
        {diagnostics.resources.unavailable.length > 0 ? (
          <ul className="flex flex-col gap-1">
            {diagnostics.resources.unavailable.map((item) => (
              <li
                key={item.entity}
                className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900"
                data-testid="resource-unavailable"
              >
                不可用：{item.name}（{item.unit}）—— {item.reason}
              </li>
            ))}
          </ul>
        ) : null}
        {diagnostics.resources.note ? (
          <p className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900">
            {diagnostics.resources.note}
          </p>
        ) : null}
        {diagnostics.resources.latest.length === 0 &&
        diagnostics.resources.series.length === 0 &&
        diagnostics.resources.unavailable.length === 0 &&
        diagnostics.resources.note === null ? (
          <p className="rounded-md border border-slate-200 bg-white px-3 py-2 text-xs text-slate-600">
            这次启动没有该服务的资源采样，无法确定资源使用。
          </p>
        ) : null}
      </section>
    </section>
  );
}
