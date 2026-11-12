"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { deleteTarget, describeError } from "@/lib/api/client";

/** 删除目标机器：两步确认，并写清删除范围。
 *
 * 后端只删 `ssh_targets` 一行，不级联：已经创建的采集任务与已经导入的诊断记录都保留。
 * 这一点必须在确认框里说清，否则人会以为历史数据一起没了。
 */
export function DeleteTargetButton({ targetId, host }: { targetId: string; host: string }) {
  const router = useRouter();
  const [confirming, setConfirming] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleDelete() {
    setPending(true);
    setError(null);
    try {
      await deleteTarget(targetId);
      router.push("/machines");
      router.refresh();
    } catch (caught) {
      setError(describeError(caught));
      setPending(false);
    }
  }

  if (!confirming) {
    return (
      <div className="flex flex-col items-start gap-2">
        <Button type="button" variant="outline" size="sm" onClick={() => setConfirming(true)}>
          删除这台机器
        </Button>
        {error ? (
          <p className="text-xs text-red-800" role="alert">
            {error}
          </p>
        ) : null}
      </div>
    );
  }

  return (
    <div
      className="flex flex-col gap-2 rounded-md border border-red-200 bg-red-50 p-3 text-xs text-red-900"
      data-testid="delete-confirm"
    >
      <p>
        确认删除 {host} 这条机器配置？只删除配置本身，已经创建的采集任务与已经导入的诊断记录都保留。
      </p>
      <div className="flex items-center gap-2">
        <Button type="button" size="sm" onClick={handleDelete} disabled={pending}>
          {pending ? "删除中…" : "确认删除"}
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => setConfirming(false)}
          disabled={pending}
        >
          取消
        </Button>
      </div>
      {error ? (
        <p className="text-red-800" role="alert">
          {error}
        </p>
      ) : null}
    </div>
  );
}
