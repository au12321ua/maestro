import { useState, type FormEvent } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  EmptyNote,
  ErrorAlert,
  FormActions,
  Loading,
  OneTimeSecret,
  PageTitle,
  SelectField,
  SettingsForm,
  StatusPill,
  TextField,
} from "@/components/common";
import { ApiError, api, errorText } from "@/lib/api";
import { PAT_STATE, fmtTime, label } from "@/lib/format";
import { limit, useLoad, useSession } from "@/lib/hooks";
import type { IssuedPat, Pat, Scope } from "@/lib/types";
import { Trans, t } from "@/i18n";

export function SettingsPage() {
  const { me } = useSession();
  return (
    <div className="grid gap-4">
      <PageTitle title={t("common.nav.settings")} />
      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t("settings.account.title")}</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-2 text-sm sm:grid-cols-3">
          <div>
            <div className="text-xs text-muted-foreground">{t("settings.account.username")}</div>
            {me.username}
          </div>
          <div>
            <div className="text-xs text-muted-foreground">{t("settings.account.displayName")}</div>
            {me.display_name || "—"}
          </div>
          <div>
            <div className="text-xs text-muted-foreground">{t("settings.account.role")}</div>
            {me.role === "admin" ? t("common.app.admin") : t("common.app.member")}
          </div>
          <div>
            <div className="text-xs text-muted-foreground">{t("settings.account.userId")}</div>
            <code className="text-xs">{me.id}</code>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">{t("settings.account.createdAt")}</div>
            {fmtTime(me.created_at)}
          </div>
          <div>
            <div className="text-xs text-muted-foreground">{t("settings.account.status")}</div>
            {me.status === "active" ? t("settings.account.active") : t("settings.account.disabled")}
          </div>
        </CardContent>
      </Card>
      <PasswordCard />
      <PatCard />
      <SdkCard />
    </div>
  );
}

/** How an agent or script uses this service with a PAT (10 section 7). */
function SdkCard() {
  const example = `from maestro_sdk import MaestroClient

client = MaestroClient(base_url="${window.location.origin}", token=os.environ["MAESTRO_TOKEN"])
caps = client.capabilities()
accepted = client.runs.submit(
    name="${t("settings.sdk.exampleRunName")}",
    dataset_version_id="dsv_…",
    execution_mode="agent",
    model_config_id="mdl_…",
    harness_version_id="hrn_…",
)
operation = client.operations.wait(accepted.operation_id, timeout=3600)`;
  return (
    <Card data-testid="sdk-card">
      <CardHeader>
        <CardTitle className="text-base">{t("settings.sdk.title")}</CardTitle>
        <CardDescription>{t("settings.sdk.description")}</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-2 text-sm">
        <pre className="overflow-auto rounded bg-muted p-2 font-mono text-xs">
          pip install &lt;{t("settings.sdk.repo")}&gt;/sdk
        </pre>
        <pre className="overflow-auto rounded bg-zinc-950 p-3 font-mono text-xs leading-relaxed text-zinc-100">
          {"import os\n" + example}
        </pre>
        <div className="text-xs text-muted-foreground">
          {t("settings.sdk.scopes")}
        </div>
        <div className="text-xs text-muted-foreground">
          <Trans
            i18nKey="settings.sdk.skill"
            values={{ path: "skills/maestro-sdk/SKILL.md" }}
            components={{ code: <code /> }}
          />
        </div>
      </CardContent>
    </Card>
  );
}

function PasswordCard() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (next !== confirm) {
      setError(new Error(t("settings.password.mismatch")));
      return;
    }
    setBusy(true);
    setError(undefined);
    try {
      await api.post("/me/password", { body: { current_password: current, new_password: next } });
      toast.success(t("settings.password.changed"));
      setCurrent("");
      setNext("");
      setConfirm("");
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  const field = (name: string) => (error instanceof ApiError ? error.field(name) : undefined);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{t("settings.password.title")}</CardTitle>
      </CardHeader>
      <CardContent>
        <SettingsForm onSubmit={submit}>
          <TextField
            id="pw-current"
            label={t("settings.password.current")}
            type="password"
            autoComplete="current-password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
            error={field("current_password")}
            required
          />
          <TextField
            id="pw-new"
            label={t("settings.password.new")}
            type="password"
            autoComplete="new-password"
            value={next}
            onChange={(e) => setNext(e.target.value)}
            error={field("new_password")}
            required
          />
          <TextField
            id="pw-confirm"
            label={t("settings.password.confirm")}
            type="password"
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            required
          />
          <ErrorAlert error={error} title={t("settings.password.failed")} />
          <FormActions>
            <Button type="submit" disabled={busy}>
              {busy ? t("settings.password.submitting") : t("settings.password.title")}
            </Button>
          </FormActions>
        </SettingsForm>
      </CardContent>
    </Card>
  );
}

function PatCard() {
  const { capabilities } = useSession();
  const pats = useLoad(async () => (await api.get("/me/pats")).data);
  const defaultDays = limit(capabilities, "pat_default_days", 30);
  const maxDays = limit(capabilities, "pat_max_days", 365);
  const defaultRuns = limit(capabilities, "pat_default_run_limit", 1000);
  const maxRuns = limit(capabilities, "pat_max_run_limit", 1000);
  const [name, setName] = useState("");
  const [scope, setScope] = useState<Scope>("experiment");
  const [days, setDays] = useState(String(defaultDays));
  const [runs, setRuns] = useState(String(defaultRuns));
  const [issued, setIssued] = useState<IssuedPat | null>(null);
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(undefined);
    try {
      const res = await api.post("/me/pats", {
        body: {
          name,
          scope,
          expires_in_days: days ? Number(days) : null,
          run_limit: runs ? Number(runs) : null,
        },
      });
      setIssued(res.data);
      setName("");
      await pats.reload();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (pat: Pat) => {
    if (!window.confirm(t("settings.pat.revokeConfirm", { name: pat.name }))) return;
    try {
      await api.post("/me/pats/{pat_id}/revoke", { path: { pat_id: pat.id } });
      toast.success(t("settings.pat.revoked"));
      await pats.reload();
    } catch (err) {
      toast.error(t("settings.pat.revokeFailed", { error: errorText(err) }));
    }
  };

  const remove = async (pat: Pat) => {
    const question =
      pat.state === "active" ? "settings.pat.deleteConfirmActive" : "settings.pat.deleteConfirm";
    if (!window.confirm(t(question, { name: pat.name }))) return;
    try {
      await api.del("/me/pats/{pat_id}", { path: { pat_id: pat.id } });
      toast.success(t("settings.pat.deleted"));
      await pats.reload();
    } catch (err) {
      toast.error(t("settings.pat.deleteFailed", { error: errorText(err) }));
    }
  };
  const field = (n: string) => (error instanceof ApiError ? error.field(n) : undefined);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{t("settings.pat.title")}</CardTitle>
        <CardDescription>{t("settings.pat.description")}</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4">
        {issued && (
          <OneTimeSecret
            title={t("settings.pat.issued", { name: issued.name })}
            value={issued.token}
            onDismiss={() => setIssued(null)}
          />
        )}
        <SettingsForm onSubmit={submit}>
          <TextField
            id="pat-name"
            label={t("settings.pat.name")}
            value={name}
            onChange={(e) => setName(e.target.value)}
            error={field("name")}
            required
          />
          <SelectField
            id="pat-scope"
            label={t("settings.pat.scope")}
            value={scope}
            onChange={(e) => setScope(e.target.value as Scope)}
            error={field("scope")}
          >
            <option value="experiment">{t("settings.pat.scopeExperiment")}</option>
            <option value="read">{t("settings.pat.scopeRead")}</option>
          </SelectField>
          <TextField
            id="pat-days"
            label={t("settings.pat.days")}
            hint={t("settings.pat.daysHint", { count: maxDays })}
            type="number"
            min={1}
            max={maxDays}
            value={days}
            onChange={(e) => setDays(e.target.value)}
            error={field("expires_in_days")}
          />
          <TextField
            id="pat-runs"
            label={t("settings.pat.runLimit")}
            hint={t("settings.pat.runLimitHint", { count: maxRuns })}
            type="number"
            min={1}
            max={maxRuns}
            value={runs}
            onChange={(e) => setRuns(e.target.value)}
            error={field("run_limit")}
          />
          <FormActions>
            <Button type="submit" disabled={busy}>
              {busy ? t("settings.pat.creating") : t("settings.pat.create")}
            </Button>
          </FormActions>
        </SettingsForm>
        <ErrorAlert error={error} title={t("settings.pat.createFailed")} />
        <ErrorAlert error={pats.error} title={t("settings.pat.loadFailed")} />
        {pats.loading && !pats.data && <Loading />}
        {pats.data && pats.data.length === 0 && <EmptyNote>{t("settings.pat.empty")}</EmptyNote>}
        {pats.data && pats.data.length > 0 && (
          <div className="rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t("settings.pat.name")}</TableHead>
                  <TableHead>{t("settings.pat.prefix")}</TableHead>
                  <TableHead>{t("settings.pat.scopeColumn")}</TableHead>
                  <TableHead>{t("settings.account.status")}</TableHead>
                  <TableHead>{t("settings.pat.runsColumn")}</TableHead>
                  <TableHead>{t("settings.pat.expires")}</TableHead>
                  <TableHead>{t("settings.pat.lastUsed")}</TableHead>
                  <TableHead />
                </TableRow>
              </TableHeader>
              <TableBody>
                {pats.data.map((pat) => (
                  <TableRow key={pat.id}>
                    <TableCell>{pat.name}</TableCell>
                    <TableCell className="font-mono text-xs">{pat.token_prefix}…</TableCell>
                    <TableCell>{pat.scope}</TableCell>
                    <TableCell>
                      <StatusPill value={label(PAT_STATE, pat.state)} />
                    </TableCell>
                    <TableCell className="tabular-nums">
                      {pat.accepted_runs} / {pat.run_limit}
                      <span className="ml-1 text-xs text-muted-foreground">
                        {t("settings.pat.remaining", { count: pat.remaining_runs })}
                      </span>
                    </TableCell>
                    <TableCell className="text-xs">{fmtTime(pat.expires_at)}</TableCell>
                    <TableCell className="text-xs">{fmtTime(pat.last_used_at)}</TableCell>
                    <TableCell className="flex justify-end gap-2">
                      {pat.state === "active" && (
                        <Button size="sm" variant="outline" onClick={() => void revoke(pat)}>
                          {t("settings.pat.revoke")}
                        </Button>
                      )}
                      <Button size="sm" variant="outline" onClick={() => void remove(pat)}>
                        {t("settings.pat.delete")}
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
