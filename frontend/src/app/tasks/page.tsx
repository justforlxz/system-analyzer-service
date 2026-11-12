import { CollectionTasksPanel } from "@/components/management/collection-tasks-panel";
import { Panel } from "@/components/management/panel";
import {
  describeError,
  fetchCollectionTasks,
  fetchTargets,
  type CollectionTaskResponse,
  type PageOf,
  type SshTarget,
} from "@/lib/api/client";

export const dynamic = "force-dynamic";

export const metadata = { title: "采集任务 · system-analyzer-service" };

type PageProps = {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

/** 采集任务页：创建、看状态变化、取消。
 *
 * 首屏数据在服务端取（机器列表 + 任务第一页），状态轮询与取消在 Client Component 里做。
 */
export default async function TasksPage({ searchParams }: PageProps) {
  const params = await searchParams;
  const targetId = firstValue(params.target_id);

  let targets: SshTarget[];
  let page: PageOf<CollectionTaskResponse>;
  try {
    [targets, page] = await Promise.all([
      fetchTargets(),
      fetchCollectionTasks({ targetId: targetId ?? undefined, limit: 20 }),
    ]);
  } catch (error) {
    return (
      <main className="mx-auto flex max-w-4xl flex-col gap-6 px-6 py-10">
        <Panel title="无法读取采集任务" tone="error" testId="tasks-error">
          <p>{describeError(error)}</p>
        </Panel>
      </main>
    );
  }

  return (
    <main className="mx-auto flex max-w-4xl flex-col gap-6 px-6 py-10">
      <header className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold text-slate-900">采集任务</h1>
        <p className="text-sm text-slate-600">
          任务交给 Worker 执行，页面在任务进行中自动刷新状态。取消只是请求，最终结果以采集记录为准。
          {targetId ? `当前只看目标机器 ${targetId}。` : ""}
        </p>
      </header>
      <CollectionTasksPanel targets={targets} initial={page} targetId={targetId} />
    </main>
  );
}

function firstValue(value: string | string[] | undefined): string | null {
  if (Array.isArray(value)) return value[0]?.trim() || null;
  return value?.trim() || null;
}
