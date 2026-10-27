// Administrator tab for the setup network (N-08) and the read-only deployment settings.
import { useState, type FormEvent } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { ErrorAlert, Loading, TextField } from "@/components/common";
import { ApiError, api } from "@/lib/api";
import { fmtTime } from "@/lib/format";
import { useLoad } from "@/lib/hooks";
import type { SetupNetwork } from "@/lib/types";

interface Draft {
  proxy: string;
  no_proxy: string;
  env: { name: string; value: string }[];
  timeout: string;
  note: string;
}

function draftOf(network: SetupNetwork): Draft {
  return {
    proxy: network.proxy,
    no_proxy: network.no_proxy,
    env: Object.entries(network.env).map(([name, value]) => ({ name, value })),
    timeout:
      network.harness_setup_timeout_sec == null ? "" : String(network.harness_setup_timeout_sec),
    note: "",
  };
}

export function SetupNetworkTab() {
  const network = useLoad(async () => (await api.get("/setup-network")).data);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const data = network.data;
  const form = draft ?? (data ? draftOf(data) : null);
  const fieldError = (name: string) => (error instanceof ApiError ? error.field(name) : undefined);

  const change = (patch: Partial<Draft>) => form && setDraft({ ...form, ...patch });
  const setEnv = (index: number, patch: Partial<Draft["env"][number]>) =>
    form &&
    change({
      env: form.env.map((row, i) => (i === index ? { ...row, ...patch } : row)),
    });

  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (!data || !form) return;
    setBusy(true);
    setError(undefined);
    try {
      const saved = await api.put("/admin/setup-network", {
        body: {
          proxy: form.proxy.trim(),
          no_proxy: form.no_proxy.trim(),
          env: Object.fromEntries(
            form.env
              .filter((row) => row.name.trim())
              .map((row) => [row.name.trim(), row.value.trim()]),
          ),
          harness_setup_timeout_sec: form.timeout.trim() ? Number(form.timeout) : null,
          note: form.note.trim(),
          expected_revision: data.revision,
        },
      });
      toast.success(`已保存为第 ${saved.data.revision} 版；之后开始的准备与执行使用它`);
      setDraft(null);
      await network.reload();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="grid gap-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">安装网络</CardTitle>
          <CardDescription>
            本机安装软件时经什么网络：镜像构建、Harness 自身安装和附件脚本用到的代理与包镜像源。
            每次保存生成一个新版本，之后开始的准备与执行立即使用，排队中的运行也一样；Agent
            与验证阶段从不使用。它不属于实验配置，团队成员都能看到内容，所以不能填写任何凭据。
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4 text-sm">
          {network.loading && !data && <Loading />}
          <ErrorAlert error={network.error} />
          {data && form && (
            <form className="grid gap-4" onSubmit={save}>
              <p className="text-muted-foreground">
                当前第 {data.revision} 版
                {data.revision === 0
                  ? "（什么都没设置）"
                  : ` · ${data.created_by_username} · ${fmtTime(data.created_at)}`}
              </p>
              {data.warnings.map((w) => (
                <p key={w} className="text-amber-700 dark:text-amber-400">
                  {w}
                </p>
              ))}
              <div className="grid gap-x-4 gap-y-2 md:grid-cols-2">
                <TextField
                  id="setup-proxy"
                  label="代理"
                  placeholder="http://10.0.0.5:3128"
                  value={form.proxy}
                  onChange={(e) => change({ proxy: e.target.value })}
                  hint="容器要能访问到的地址，不能是 127.0.0.1；留空表示不用代理。用于镜像构建、Harness 安装和附件脚本"
                  error={fieldError("proxy")}
                />
                <TextField
                  id="setup-no-proxy"
                  label="不走代理"
                  placeholder="localhost,10.0.0.0/8"
                  value={form.no_proxy}
                  onChange={(e) => change({ no_proxy: e.target.value })}
                  hint="逗号分隔，不含空格"
                  error={fieldError("no_proxy")}
                />
              </div>
              <div className="grid gap-2">
                <h3 className="font-medium">安装变量</h3>
                <p className="text-xs text-muted-foreground">
                  交给 Harness 安装和附件脚本的环境变量，常用来指定包镜像源，例如
                  npm_config_registry、NVM_NODEJS_ORG_MIRROR、PIP_INDEX_URL。不用于镜像构建。代理只能在上面一栏设置，
                  MAESTRO_ 开头的名字留给平台，像凭据的名字（含 token、key、auth 等）会被拒绝。
                </p>
                {form.env.map((row, index) => (
                  <div key={index} className="grid gap-1">
                    <div className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)_auto] gap-2">
                      <Input
                        aria-label="变量名"
                        placeholder="npm_config_registry"
                        value={row.name}
                        onChange={(e) => setEnv(index, { name: e.target.value })}
                      />
                      <Input
                        aria-label="变量值"
                        placeholder="https://registry.npmmirror.com"
                        value={row.value}
                        onChange={(e) => setEnv(index, { value: e.target.value })}
                      />
                      <Button
                        type="button"
                        variant="outline"
                        onClick={() =>
                          change({
                            env: form.env.filter((_, i) => i !== index),
                          })
                        }
                      >
                        删除
                      </Button>
                    </div>
                    {row.name && fieldError(`env/${row.name.trim()}`) && (
                      <p className="text-xs text-destructive">
                        {fieldError(`env/${row.name.trim()}`)}
                      </p>
                    )}
                  </div>
                ))}
                <div>
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => change({ env: [...form.env, { name: "", value: "" }] })}
                  >
                    添加变量
                  </Button>
                </div>
                {fieldError("env") && (
                  <p className="text-xs text-destructive">{fieldError("env")}</p>
                )}
              </div>
              <div className="grid gap-x-4 gap-y-2 md:grid-cols-2">
                <TextField
                  id="setup-timeout"
                  label="Harness 安装时限（秒）"
                  type="number"
                  min={30}
                  max={86400}
                  value={form.timeout}
                  onChange={(e) => change({ timeout: e.target.value })}
                  hint="留空用 Harness 自己的默认值（多数为 360 秒）；带附件时另加附件的安装时间"
                  error={fieldError("harness_setup_timeout_sec")}
                />
                <TextField
                  id="setup-note"
                  label="说明（写入这一版）"
                  value={form.note}
                  onChange={(e) => change({ note: e.target.value })}
                  placeholder="例如：换 npmmirror，nodejs.org 经代理常断"
                  error={fieldError("note")}
                />
              </div>
              <div className="flex items-center gap-3">
                <Button type="submit" disabled={busy || draft === null}>
                  保存为第 {data.revision + 1} 版
                </Button>
                {draft !== null && (
                  <Button type="button" variant="outline" onClick={() => setDraft(null)}>
                    放弃改动
                  </Button>
                )}
              </div>
              <ErrorAlert error={error} />
            </form>
          )}
          <SetupNetworkHistory revision={data?.revision} />
        </CardContent>
      </Card>
      <DeploymentCard />
    </div>
  );
}

/** One revision's content, as runs and preparations show it. */
export function SetupNetworkContent({ network }: { network: SetupNetwork }) {
  const env = Object.entries(network.env);
  return (
    <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-xs">
      <dt className="text-muted-foreground">代理</dt>
      <dd className="break-all">{network.proxy || "不用"}</dd>
      {network.no_proxy && (
        <>
          <dt className="text-muted-foreground">不走代理</dt>
          <dd className="break-all">{network.no_proxy}</dd>
        </>
      )}
      <dt className="text-muted-foreground">安装变量</dt>
      <dd className="grid gap-0.5 break-all">
        {env.length === 0 ? "无" : env.map(([k, v]) => <span key={k}>{`${k}=${v}`}</span>)}
      </dd>
      <dt className="text-muted-foreground">Harness 安装时限</dt>
      <dd>
        {network.harness_setup_timeout_sec == null
          ? "Harness 默认"
          : `${network.harness_setup_timeout_sec} 秒`}
      </dd>
      {network.note && (
        <>
          <dt className="text-muted-foreground">说明</dt>
          <dd className="break-all">{network.note}</dd>
        </>
      )}
    </dl>
  );
}

/** "安装网络第 N 版", expandable to that revision's content (loaded when opened). */
export function SetupNetworkRevision({
  revision,
  prefix = "安装网络",
}: {
  revision: number;
  prefix?: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <details className="text-xs" onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary className="cursor-pointer text-muted-foreground">
        {prefix}第 {revision} 版
      </summary>
      {open && <RevisionContent revision={revision} />}
    </details>
  );
}

function RevisionContent({ revision }: { revision: number }) {
  const history = useLoad(async () => (await api.get("/setup-network/revisions")).data);
  const found = history.data?.find((h) => h.revision === revision);
  return (
    <div className="mt-1 rounded border bg-muted/40 px-2 py-1">
      {history.loading && <Loading />}
      <ErrorAlert error={history.error} />
      {found && <SetupNetworkContent network={found} />}
    </div>
  );
}

function SetupNetworkHistory({ revision }: { revision?: number }) {
  const history = useLoad(async () => (await api.get("/setup-network/revisions")).data, [revision]);
  if (!history.data || history.data.length <= 1) return null;
  return (
    <details>
      <summary className="cursor-pointer font-medium">版本历史（{history.data.length}）</summary>
      <ul className="mt-2 grid gap-3">
        {history.data.map((h) => (
          <li key={h.revision} className="grid gap-1">
            <span className="text-xs font-medium">
              第 {h.revision} 版
              {h.revision === 0
                ? "（什么都没设置）"
                : ` · ${h.created_by_username} · ${fmtTime(h.created_at)}`}
            </span>
            <SetupNetworkContent network={h} />
          </li>
        ))}
      </ul>
    </details>
  );
}

function DeploymentCard() {
  const deployment = useLoad(async () => (await api.get("/admin/deployment")).data);
  const data = deployment.data;
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">部署配置（只读）</CardTitle>
        <CardDescription>
          来自主机上的 /etc/maestro/config.toml 与服务环境，在主机上修改后重启服务生效。拉取镜像走
          Docker 守护进程自己的代理与镜像源，maestroctl check 会报告它们。
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-3 text-sm">
        {deployment.loading && !data && <Loading />}
        <ErrorAlert error={deployment.error} />
        {data && (
          <>
            <p>
              服务进程代理：{data.service_proxy ? "已设置" : "未设置"}
              <span className="text-xs text-muted-foreground">
                （Hub 下载、镜像仓库令牌与 Terminus 的模型请求经它）
              </span>
            </p>
            {data.retired.length > 0 && (
              <p className="text-amber-700 dark:text-amber-400">
                config.toml 中的 {data.retired.join("、")}{" "}
                已不再使用，会被忽略；请在上面的“安装网络”中重新填写，并从文件中删去。
              </p>
            )}
            <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 font-mono text-xs">
              {Object.entries(data.values).map(([key, value]) => (
                <div key={key} className="contents">
                  <dt className="text-muted-foreground">{key}</dt>
                  <dd className="break-all">{value === null ? "—" : String(value)}</dd>
                </div>
              ))}
            </dl>
          </>
        )}
      </CardContent>
    </Card>
  );
}
