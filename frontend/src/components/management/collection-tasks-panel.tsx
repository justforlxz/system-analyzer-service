"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { StatusBadge } from "@/components/management/status-badge";
import { Button } from "@/components/ui/button";
import {
  cancelCollectionTask,
  createCollectionTask,
  describeError,
  fetchCollectionTasks,
  type CollectionTaskResponse,
  type PageOf,
  type SshTarget,
} from "@/lib/api/client";
import {
  attemptStatusMeta,
  collectionTaskFailure,
  collectionTaskNotice,
  formatTimestamp,
  isActiveJobStatus,
  jobStatusMeta,
  phaseLabel,
  phaseStatusMeta,
} from "@/lib/management/labels";

type Props = {
  targets: SshTarget[];
  initial: PageOf<CollectionTaskResponse>;
  /** 只看这台机器的任务（机器详情页用）；空表示看全部。 */
  targetId?: string | null;
  showCreate?: boolean;
};

const POLL_INTERVAL_MS = 2000;

/** 采集任务面板：创建、看状态变化、取消。
 *
 * 状态只从后端读：轮询 `GET /api/collection-tasks`，只要还有排队中或执行中的任务就继续刷新，
 * 全部结束就停下——不给服务器发无意义的请求。取消只是置一个标记，真正的停止发生在 Worker
 * 两个步骤之间，所以取消后仍以采集记录显示的结果为准。
 */
export function CollectionTasksPanel({ targets, initial, targetId, showCreate = true }: Props) {
  const [tasks, setTasks] = useState<CollectionTaskResponse[]>(initial.items);
  const [total, setTotal] = useState(initial.total);
  const [selected, setSelected] = useState(targetId ?? targets[0]?.target_id ?? "");
  const [creating, setCreating] = useState(false);
  const [busyTaskId, setBusyTaskId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const active = tasks.some((task) => isActiveJobStatus(task.status));

  const refresh = useCallback(async () => {
    try {
      const page = await fetchCollectionTasks({ targetId: targetId ?? undefined, limit: 20 });
      setTasks(page.items);
      setTotal(page.total);
    } catch (caught) {
      setError(describeError(caught));
    }
  }, [targetId]);

  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => {
      void refresh();
    }, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [active, refresh]);

  async function handleCreate() {
    if (!selected) {
      setError("先选择一台目标机器");
      return;
    }
    setCreating(true);
    setError(null);
    setNotice(null);
    try {
      const created = await createCollectionTask(selected);
      setNotice(
        `已创建采集任务 ${created.task_id}，当前状态：${jobStatusMeta(created.status).label}`,
      );
      await refresh();
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setCreating(false);
    }
  }

  async function handleCancel(taskId: string) {
    setBusyTaskId(taskId);
    setError(null);
    setNotice(null);
    try {
      const updated = await cancelCollectionTask(taskId);
      setTasks((current) =>
        current.map((task) => (task.task_id === updated.task_id ? updated : task)),
      );
      setNotice("已请求取消：Worker 在两个步骤之间读取取消标记，最终结果以采集记录为准");
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusyTaskId(null);
    }
  }

  return (
    <section className="flex flex-col gap-4" data-testid="collection-tasks-panel">
      {showCreate ? (
        <div className="flex flex-col gap-2 rounded-lg border border-slate-200 bg-white p-4">
          <h2 className="text-sm font-medium text-slate-900">创建采集任务</h2>
          {targets.length === 0 ? (
            <p className="text-xs text-slate-600">
              还没有可采集的机器。
              <Link className="ml-1 text-slate-900 underline" href="/machines">
                先新增一台机器
              </Link>
              。
            </p>
          ) : (
            <div className="flex flex-wrap items-center gap-2">
              <label className="text-xs text-slate-700">
                目标机器
                <select
                  className="ml-2 rounded-md border border-slate-300 bg-white px-2 py-1 text-sm"
                  name="target_id"
                  value={selected}
                  onChange={(event) => setSelected(event.target.value)}
                >
                  {targets.map((target) => (
                    <option key={target.target_id} value={target.target_id}>
                      {target.host}（{target.username}@{String(target.port)}）
                    </option>
                  ))}
                </select>
              </label>
              <Button type="button" size="sm" onClick={handleCreate} disabled={creating}>
                {creating ? "创建中…" : "创建采集任务"}
              </Button>
              <span className="text-xs text-slate-500">
                任务交给 Worker 执行；本页会在任务进行中自动刷新状态。
              </span>
            </div>
          )}
        </div>
      ) : null}

      {error ? (
        <p
          className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800"
          role="alert"
        >
          {error}
        </p>
      ) : null}
      {notice ? (
        <p className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-xs text-emerald-800">
          {notice}
        </p>
      ) : null}

      <div className="flex items-baseline justify-between">
        <h2 className="text-sm font-medium text-slate-900">采集任务</h2>
        <span className="text-xs text-slate-500">
          共 {total} 条{active ? " · 自动刷新中" : ""}
        </span>
      </div>

      {tasks.length === 0 ? (
        <p
          className="rounded-lg border border-slate-200 bg-white px-4 py-6 text-sm text-slate-600"
          data-testid="collection-tasks-empty"
        >
          还没有采集任务。选一台机器点「创建采集任务」，任务会交给 Worker 执行，这里会显示 「排队中
          → 执行中 → 成功 / 失败 / 已取消」的状态变化。
        </p>
      ) : (
        <ul className="flex flex-col gap-3">
          {tasks.map((task) => (
            <TaskCard
              key={task.task_id}
              task={task}
              busy={busyTaskId === task.task_id}
              onCancel={() => void handleCancel(task.task_id)}
            />
          ))}
        </ul>
      )}
    </section>
  );
}

function TaskCard({
  task,
  busy,
  onCancel,
}: {
  task: CollectionTaskResponse;
  busy: boolean;
  onCancel: () => void;
}) {
  const status = jobStatusMeta(task.status);
  const attempt = task.latest_attempt;
  const failure = collectionTaskFailure(task);
  const notice = collectionTaskNotice(task);
  const canCancel = isActiveJobStatus(task.status) && !task.cancel_requested;

  return (
    <li
      className="flex flex-col gap-2 rounded-lg border border-slate-200 bg-white p-4"
      data-testid="collection-task"
      data-task-status={task.status}
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <StatusBadge label={status.label} tone={status.tone} testId="task-status" />
        {task.target_id ? (
          <Link
            className="text-sm font-medium text-slate-900 underline"
            href={`/machines/${encodeURIComponent(task.target_id)}`}
          >
            {task.target_id}
          </Link>
        ) : (
          <span className="text-sm text-slate-600">目标机器已删除</span>
        )}
        <span className="font-mono text-xs text-slate-500">任务 {task.task_id}</span>
        {task.cancel_requested ? (
          <StatusBadge label="已请求取消" tone="neutral" testId="task-cancel-requested" />
        ) : null}
        <span className="ml-auto flex items-center gap-2">
          {canCancel ? (
            <Button type="button" variant="outline" size="sm" onClick={onCancel} disabled={busy}>
              {busy ? "取消中…" : "取消任务"}
            </Button>
          ) : null}
        </span>
      </div>

      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-500">
        <span>创建 {formatTimestamp(task.created_at)}</span>
        <span>更新 {formatTimestamp(task.updated_at)}</span>
        <span>
          尝试 {task.attempts} / {task.max_attempts} 次
        </span>
        {task.run_id ? <span className="font-mono">run {task.run_id}</span> : null}
      </div>

      {attempt ? (
        <div className="flex flex-wrap items-center gap-2 text-xs" data-testid="task-attempt">
          <span className="text-slate-500">最近一次尝试</span>
          <StatusBadge
            label={attemptStatusMeta(attempt.status).label}
            tone={attemptStatusMeta(attempt.status).tone}
          />
          {attempt.phases.map((phase) => (
            <span
              key={phase.phase}
              className="inline-flex items-center gap-1 rounded border border-slate-200 px-2 py-0.5"
            >
              <span className="text-slate-600">{phaseLabel(phase.phase)}</span>
              <StatusBadge
                label={phaseStatusMeta(phase.status).label}
                tone={phaseStatusMeta(phase.status).tone}
              />
            </span>
          ))}
        </div>
      ) : (
        <p className="text-xs text-slate-500">Worker 还没有领取这条任务，尚未产生采集记录。</p>
      )}

      {failure ? (
        <p
          className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800"
          data-testid="task-failure"
        >
          {failure}
        </p>
      ) : null}

      {notice ? (
        <p
          className="rounded-md border border-slate-300 bg-slate-50 px-3 py-2 text-xs text-slate-700"
          data-testid="task-notice"
        >
          {notice}
        </p>
      ) : null}

      {attempt && attempt.events.length > 0 ? (
        <details className="text-xs text-slate-600">
          <summary className="cursor-pointer text-slate-700">
            操作记录（{attempt.events.length} 条）
          </summary>
          <ol className="mt-2 space-y-1" data-testid="task-events">
            {attempt.events.map((event, index) => (
              <li key={`${event.at}-${String(index)}`} className="flex flex-wrap gap-x-2">
                <span className="font-mono text-slate-500">{formatTimestamp(event.at)}</span>
                <span className="text-slate-700">
                  {phaseLabel(event.phase)} · {event.kind}
                </span>
                {event.step ? <span className="text-slate-600">步骤 {event.step}</span> : null}
                {event.detail ? <span className="text-slate-600">{event.detail}</span> : null}
              </li>
            ))}
          </ol>
        </details>
      ) : null}
    </li>
  );
}
