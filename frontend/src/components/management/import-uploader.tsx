"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { StatusBadge } from "@/components/management/status-badge";
import { Button } from "@/components/ui/button";
import {
  describeError,
  fetchImportStatus,
  submitImport,
  type ImportStatusResponse,
  type ImportSubmitResponse,
} from "@/lib/api/client";
import { importFailureKindLabel, importStatusMeta, jobStatusMeta } from "@/lib/management/labels";
import { formatBytes } from "@/lib/timeline/format";

const POLL_INTERVAL_MS = 1500;

/** Journal 压缩包上传与导入过程。
 *
 * 全过程分三步，与后端的分工一致：上传（`POST /api/imports`，只落文件并入队）→ 任务状态
 * （`GET /api/imports/{job_id}` 轮询）→ 结果（导入写出的采集与失败分类）。前端不解析文件、
 * 不推断进度：任务状态与失败原因都来自后端字段。
 */
export function ImportUploader() {
  const [file, setFile] = useState<File | null>(null);
  const [submitted, setSubmitted] = useState<ImportSubmitResponse | null>(null);
  const [status, setStatus] = useState<ImportStatusResponse | null>(null);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!submitted) return;
    const jobId = submitted.job_id;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    async function poll() {
      try {
        const next = await fetchImportStatus(jobId);
        if (stopped) return;
        setStatus(next);
        if (next.job.status === "succeeded" || next.job.status === "failed") return;
      } catch (caught) {
        if (!stopped) setError(describeError(caught));
      }
      timer = setTimeout(() => {
        void poll();
      }, POLL_INTERVAL_MS);
    }

    void poll();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }, [submitted]);

  async function handleUpload() {
    if (!file) {
      setError("先选择一个文件");
      return;
    }
    setUploading(true);
    setError(null);
    setStatus(null);
    setSubmitted(null);
    try {
      setSubmitted(await submitImport(file));
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setUploading(false);
    }
  }

  const job = status?.job ?? null;
  const terminal = job?.status === "succeeded" || job?.status === "failed";

  return (
    <section className="flex flex-col gap-4" data-testid="import-uploader">
      <div className="flex flex-col gap-3 rounded-lg border border-slate-200 bg-white p-4">
        <h2 className="text-sm font-medium text-slate-900">上传 Journal 压缩包</h2>
        <p className="text-xs text-slate-500">
          支持 Journal 原生导出文件、gzip / xz / bzip2 压缩包与 tar 打包，也接受 .bootrec 容器。
          上传只把文件写到服务端并入队，真正的解析由 Worker 执行。
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <input
            type="file"
            name="file"
            data-testid="import-file"
            className="text-xs"
            onChange={(event) => setFile(event.target.files?.[0] ?? null)}
          />
          <Button type="button" size="sm" onClick={handleUpload} disabled={uploading || !file}>
            {uploading ? "上传中…" : "上传并导入"}
          </Button>
        </div>
        {!file && !submitted ? (
          <p className="text-xs text-slate-500" data-testid="import-empty">
            还没有选择文件。选好之后点「上传并导入」，这里会显示上传、排队、执行与结果。
          </p>
        ) : null}
      </div>

      {error ? (
        <p
          className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-xs text-red-800"
          role="alert"
        >
          {error}
        </p>
      ) : null}

      {submitted ? (
        <div className="flex flex-col gap-2 rounded-lg border border-slate-200 bg-white p-4 text-xs">
          <h3 className="text-sm font-medium text-slate-900">上传完成</h3>
          <dl className="grid gap-x-6 gap-y-1 sm:grid-cols-2">
            <Row label="文件名" value={submitted.filename ?? "（未提供）"} />
            <Row label="大小" value={formatBytes(submitted.size_bytes)} />
            <Row label="使用插件" value={submitted.plugin_id} mono />
            <Row label="上传 ID" value={submitted.upload_id} mono />
            <Row label="内容 sha256" value={`${submitted.source_sha256.slice(0, 16)}…`} mono />
          </dl>
        </div>
      ) : null}

      {job ? (
        <div className="flex flex-col gap-2 rounded-lg border border-slate-200 bg-white p-4 text-xs">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-sm font-medium text-slate-900">导入任务</h3>
            <StatusBadge
              label={jobStatusMeta(job.status).label}
              tone={jobStatusMeta(job.status).tone}
              testId="import-job-status"
            />
            <span className="text-slate-500">
              第 {job.attempts} / {job.max_attempts} 次尝试
            </span>
            <span className="font-mono text-slate-500">任务 {job.id}</span>
            {terminal ? null : <span className="text-slate-500">· 自动刷新中</span>}
          </div>

          {job.status === "failed" ? (
            <div
              className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-red-800"
              data-testid="import-failure"
            >
              {job.failure ? (
                <>
                  <StatusBadge
                    label={importFailureKindLabel(job.failure.kind)}
                    tone="failure"
                    testId="import-failure-kind"
                  />
                  <p className="mt-1 break-words">{job.failure.message}</p>
                </>
              ) : (
                <p>任务执行失败，后端没有写回失败原因；详细原因见 Worker 日志。</p>
              )}
            </div>
          ) : null}

          {job.status === "succeeded" ? (
            <div
              className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-emerald-900"
              data-testid="import-success"
            >
              {status?.collection ? (
                <>
                  <p>导入完成，已经可以从诊断记录里回放这次采集。</p>
                  <dl className="mt-1 grid gap-x-6 gap-y-1 sm:grid-cols-2">
                    <Row label="采集" value={status.collection.collection_id} mono />
                    <Row label="数据来源" value={status.collection.mode} mono />
                    <Row
                      label="导入状态"
                      value={importStatusMeta(status.collection.import_status).label}
                    />
                    <Row
                      label="写入记录"
                      value={
                        job.result?.records_written === null ||
                        job.result?.records_written === undefined
                          ? "未报告"
                          : `${String(job.result.records_written)} 条`
                      }
                    />
                    <Row
                      label="资源数据"
                      value={
                        status.collection.resources_available === null
                          ? "未说明"
                          : status.collection.resources_available
                            ? "可用"
                            : `不可用：${status.collection.resources_reason ?? "未给原因"}`
                      }
                    />
                  </dl>
                  <p className="mt-2 flex gap-3">
                    <Link className="underline" href="/collections">
                      查看诊断记录
                    </Link>
                    <Link
                      className="underline"
                      href={`/timeline?machine_id=${encodeURIComponent(
                        status.collection.machine_id,
                      )}&boot_id=${encodeURIComponent(status.collection.boot_id)}`}
                    >
                      打开时间线回放
                    </Link>
                  </p>
                </>
              ) : (
                <p>任务已成功，但还没有写出采集记录；稍后到诊断记录页查看。</p>
              )}
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

function Row({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex gap-2">
      <dt className="text-slate-500">{label}</dt>
      <dd className={mono ? "break-all font-mono text-slate-900" : "text-slate-900"}>{value}</dd>
    </div>
  );
}
