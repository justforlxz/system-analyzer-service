import { apiBaseUrl, apiRequestBaseUrl } from "@/lib/api/config";
import type { components } from "@/lib/api/schema";

export type ClockDriftResponse = components["schemas"]["ClockDriftResponse"];
export type CollectionResponse = components["schemas"]["CollectionResponse"];
export type CursorResponse = components["schemas"]["CursorResponse"];
export type MachineResponse = components["schemas"]["MachineResponse"];
export type TimelineStateResponse = components["schemas"]["TimelineStateResponse"];
export type TracksResponse = components["schemas"]["TracksResponse"];
export type ResourceStatusResponse = components["schemas"]["ResourceStatusResponse"];
export type TrackResponse = components["schemas"]["TrackResponse"];
export type EventItem = components["schemas"]["EventItemResponse"];
export type SpanItem = components["schemas"]["SpanItemResponse"];
export type MetricItem = components["schemas"]["MetricItemResponse"];
export type TrackStatus = components["schemas"]["TrackStatus"];
export type TrackStateResponse = components["schemas"]["TrackStateResponse"];

export type PageOf<T> = { items: T[]; limit: number; offset: number; total: number };

/** FastAPI 的统一错误信封：`{"error": {"type", "message", "details"}}`。 */
type ErrorEnvelope = {
  error?: { type?: string; message?: string; details?: string[] | null };
};

/** 后端返回的业务错误，保留错误分类与逐条说明，界面据此给出可读文案。 */
export class ApiError extends Error {
  readonly status: number;
  readonly type: string;
  readonly details: string[];

  constructor(status: number, type: string, message: string, details: string[] = []) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.type = type;
    this.details = details;
  }
}

export function describeError(error: unknown): string {
  if (error instanceof ApiError) {
    const details = error.details.length > 0 ? `（${error.details.join("；")}）` : "";
    return `${error.message}${details}`;
  }
  if (error instanceof Error) {
    return `${error.message}。后端地址：${apiBaseUrl()}`;
  }
  return `请求失败：${String(error)}`;
}

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${apiRequestBaseUrl()}${path}`, {
    cache: "no-store",
    headers: { accept: "application/json" },
  });
  if (!response.ok) {
    let envelope: ErrorEnvelope = {};
    try {
      envelope = (await response.json()) as ErrorEnvelope;
    } catch {
      envelope = {};
    }
    throw new ApiError(
      response.status,
      envelope.error?.type ?? "unknown",
      envelope.error?.message ?? `请求失败（HTTP ${response.status}）`,
      envelope.error?.details ?? [],
    );
  }
  return (await response.json()) as T;
}

function timelinePath(machineId: string, bootId: string): string {
  return `/api/machines/${encodeURIComponent(machineId)}/boots/${encodeURIComponent(bootId)}/timeline`;
}

export function fetchMachine(machineId: string): Promise<MachineResponse> {
  return getJson<MachineResponse>(`/api/machines/${encodeURIComponent(machineId)}`);
}

export function fetchCollections(limit = 50): Promise<PageOf<CollectionResponse>> {
  return getJson<PageOf<CollectionResponse>>(`/api/collections?limit=${limit}`);
}

export type TracksOptions = { startNs?: number | null; endNs?: number | null; kinds?: string[] };

export function fetchTracks(
  machineId: string,
  bootId: string,
  options: TracksOptions = {},
): Promise<TracksResponse> {
  const params = new URLSearchParams();
  if (typeof options.startNs === "number") params.set("start_ns", String(options.startNs));
  if (typeof options.endNs === "number") params.set("end_ns", String(options.endNs));
  if (options.kinds && options.kinds.length > 0) params.set("kinds", options.kinds.join(","));
  const query = params.toString();
  return getJson<TracksResponse>(
    `${timelinePath(machineId, bootId)}/tracks${query ? `?${query}` : ""}`,
  );
}

export type CursorOptions = { atNs?: number | null; forward?: boolean };

export function fetchCursor(
  machineId: string,
  bootId: string,
  options: CursorOptions = {},
): Promise<CursorResponse> {
  const params = new URLSearchParams();
  if (typeof options.atNs === "number") params.set("at_ns", String(options.atNs));
  if (typeof options.forward === "boolean") params.set("forward", String(options.forward));
  const query = params.toString();
  return getJson<CursorResponse>(
    `${timelinePath(machineId, bootId)}/cursor${query ? `?${query}` : ""}`,
  );
}

export function fetchState(
  machineId: string,
  bootId: string,
  monotonicNs: number,
): Promise<TimelineStateResponse> {
  return getJson<TimelineStateResponse>(
    `${timelinePath(machineId, bootId)}/state?monotonic_ns=${monotonicNs}`,
  );
}
