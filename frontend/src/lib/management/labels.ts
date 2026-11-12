/** 机器管理与采集任务界面的文案与着色规则。
 *
 * 只做「后端字段 → 给人看的文字」这一层转换：状态、阶段、失败分类都是后端定义的取值，
 * 界面不新增含义、不推断新的状态。纯函数，便于逐条断言。
 */

import type { CollectionTaskResponse } from "@/lib/api/client";

export type Tone = "neutral" | "running" | "success" | "failure";

type Labelled = { label: string; tone: Tone };

const JOB_STATUS: Record<string, Labelled> = {
  queued: { label: "排队中", tone: "neutral" },
  running: { label: "执行中", tone: "running" },
  succeeded: { label: "已完成", tone: "success" },
  failed: { label: "失败", tone: "failure" },
};

const ATTEMPT_STATUS: Record<string, Labelled> = {
  pending: { label: "未开始", tone: "neutral" },
  running: { label: "执行中", tone: "running" },
  succeeded: { label: "成功", tone: "success" },
  failed: { label: "失败", tone: "failure" },
  cancelled: { label: "已取消", tone: "neutral" },
  timed_out: { label: "超时", tone: "failure" },
  interrupted: { label: "中断", tone: "failure" },
};

const PHASE_STATUS: Record<string, Labelled> = {
  ...ATTEMPT_STATUS,
  reused: { label: "复用上次结果", tone: "neutral" },
};

const PHASE_NAME: Record<string, string> = {
  pre: "采集前准备",
  in_progress: "采集中",
  post: "采集后恢复",
};

const IMPORT_STATUS: Record<string, Labelled> = {
  pending: { label: "待导入", tone: "neutral" },
  importing: { label: "导入中", tone: "running" },
  imported: { label: "已导入", tone: "success" },
  failed: { label: "导入失败", tone: "failure" },
};

/** 四个失败分类的后缀说明，帮人决定接下来做什么；具体原因以后端给的 `message` 为准。 */
const FAILURE_KIND: Record<string, string> = {
  connect: "连接失败",
  auth: "认证失败",
  timeout: "超时",
  credential: "服务端未配置口令",
};

/** 未知取值原样显示：后端加了新状态时，界面显示原始英文比显示空白更容易排查。 */
function lookup(table: Record<string, Labelled>, value: string): Labelled {
  return table[value] ?? { label: value, tone: "neutral" };
}

export function jobStatusMeta(status: string): Labelled {
  return lookup(JOB_STATUS, status);
}

export function jobStatusLabel(status: string): string {
  return jobStatusMeta(status).label;
}

/** 任务是否还在动：这两状态下界面才需要轮询刷新。 */
export function isActiveJobStatus(status: string): boolean {
  return status === "queued" || status === "running";
}

export function attemptStatusMeta(status: string): Labelled {
  return lookup(ATTEMPT_STATUS, status);
}

export function attemptStatusLabel(status: string): string {
  return attemptStatusMeta(status).label;
}

export function phaseStatusMeta(status: string): Labelled {
  return lookup(PHASE_STATUS, status);
}

export function phaseStatusLabel(status: string): string {
  return phaseStatusMeta(status).label;
}

export function phaseLabel(phase: string): string {
  return PHASE_NAME[phase] ?? phase;
}

export function importStatusMeta(status: string): Labelled {
  return lookup(IMPORT_STATUS, status);
}

export function importStatusLabel(status: string): string {
  return importStatusMeta(status).label;
}

export function failureKindLabel(kind: string): string {
  return FAILURE_KIND[kind] ?? kind;
}

/** 导入任务失败的两类原因（`import.source` 的 `payload.result.failure.kind`）。 */
const IMPORT_FAILURE_KIND: Record<string, string> = {
  invalid_input: "输入不合格",
  internal: "服务端内部错误",
};

export function importFailureKindLabel(kind: string): string {
  return IMPORT_FAILURE_KIND[kind] ?? kind;
}

/** 采集失败的一句话原因；没有失败时为 null。
 *
 * 用后端给的 `failure`（来自编排写下的阶段说明），前面补上失败的阶段名，便于定位。
 */
export function collectionTaskFailure(task: CollectionTaskResponse): string | null {
  if (!task.failure) return null;
  const phase = task.failure.phase ? `${phaseLabel(task.failure.phase)}：` : "";
  return `${phase}${task.failure.message}`;
}

/** 不是失败、但值得说一句的结果：取消与中断。
 *
 * 与失败分开：取消是用户要求的结果，不能画成红色；中断只是本次尝试没跑完，任务还在等重试。
 */
export function collectionTaskNotice(task: CollectionTaskResponse): string | null {
  if (task.failure) return null;
  if (task.latest_attempt?.status === "cancelled") {
    return "已按用户要求取消，采集后恢复阶段已执行";
  }
  if (task.latest_attempt?.status === "interrupted") {
    return "上一次执行因 Worker 退出而中断，等待重新执行";
  }
  return null;
}

/** ISO 时间按 UTC 写成 `YYYY-MM-DD HH:MM:SS UTC`。
 *
 * 与时间线界面同一口径：时间来自目标机器或 Worker，混进浏览器本地时区会让「机器上几点」
 * 和「我这里几点」分不清。空值与非法值不猜，直接返回占位符或原文。
 */
export function formatTimestamp(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const text = date.toISOString();
  return `${text.slice(0, 10)} ${text.slice(11, 19)} UTC`;
}
