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

export type ServiceListResponse = components["schemas"]["ServiceListResponse"];
export type ServiceSummary = components["schemas"]["ServiceSummaryResponse"];
export type ServiceDiagnosticsResponse = components["schemas"]["ServiceDiagnosticsResponse"];
export type DiagnosticFact = components["schemas"]["FactResponse"];
export type DiagnosticLifecycleSpan = components["schemas"]["LifecycleSpanResponse"];
export type DiagnosticJournalEntry = components["schemas"]["JournalEntryResponse"];
export type DiagnosticAnnotation = components["schemas"]["AnnotationResponse"];
export type DiagnosticResourceSeries = components["schemas"]["ResourceSeriesResponse"];

export type SshTarget = components["schemas"]["SshTarget"];
export type SshTargetDraft = components["schemas"]["SshTargetDraft"];
export type TestConnectionResponse = components["schemas"]["TestConnectionResponse"];
export type TestConnectionFailure = components["schemas"]["TestConnectionFailure"];
export type SystemInfoResponse = components["schemas"]["SystemInfoResponse"];
export type CollectionTaskResponse = components["schemas"]["CollectionTaskResponse"];
export type CollectionTaskFailure = components["schemas"]["CollectionTaskFailure"];
export type CollectionAttemptResponse = components["schemas"]["CollectionAttemptResponse"];
export type CollectionPhaseRecordResponse = components["schemas"]["CollectionPhaseRecordResponse"];
export type CollectionEventResponse = components["schemas"]["CollectionEventResponse"];
export type ImportStatusResponse = components["schemas"]["ImportStatusResponse"];
export type ImportSubmitResponse = components["schemas"]["ImportSubmitResponse"];
export type ImportJobStatus = components["schemas"]["ImportJobStatus"];
export type ImportJobFailure = components["schemas"]["ImportJobFailure"];
export type JobStatus = components["schemas"]["JobStatus"];

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

type RequestOptions = {
  method?: "GET" | "POST" | "PUT" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
};

/** 发一次 JSON 请求并把错误统一成 ``ApiError``。
 *
 * ``204`` 表示「成功但没有响应体」（例如删除），此时返回 ``undefined``：界面不需要为删除
 * 造一个空对象。
 */
async function requestJson<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { accept: "application/json" };
  const init: RequestInit = {
    method: options.method ?? "GET",
    cache: "no-store",
    headers,
    signal: options.signal,
  };
  if (options.body !== undefined) {
    headers["content-type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  const response = await fetch(`${apiRequestBaseUrl()}${path}`, init);
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
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function getJson<T>(path: string): Promise<T> {
  return requestJson<T>(path);
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

// ----------------------------------------------------------------------
// 机器管理（任务 4.1）
// ----------------------------------------------------------------------

export function fetchTargets(): Promise<SshTarget[]> {
  return getJson<SshTarget[]>("/api/targets");
}

export function fetchTarget(targetId: string): Promise<SshTarget> {
  return getJson<SshTarget>(`/api/targets/${encodeURIComponent(targetId)}`);
}

export function createTarget(draft: SshTargetDraft): Promise<SshTarget> {
  return requestJson<SshTarget>("/api/targets", { method: "POST", body: draft });
}

/** 整体替换：四个字段必须全部提交，与后端的 ``PUT`` 语义一致。 */
export function replaceTarget(targetId: string, draft: SshTargetDraft): Promise<SshTarget> {
  return requestJson<SshTarget>(`/api/targets/${encodeURIComponent(targetId)}`, {
    method: "PUT",
    body: draft,
  });
}

export function deleteTarget(targetId: string): Promise<void> {
  return requestJson<void>(`/api/targets/${encodeURIComponent(targetId)}`, { method: "DELETE" });
}

/** 只读连接测试。「连不上」是正常结果（200 加 ``ok: false``），所以不用错误分支处理。 */
export function testTargetConnection(targetId: string): Promise<TestConnectionResponse> {
  return requestJson<TestConnectionResponse>(
    `/api/targets/${encodeURIComponent(targetId)}/test-connection`,
    { method: "POST" },
  );
}

// ----------------------------------------------------------------------
// 采集任务（任务 4.1）
// ----------------------------------------------------------------------

export type CollectionTaskQuery = {
  targetId?: string | null;
  status?: JobStatus | null;
  limit?: number;
  offset?: number;
};

export function fetchCollectionTasks(
  query: CollectionTaskQuery = {},
  signal?: AbortSignal,
): Promise<PageOf<CollectionTaskResponse>> {
  const params = new URLSearchParams();
  if (query.targetId) params.set("target_id", query.targetId);
  if (query.status) params.set("status", query.status);
  if (typeof query.limit === "number") params.set("limit", String(query.limit));
  if (typeof query.offset === "number") params.set("offset", String(query.offset));
  const suffix = params.toString();
  return requestJson<PageOf<CollectionTaskResponse>>(
    `/api/collection-tasks${suffix ? `?${suffix}` : ""}`,
    { signal },
  );
}

export function fetchCollectionTask(taskId: string): Promise<CollectionTaskResponse> {
  return getJson<CollectionTaskResponse>(`/api/collection-tasks/${encodeURIComponent(taskId)}`);
}

export function createCollectionTask(targetId: string): Promise<CollectionTaskResponse> {
  return requestJson<CollectionTaskResponse>("/api/collection-tasks", {
    method: "POST",
    body: { target_id: targetId },
  });
}

export function cancelCollectionTask(taskId: string): Promise<CollectionTaskResponse> {
  return requestJson<CollectionTaskResponse>(
    `/api/collection-tasks/${encodeURIComponent(taskId)}/cancel`,
    { method: "POST" },
  );
}

// ----------------------------------------------------------------------
// 导入（任务 1.6 的上传接口，界面在任务 4.1 接入）
// ----------------------------------------------------------------------

/** 上传文件并提交导入任务。文件名与内容都由浏览器给出，前端不解析文件。 */
export async function submitImport(file: File): Promise<ImportSubmitResponse> {
  const form = new FormData();
  form.append("file", file);
  const response = await fetch(`${apiRequestBaseUrl()}/api/imports`, {
    method: "POST",
    body: form,
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
      envelope.error?.message ?? `上传失败（HTTP ${response.status}）`,
      envelope.error?.details ?? [],
    );
  }
  return (await response.json()) as ImportSubmitResponse;
}

export function fetchImportStatus(jobId: string): Promise<ImportStatusResponse> {
  return getJson<ImportStatusResponse>(`/api/imports/${encodeURIComponent(jobId)}`);
}

// ----------------------------------------------------------------------
// 时间线（任务 4.2）
// ----------------------------------------------------------------------

export type TracksOptions = {
  startNs?: number | null;
  endNs?: number | null;
  kinds?: string[];
  /** 是否返回事件与区间的 attributes；界面不用它们，默认关闭以缩小响应。 */
  includeAttributes?: boolean;
};

export function fetchTracks(
  machineId: string,
  bootId: string,
  options: TracksOptions = {},
): Promise<TracksResponse> {
  const params = new URLSearchParams();
  if (typeof options.startNs === "number") params.set("start_ns", String(options.startNs));
  if (typeof options.endNs === "number") params.set("end_ns", String(options.endNs));
  if (options.kinds && options.kinds.length > 0) params.set("kinds", options.kinds.join(","));
  if (options.includeAttributes !== undefined) {
    params.set("include_attributes", String(options.includeAttributes));
  }
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

// ----------------------------------------------------------------------
// 服务诊断（任务 4.3）
// ----------------------------------------------------------------------

function diagnosticsPath(machineId: string, bootId: string): string {
  return `/api/machines/${encodeURIComponent(machineId)}/boots/${encodeURIComponent(
    bootId,
  )}/diagnostics`;
}

/** 这次启动里可以诊断的服务：来自分析写下的 service 实体与记录上的服务实体。 */
export function fetchDiagnosticServices(
  machineId: string,
  bootId: string,
  signal?: AbortSignal,
): Promise<ServiceListResponse> {
  return requestJson<ServiceListResponse>(`${diagnosticsPath(machineId, bootId)}/services`, {
    signal,
  });
}

export type ServiceDiagnosticsOptions = {
  atNs?: number | null;
  journalWindowNs?: number | null;
  signal?: AbortSignal;
};

/** 指定服务在游标时刻的状态、身份信息、关联 Journal、资源与分析结论。 */
export function fetchServiceDiagnostics(
  machineId: string,
  bootId: string,
  entityId: string,
  options: ServiceDiagnosticsOptions = {},
): Promise<ServiceDiagnosticsResponse> {
  const params = new URLSearchParams();
  if (typeof options.atNs === "number") params.set("at_ns", String(options.atNs));
  if (typeof options.journalWindowNs === "number") {
    params.set("journal_window_ns", String(options.journalWindowNs));
  }
  const query = params.toString();
  return requestJson<ServiceDiagnosticsResponse>(
    `${diagnosticsPath(machineId, bootId)}/services/${encodeURIComponent(entityId)}${
      query ? `?${query}` : ""
    }`,
    { signal: options.signal },
  );
}
