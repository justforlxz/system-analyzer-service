import Link from "next/link";
import type { ReactNode } from "react";

import { TimelineReplay } from "@/components/timeline/timeline-replay";
import {
  describeError,
  fetchCollections,
  fetchCursor,
  fetchMachine,
  fetchTracks,
  type CollectionResponse,
} from "@/lib/api/client";

export const dynamic = "force-dynamic";

export const metadata = {
  title: "启动过程时间线回放 · system-analyzer-service",
};

type PageProps = {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

function firstValue(value: string | string[] | undefined): string | null {
  if (Array.isArray(value)) return value[0]?.trim() || null;
  return value?.trim() || null;
}

function timelineHref(machineId: string, bootId: string): string {
  return `/timeline?machine_id=${encodeURIComponent(machineId)}&boot_id=${encodeURIComponent(bootId)}`;
}

function Panel({
  title,
  children,
  tone,
}: {
  title: string;
  children: ReactNode;
  tone: "error" | "info";
}) {
  return (
    <section
      className={
        tone === "error"
          ? "rounded-lg border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800"
          : "rounded-lg border border-slate-200 bg-white px-4 py-3 text-sm text-slate-700"
      }
    >
      <h2 className="font-medium">{title}</h2>
      <div className="mt-1 space-y-1">{children}</div>
    </section>
  );
}

/** 时间线页面是 Server Component：目标选择与首屏数据都在服务端取，交互交给 Client Component。 */
export default async function TimelinePage({ searchParams }: PageProps) {
  const params = await searchParams;
  const machineId = firstValue(params.machine_id);
  const bootId = firstValue(params.boot_id);

  return (
    <main className="mx-auto flex max-w-6xl flex-col gap-6 px-6 py-10">
      {machineId && bootId ? (
        <TimelineBoot machineId={machineId} bootId={bootId} />
      ) : (
        <BootPicker />
      )}
    </main>
  );
}

type BootData =
  | {
      ok: true;
      machine: Awaited<ReturnType<typeof fetchMachine>>;
      tracks: Awaited<ReturnType<typeof fetchTracks>>;
      cursor: Awaited<ReturnType<typeof fetchCursor>>;
    }
  | { ok: false; message: string };

async function loadBoot(machineId: string, bootId: string): Promise<BootData> {
  try {
    const [machine, tracks, cursor] = await Promise.all([
      fetchMachine(machineId),
      fetchTracks(machineId, bootId),
      fetchCursor(machineId, bootId),
    ]);
    return { ok: true, machine, tracks, cursor };
  } catch (error) {
    return { ok: false, message: describeError(error) };
  }
}

async function TimelineBoot({ machineId, bootId }: { machineId: string; bootId: string }) {
  const data = await loadBoot(machineId, bootId);
  if (!data.ok) {
    return (
      <Panel title="无法读取这次启动的时间线" tone="error">
        <p>{data.message}</p>
        <p>
          <Link className="underline" href="/timeline">
            重新选择启动
          </Link>
        </p>
      </Panel>
    );
  }
  return (
    <TimelineReplay
      machine={data.machine}
      bootId={bootId}
      initialTracks={data.tracks}
      initialCursor={data.cursor}
    />
  );
}

type BootEntry = { machineId: string; bootId: string; collections: CollectionResponse[] };

function groupByBoot(collections: CollectionResponse[]): BootEntry[] {
  const entries = new Map<string, BootEntry>();
  for (const collection of collections) {
    const key = `${collection.machine_id}\u0000${collection.boot_id}`;
    const existing = entries.get(key);
    if (existing) {
      existing.collections.push(collection);
    } else {
      entries.set(key, {
        machineId: collection.machine_id,
        bootId: collection.boot_id,
        collections: [collection],
      });
    }
  }
  return [...entries.values()];
}

async function BootPicker() {
  let collections: CollectionResponse[];
  try {
    collections = (await fetchCollections(50)).items;
  } catch (error) {
    return (
      <Panel title="无法读取启动列表" tone="error">
        <p>{describeError(error)}</p>
      </Panel>
    );
  }

  const boots = groupByBoot(collections);
  return (
    <>
      <header className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold text-slate-900">启动过程时间线回放</h1>
        <p className="text-sm text-slate-600">
          选择一台机器的一次启动。数据只展示已经导入完成的采集，缺失的资源数据会如实标注。
        </p>
      </header>
      {boots.length === 0 ? (
        <Panel title="还没有可回放的启动" tone="info">
          <p>先上传一份 .bootrec 或 Journal 压缩包并等导入完成，再回到这里选择启动。</p>
        </Panel>
      ) : (
        <ul className="flex flex-col gap-3" data-testid="boot-picker">
          {boots.map((boot) => (
            <li
              key={`${boot.machineId}/${boot.bootId}`}
              className="rounded-lg border border-slate-200 bg-white p-4"
            >
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <Link
                  className="text-sm font-medium text-slate-900 underline"
                  href={timelineHref(boot.machineId, boot.bootId)}
                >
                  {boot.machineId}
                </Link>
                <span className="font-mono text-xs text-slate-500">启动 {boot.bootId}</span>
              </div>
              <ul className="mt-2 space-y-1 text-xs text-slate-600">
                {boot.collections.map((collection) => (
                  <li key={collection.collection_id} className="font-mono">
                    {collection.collection_id} · {collection.mode} · {collection.import_status}
                    {collection.resources_available === false
                      ? ` · 资源不可用：${collection.resources_reason ?? "未给原因"}`
                      : ""}
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}
