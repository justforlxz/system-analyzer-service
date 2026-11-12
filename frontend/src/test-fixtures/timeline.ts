import type {
  ClockDriftResponse,
  CursorResponse,
  EventItem,
  MachineResponse,
  MetricItem,
  ResourceStatusResponse,
  SpanItem,
  TimelineStateResponse,
  TrackResponse,
  TrackStateResponse,
  TracksResponse,
} from "@/lib/api/client";

export const MACHINE_ID = "machine:test-host";
export const BOOT_ID = "boot-0001";
export const AXIS_START = 1_000_000_000;
export const AXIS_END = 9_000_000_000;
export const ANCHOR_NS = 1_794_009_600_000_000_000;

export const machine: MachineResponse = {
  machine_id: MACHINE_ID,
  hostname: "test-host",
  architecture: "x86_64",
  kernel_version: "6.6.0-1-amd64",
  os_release: "UOS Desktop 25 Professional",
};

export const clock: ClockDriftResponse = {
  anchor_ns: ANCHOR_NS,
  drift_ns: 0,
  anchor_count: 4,
  unreliable: false,
};

export const resourcesAvailable: ResourceStatusResponse = { available: true, reasons: [] };
export const resourcesUnavailable: ResourceStatusResponse = {
  available: false,
  reasons: ["journal_import：压缩包只含 Journal，没有采集 CPU、内存、磁盘 I/O"],
};

export function makeEvent(overrides: Partial<EventItem> = {}): EventItem {
  return {
    type: "event",
    record_id: "record-event",
    collection_id: "collection:one",
    monotonic_ns: 2_000_000_000,
    realtime_ns: ANCHOR_NS + 2_000_000_000,
    event_type: "systemd.unit.started",
    severity: "info",
    message: "Started nginx.service",
    attributes: {},
    entity: "service:nginx.service",
    source_plugin: "systemd",
    stream_id: "systemd.units",
    ...overrides,
  };
}

export function makeSpan(overrides: Partial<SpanItem> = {}): SpanItem {
  return {
    type: "span",
    record_id: "record-span",
    collection_id: "collection:one",
    monotonic_ns: 2_000_000_000,
    realtime_ns: ANCHOR_NS + 2_000_000_000,
    end_monotonic_ns: 6_000_000_000,
    end_realtime_ns: ANCHOR_NS + 6_000_000_000,
    end_is_declared: true,
    closed_by: null,
    span_type: "service.active",
    state: "active",
    attributes: {},
    entity: "service:nginx.service",
    source_plugin: "systemd",
    stream_id: "systemd.units",
    ...overrides,
  };
}

export function makeMetric(overrides: Partial<MetricItem> = {}): MetricItem {
  return {
    type: "metric",
    record_id: "record-metric",
    collection_id: "collection:one",
    monotonic_ns: 2_000_000_000,
    realtime_ns: ANCHOR_NS + 2_000_000_000,
    name: "cpu.total.utilization",
    unit: "percent",
    value: 12.5,
    entity: "metric:cpu.total.utilization",
    source_plugin: "resource-sampler",
    stream_id: "cpu",
    ...overrides,
  };
}

function trackBase(): Omit<TrackResponse, "track_id" | "kind"> {
  return {
    entity: null,
    items: [],
    name: null,
    source_plugin: null,
    status: "available",
    stream_id: null,
    unavailable_reason: null,
    unit: null,
  };
}

export function makeSpanTrack(overrides: Partial<TrackResponse> = {}): TrackResponse {
  return {
    ...trackBase(),
    track_id: "span:systemd:systemd.units:service:nginx.service",
    kind: "span",
    entity: "service:nginx.service",
    source_plugin: "systemd",
    stream_id: "systemd.units",
    items: [makeSpan()],
    ...overrides,
  };
}

export function makeEventTrack(overrides: Partial<TrackResponse> = {}): TrackResponse {
  return {
    ...trackBase(),
    track_id: "event:systemd:systemd.units:service:nginx.service",
    kind: "event",
    entity: "service:nginx.service",
    source_plugin: "systemd",
    stream_id: "systemd.units",
    items: [makeEvent()],
    ...overrides,
  };
}

export function makeMetricTrack(overrides: Partial<TrackResponse> = {}): TrackResponse {
  const items = overrides.items ?? [
    makeMetric({ monotonic_ns: 2_000_000_000, value: 12.5 }),
    makeMetric({ monotonic_ns: 3_000_000_000, value: 30.25, record_id: "record-metric-2" }),
  ];
  return {
    ...trackBase(),
    track_id: "metric:resource-sampler:cpu:metric:cpu.total.utilization",
    kind: "metric",
    entity: "metric:cpu.total.utilization",
    source_plugin: "resource-sampler",
    stream_id: "cpu",
    name: "cpu.total.utilization",
    unit: "percent",
    items,
    ...overrides,
  };
}

export function makeTracksResponse(overrides: Partial<TracksResponse> = {}): TracksResponse {
  const tracks = overrides.tracks ?? [makeSpanTrack(), makeEventTrack(), makeMetricTrack()];
  return {
    machine_id: MACHINE_ID,
    boot_id: BOOT_ID,
    axis_start_ns: AXIS_START,
    axis_end_ns: AXIS_END,
    window_start_ns: null,
    window_end_ns: null,
    tracks,
    items: tracks.flatMap((track) => track.items),
    wall_clock_items: [],
    resources: resourcesAvailable,
    clock,
    ...overrides,
  };
}

export function makeCursor(overrides: Partial<CursorResponse> = {}): CursorResponse {
  return {
    machine_id: MACHINE_ID,
    boot_id: BOOT_ID,
    start_ns: AXIS_START,
    end_ns: AXIS_END,
    position_ns: AXIS_START,
    previous_ns: null,
    next_ns: 2_000_000_000,
    ...overrides,
  };
}

export function makeTrackState(overrides: Partial<TrackStateResponse> = {}): TrackStateResponse {
  return {
    track_id: "metric:resource-sampler:cpu:metric:cpu.total.utilization",
    kind: "metric",
    entity: "metric:cpu.total.utilization",
    status: "available",
    unavailable_reason: null,
    metric: makeMetric({ value: 30.25 }),
    spans: [],
    last_event: null,
    ...overrides,
  };
}

export function makeState(overrides: Partial<TimelineStateResponse> = {}): TimelineStateResponse {
  return {
    machine_id: MACHINE_ID,
    boot_id: BOOT_ID,
    monotonic_ns: 3_000_000_000,
    tracks: [makeTrackState()],
    resources: resourcesAvailable,
    clock,
    ...overrides,
  };
}
