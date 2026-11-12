"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { createTarget, describeError, replaceTarget, type SshTarget } from "@/lib/api/client";

type Props = {
  mode: "create" | "edit";
  /** 编辑模式下要替换的那台机器；新增模式不需要。 */
  target?: SshTarget;
};

const FIELD_CLASS =
  "mt-1 block w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900";

/** 目标机器表单。新增与编辑共用一组字段：编辑走 `PUT` 整体替换，所以四个字段都要提交。
 *
 * 表单里没有口令输入框：库里只存口令的变量名，真正的口令由 API 进程的运行环境提供
 * （`remote/credentials.py`）。页面上把这一点写出来，免得人以为忘记填了。
 */
export function TargetForm({ mode, target }: Props) {
  const router = useRouter();
  const [host, setHost] = useState(target?.host ?? "");
  const [username, setUsername] = useState(target?.username ?? "");
  const [port, setPort] = useState(String(target?.port ?? 22));
  const [passwordEnvVar, setPasswordEnvVar] = useState(
    target?.password_env_var ?? "SAS_SSH_PASSWORD",
  );
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const parsedPort = Number(port);
    if (!Number.isInteger(parsedPort) || parsedPort < 1 || parsedPort > 65535) {
      setError("端口必须是 1 到 65535 之间的整数");
      return;
    }
    setPending(true);
    setError(null);
    setNotice(null);
    const draft = {
      host: host.trim(),
      username: username.trim(),
      port: parsedPort,
      password_env_var: passwordEnvVar.trim(),
    };
    try {
      if (mode === "create") {
        const created = await createTarget(draft);
        router.push(`/machines/${encodeURIComponent(created.target_id)}`);
        return;
      }
      if (!target) throw new Error("编辑模式缺少目标机器");
      await replaceTarget(target.target_id, draft);
      setNotice("已保存，四个字段已整体替换");
      router.refresh();
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setPending(false);
    }
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="flex flex-col gap-3 rounded-lg border border-slate-200 bg-white p-4"
      data-testid={`target-form-${mode}`}
    >
      <h2 className="text-sm font-medium text-slate-900">
        {mode === "create" ? "新增机器" : "编辑机器"}
      </h2>
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="text-xs font-medium text-slate-700">
          主机名或 IP 地址
          <input
            className={FIELD_CLASS}
            name="host"
            value={host}
            onChange={(event) => setHost(event.target.value)}
            placeholder="10.20.50.152"
            required
          />
        </label>
        <label className="text-xs font-medium text-slate-700">
          SSH 用户名
          <input
            className={FIELD_CLASS}
            name="username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            placeholder="uos"
            required
          />
        </label>
        <label className="text-xs font-medium text-slate-700">
          SSH 端口
          <input
            className={FIELD_CLASS}
            name="port"
            inputMode="numeric"
            value={port}
            onChange={(event) => setPort(event.target.value)}
            required
          />
        </label>
        <label className="text-xs font-medium text-slate-700">
          口令变量名
          <input
            className={FIELD_CLASS}
            name="password_env_var"
            value={passwordEnvVar}
            onChange={(event) => setPasswordEnvVar(event.target.value)}
            placeholder="SAS_SSH_PASSWORD"
            required
          />
        </label>
      </div>
      <p className="text-xs text-slate-500">
        口令不在界面上填写：库里只保存变量名，口令由服务端进程的运行环境提供。改动口令请改服务端配置。
      </p>
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
      <div className="flex items-center gap-3">
        <Button type="submit" disabled={pending} size="sm">
          {mode === "create" ? "新增机器" : "保存修改"}
        </Button>
        <span className="text-xs text-slate-500">
          {mode === "create" ? "保存后进入详情页做连接测试" : "保存是整体替换，不会保留旧值"}
        </span>
      </div>
    </form>
  );
}
