import Link from "next/link";
import type { ReactNode } from "react";

import { DiagnosticsView } from "@/components/diagnostics/diagnostics-view";
import {
  describeError,
  fetchCollections,
  fetchCursor,
  fetchDiagnosticServices,
  fetchMachine,
  fetchServiceDiagnostics,
  type CollectionResponse,
  type MachineResponse,
  type ServiceDiagnosticsResponse,
  type ServiceSummary,
} from "@/lib/api/client";
import type { WindowRange } from "@/lib/timeline/scale";

export const dynamic = "force-dynamic";

export const metadata = {
  title: "服务状态与异常诊断 · system-analyzer-service",
};

type PageProps = {
  searchParams: Promise<Record<string, string | string[] | undefined>>;
};

function firstValue(value: string | string[] | undefined): string | null {
  if (Array.isArray(value)) return value[0]?.trim() || null;
  return value?.trim() || null;
}

function diagnosticsHref(machineId: string, bootId: string, entity?: string | null): string {
  const params = new URLSearchParams({ machine_id: machineId, boot_id: bootId });
  if (entity) params.set("entity", entity);
  return `/diagnostics?${params.toString()}`;
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
          : "rounded-lg border border-slate-200 bg-white px-4 py-6 text-sm text-slate-600"
      }
    >
      <h2 className="font-medium">{title}</h2>
      <div className="mt-1 space-y-1">{children}</div>
    </section>
  );
}

/** 诊断页面是 Server Component：首屏的服务列表、时间轴和游标都在服务端取好，交互交给 Client Component。 */
export default async function DiagnosticsPage({ searchParams }: PageProps) {
  const params = await searchParams;
  const machineId = firstValue(params.machine_id);
  const bootId = firstValue(params.boot_id);
  const entity = firstValue(params.entity);
  const rawAtNs = firstValue(params.at_ns);
  const parsedAtNs = rawAtNs === null ? null : Number(rawAtNs);
  const atNs =
    parsedAtNs !== null && Number.isSafeInteger(parsedAtNs) && parsedAtNs >= 0 ? parsedAtNs : null;

  return (
    <main className="mx-auto flex max-w-6xl flex-col gap-6 px-6 py-10">
      {machineId && bootId ? (
        <DiagnosticsBoot machineId={machineId} bootId={bootId} entity={entity} atNs={atNs} />
      ) : (
        <BootPicker />
      )}
    </main>
  );
}

type DiagnosticsData =
  | {
      ok: true;
      machine: MachineResponse;
      services: ServiceSummary[];
      diagnostics: ServiceDiagnosticsResponse;
      axis: WindowRange | null;
    }
  | { ok: false; message: string; empty?: boolean; unknownEntity?: string };

async function loadDiagnostics(
  machineId: string,
  bootId: string,
  requestedEntity: string | null,
  atNs: number | null,
): Promise<DiagnosticsData> {
  try {
    const [machine, services, cursor] = await Promise.all([
      fetchMachine(machineId),
      fetchDiagnosticServices(machineId, bootId),
      fetchCursor(machineId, bootId),
    ]);
    if (services.services.length === 0) {
      return {
        ok: false,
        empty: true,
        message:
          "这次启动里没有可诊断的服务：分析任务可能还没有完成，或者采集里没有 Systemd Unit 记录。",
      };
    }
    if (requestedEntity && !services.services.some((item) => item.entity === requestedEntity)) {
      return {
        ok: false,
        unknownEntity: requestedEntity,
        message: `这次启动里没有服务 ${requestedEntity}。`,
      };
    }
    const target = requestedEntity ?? services.services[0].entity;
    const diagnostics = await fetchServiceDiagnostics(machineId, bootId, target, { atNs });
    const axis =
      cursor.start_ns !== null && cursor.end_ns !== null
        ? { startNs: cursor.start_ns, endNs: cursor.end_ns }
        : null;
    return {
      ok: true,
      machine,
      services: services.services,
      diagnostics,
      axis,
    };
  } catch (error) {
    return { ok: false, message: describeError(error) };
  }
}

async function DiagnosticsBoot({
  machineId,
  bootId,
  entity,
  atNs,
}: {
  machineId: string;
  bootId: string;
  entity: string | null;
  atNs: number | null;
}) {
  const data = await loadDiagnostics(machineId, bootId, entity, atNs);
  if (!data.ok) {
    return (
      <Panel title="无法读取这次启动的服务诊断" tone="error">
        <p>{data.message}</p>
        <p className="flex flex-wrap gap-3">
          <Link className="underline" href={diagnosticsHref(machineId, bootId)}>
            重新选择服务
          </Link>
          <Link
            className="underline"
            href={`/timeline?machine_id=${encodeURIComponent(machineId)}&boot_id=${encodeURIComponent(bootId)}`}
          >
            查看时间线
          </Link>
          <Link className="underline" href="/diagnostics">
            重新选择启动
          </Link>
        </p>
      </Panel>
    );
  }
  return (
    <DiagnosticsView
      machine={data.machine}
      bootId={bootId}
      services={data.services}
      initialDiagnostics={data.diagnostics}
      axis={data.axis}
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
        <h1 className="text-xl font-semibold text-slate-900">服务状态与异常诊断</h1>
        <p className="text-sm text-slate-600">
          选择一台机器的一次启动，再选择服务。诊断视图按游标重建服务状态，结论附带原始 Journal
          证据。
        </p>
      </header>
      {boots.length === 0 ? (
        <Panel title="还没有可诊断的启动" tone="info">
          <p>先上传一份 .bootrec 或 Journal 压缩包并等导入与分析完成，再回到这里选择启动。</p>
        </Panel>
      ) : (
        <ul className="flex flex-col gap-3" data-testid="diagnostics-boot-picker">
          {boots.map((boot) => (
            <li
              key={`${boot.machineId}/${boot.bootId}`}
              className="rounded-lg border border-slate-200 bg-white p-4"
            >
              <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <Link
                  className="text-sm font-medium text-slate-900 underline"
                  href={diagnosticsHref(boot.machineId, boot.bootId)}
                >
                  {boot.machineId}
                </Link>
                <span className="font-mono text-xs text-slate-500">启动 {boot.bootId}</span>
                <Link
                  className="text-xs text-slate-600 underline"
                  href={`/timeline?machine_id=${encodeURIComponent(boot.machineId)}&boot_id=${encodeURIComponent(boot.bootId)}`}
                >
                  时间线
                </Link>
              </div>
              <ul className="mt-2 space-y-1 text-xs text-slate-600">
                {boot.collections.map((collection) => (
                  <li key={collection.collection_id} className="font-mono">
                    {collection.collection_id} · {collection.mode} · {collection.import_status}
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
