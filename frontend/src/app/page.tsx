import Link from "next/link";

import { apiBaseUrl } from "@/lib/api/config";

const ENTRIES = [
  {
    href: "/machines",
    title: "机器",
    body: "新增、编辑、删除 SSH 目标机器，对机器做只读连接测试，看它的采集任务。",
  },
  {
    href: "/tasks",
    title: "采集任务",
    body: "创建采集任务，观察「排队中 → 执行中 → 成功 / 失败 / 已取消」的状态变化，必要时取消。",
  },
  {
    href: "/imports",
    title: "导入",
    body: "上传 Journal 压缩包或 .bootrec 容器，看上传、排队、执行与结果，失败时显示可读原因。",
  },
  {
    href: "/collections",
    title: "诊断记录",
    body: "已导入的采集列表与导入状态，可以跳到对应启动的时间线回放。",
  },
  {
    href: "/timeline",
    title: "时间线",
    body: "在同一条时间轴上回放服务生命周期、资源指标与日志。",
  },
];

/** 首页只做入口：五个管理页面各管一件事，日常操作不依赖命令行。 */
export default function HomePage() {
  return (
    <main className="mx-auto flex max-w-4xl flex-col gap-6 px-6 py-12">
      <header className="flex flex-col gap-2">
        <h1 className="text-2xl font-semibold text-slate-900">system-analyzer-service</h1>
        <p className="text-sm text-slate-600">
          Linux 远程诊断与启动过程分析平台。通过 SSH
          临时采集目标机器，数据统一存进中心服务器后在同一时间轴上回放。
        </p>
        <p className="text-xs text-slate-500">
          后端 API 地址：<span className="font-mono">{apiBaseUrl()}</span>
        </p>
      </header>
      <ul className="grid gap-3 sm:grid-cols-2">
        {ENTRIES.map((entry) => (
          <li key={entry.href} className="rounded-lg border border-slate-200 bg-white p-4">
            <Link className="text-sm font-medium text-slate-900 underline" href={entry.href}>
              {entry.title}
            </Link>
            <p className="mt-1 text-xs text-slate-600">{entry.body}</p>
          </li>
        ))}
      </ul>
    </main>
  );
}
