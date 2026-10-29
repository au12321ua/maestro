// Administrator tab for the setup network (N-08) and the read-only deployment settings.
import { useState, type FormEvent } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  ErrorAlert,
  FormActions,
  FormRow,
  Loading,
  SettingsForm,
  TextField,
} from "@/components/common";
import { t } from "@/i18n";
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
      toast.success(t("adminConfig.setupNetwork.saved", { revision: saved.data.revision }));
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
          <CardTitle className="text-base">{t("adminConfig.setupNetwork.title")}</CardTitle>
          <CardDescription>{t("adminConfig.setupNetwork.description")}</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4 text-sm">
          {network.loading && !data && <Loading />}
          <ErrorAlert error={network.error} />
          {data && form && (
            <SettingsForm onSubmit={save}>
              <p className="text-muted-foreground">
                {t("adminConfig.setupNetwork.currentRevision", { revision: data.revision })}
                {data.revision === 0
                  ? t("adminConfig.setupNetwork.nothingSet")
                  : t("adminConfig.setupNetwork.byline", {
                      user: data.created_by_username,
                      time: fmtTime(data.created_at),
                    })}
              </p>
              {data.warnings.map((w) => (
                <p key={w} className="text-amber-700 dark:text-amber-400">
                  {w}
                </p>
              ))}
              <TextField
                id="setup-proxy"
                label={t("adminConfig.setupNetwork.proxy")}
                placeholder="http://10.0.0.5:3128"
                value={form.proxy}
                onChange={(e) => change({ proxy: e.target.value })}
                hint={t("adminConfig.setupNetwork.proxyHint")}
                error={fieldError("proxy")}
              />
              <TextField
                id="setup-no-proxy"
                label={t("adminConfig.setupNetwork.noProxy")}
                placeholder="localhost,10.0.0.0/8"
                value={form.no_proxy}
                onChange={(e) => change({ no_proxy: e.target.value })}
                hint={t("adminConfig.setupNetwork.noProxyHint")}
                error={fieldError("no_proxy")}
              />
              <FormRow
                label={t("adminConfig.setupNetwork.env")}
                error={fieldError("env")}
                hint={t("adminConfig.setupNetwork.envHint")}
              >
                {form.env.map((row, index) => (
                  <div key={index} className="grid gap-1">
                    <div className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)_auto] gap-2">
                      <Input
                        aria-label={t("adminConfig.setupNetwork.envName")}
                        placeholder="npm_config_registry"
                        value={row.name}
                        onChange={(e) => setEnv(index, { name: e.target.value })}
                      />
                      <Input
                        aria-label={t("adminConfig.setupNetwork.envValue")}
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
                        {t("adminConfig.shared.delete")}
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
                    {t("adminConfig.setupNetwork.addEnv")}
                  </Button>
                </div>
              </FormRow>
              <TextField
                id="setup-timeout"
                label={t("adminConfig.setupNetwork.timeout")}
                type="number"
                min={30}
                max={86400}
                value={form.timeout}
                onChange={(e) => change({ timeout: e.target.value })}
                hint={t("adminConfig.setupNetwork.timeoutHint")}
                error={fieldError("harness_setup_timeout_sec")}
              />
              <TextField
                id="setup-note"
                label={t("adminConfig.setupNetwork.note")}
                value={form.note}
                onChange={(e) => change({ note: e.target.value })}
                placeholder={t("adminConfig.setupNetwork.notePlaceholder")}
                error={fieldError("note")}
              />
              <FormActions>
                <Button type="submit" disabled={busy || draft === null}>
                  {t("adminConfig.setupNetwork.saveAs", { revision: data.revision + 1 })}
                </Button>
                {draft !== null && (
                  <Button type="button" variant="outline" onClick={() => setDraft(null)}>
                    {t("adminConfig.shared.discardChanges")}
                  </Button>
                )}
              </FormActions>
              <ErrorAlert error={error} />
            </SettingsForm>
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
      <dt className="text-muted-foreground">{t("adminConfig.setupNetwork.proxy")}</dt>
      <dd className="break-all">{network.proxy || t("adminConfig.setupNetwork.noProxyUsed")}</dd>
      {network.no_proxy && (
        <>
          <dt className="text-muted-foreground">{t("adminConfig.setupNetwork.noProxy")}</dt>
          <dd className="break-all">{network.no_proxy}</dd>
        </>
      )}
      <dt className="text-muted-foreground">{t("adminConfig.setupNetwork.env")}</dt>
      <dd className="grid gap-0.5 break-all">
        {env.length === 0
          ? t("adminConfig.shared.none")
          : env.map(([k, v]) => <span key={k}>{`${k}=${v}`}</span>)}
      </dd>
      <dt className="text-muted-foreground">{t("adminConfig.setupNetwork.timeoutShort")}</dt>
      <dd>
        {network.harness_setup_timeout_sec == null
          ? t("adminConfig.setupNetwork.harnessDefault")
          : t("adminConfig.setupNetwork.seconds", { n: network.harness_setup_timeout_sec })}
      </dd>
      {network.note && (
        <>
          <dt className="text-muted-foreground">{t("adminConfig.setupNetwork.noteShort")}</dt>
          <dd className="break-all">{network.note}</dd>
        </>
      )}
    </dl>
  );
}

/** "安装网络第 N 版", expandable to that revision's content (loaded when opened). */
export function SetupNetworkRevision({
  revision,
  prefix = t("adminConfig.setupNetwork.title"),
}: {
  revision: number;
  prefix?: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <details className="text-xs" onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary className="cursor-pointer text-muted-foreground">
        {t("adminConfig.setupNetwork.revision", { prefix, revision })}
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
      <summary className="cursor-pointer font-medium">
        {t("adminConfig.setupNetwork.history", { n: history.data.length })}
      </summary>
      <ul className="mt-2 grid gap-3">
        {history.data.map((h) => (
          <li key={h.revision} className="grid gap-1">
            <span className="text-xs font-medium">
              {t("adminConfig.setupNetwork.historyRevision", { revision: h.revision })}
              {h.revision === 0
                ? t("adminConfig.setupNetwork.nothingSet")
                : t("adminConfig.setupNetwork.byline", {
                    user: h.created_by_username,
                    time: fmtTime(h.created_at),
                  })}
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
        <CardTitle className="text-base">{t("adminConfig.deployment.title")}</CardTitle>
        <CardDescription>{t("adminConfig.deployment.description")}</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-3 text-sm">
        {deployment.loading && !data && <Loading />}
        <ErrorAlert error={deployment.error} />
        {data && (
          <>
            <p>
              {t("adminConfig.deployment.serviceProxy", {
                state: data.service_proxy
                  ? t("adminConfig.deployment.set")
                  : t("adminConfig.deployment.unset"),
              })}
              <span className="text-xs text-muted-foreground">
                {t("adminConfig.deployment.serviceProxyNote")}
              </span>
            </p>
            {data.retired.length > 0 && (
              <p className="text-amber-700 dark:text-amber-400">
                {t("adminConfig.deployment.retired", {
                  keys: data.retired.join(t("adminConfig.shared.listSeparator")),
                })}
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
