import type { Metadata } from "next";
import Link from "next/link";
import type { ReactNode } from "react";

import "./globals.css";

export const metadata: Metadata = {
  title: "system-analyzer-service",
  description: "Linux 远程诊断与启动过程分析平台",
};

const NAV = [
  { href: "/machines", label: "机器" },
  { href: "/tasks", label: "采集任务" },
  { href: "/imports", label: "导入" },
  { href: "/collections", label: "诊断记录" },
  { href: "/timeline", label: "时间线" },
];

/** 全站导航。日常操作都在浏览器里完成：机器、采集任务、导入、诊断记录、时间线。 */
export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN">
      <body className="min-h-screen">
        <header className="border-b border-slate-200 bg-white">
          <nav className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-5 gap-y-2 px-6 py-3 text-sm">
            <Link className="font-medium text-slate-900" href="/">
              system-analyzer-service
            </Link>
            {NAV.map((item) => (
              <Link
                key={item.href}
                className="text-slate-600 hover:text-slate-900"
                href={item.href}
              >
                {item.label}
              </Link>
            ))}
          </nav>
        </header>
        {children}
      </body>
    </html>
  );
}
