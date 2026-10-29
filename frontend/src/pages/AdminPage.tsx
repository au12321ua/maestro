import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from "react";
import { Link, Navigate, useParams } from "react-router-dom";
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
  Pill,
  SelectField,
  SettingsForm,
  StatusPill,
  TextField,
} from "@/components/common";
import { ApiError, api, errorText } from "@/lib/api";
import { cn } from "@/lib/utils";
import {
  INVITATION_STATE,
  RECYCLE_TYPE,
  STORAGE_LEVEL,
  fmtBytes,
  fmtTime,
  label,
  shortId,
} from "@/lib/format";
import { limit, useLoad, useSession } from "@/lib/hooks";
import { HarnessesTab, ModelsTab, PolicyTab } from "@/pages/admin/ConfigurationTabs";
import { SetupNetworkTab } from "@/pages/admin/SetupNetworkTab";
import { ADMIN_SECTIONS, type AdminSection } from "@/pages/admin/sections";
import { Trans, t } from "@/i18n";
import type {
  AuditEvent,
  Credential,
  CredentialKind,
  Health,
  Invitation,
  MasterKeyStatus,
  RecycleEntry,
  RecycleObjectType,
  Role,
  User,
} from "@/lib/types";

function inviteLink(token: string) {
  return `${window.location.origin}/invite#${encodeURIComponent(token)}`;
}

const RENDER: Record<AdminSection, () => ReactNode> = {
  members: () => (
    <>
      <MembersTab />
      <InvitationsTab />
    </>
  ),
  credentials: () => (
    <>
      <CredentialsTab />
      <MasterKeyTab />
    </>
  ),
  models: () => <ModelsTab />,
  harnesses: () => <HarnessesTab />,
  policy: () => <PolicyTab />,
  capacity: () => <CapacityTab />,
  "setup-network": () => <SetupNetworkTab />,
  datasets: () => <DatasetImportsTab />,
  "recycle-bin": () => <RecycleBinTab />,
  audit: () => <AuditTab />,
  maintenance: () => <MaintenanceTab />,
};

/** Administration: each section has its own URL (/admin/<id>), reached from the sidebar. */
export function AdminPage() {
  const { section } = useParams();
  const current = ADMIN_SECTIONS.find((s) => s.id === section);
  if (!current) return <Navigate to="/admin/members" replace />;
  return (
    <div>
      <PageTitle title={t("admin.pageTitle", { section: current.label })} />
      <div className="grid min-w-0 gap-8">{RENDER[current.id]()}</div>
    </div>
  );
}

function MembersTab() {
  const { me } = useSession();
  const members = useLoad(async () => (await api.get("/admin/members")).data);
  const [reset, setReset] = useState<{ user: User; link: string } | null>(null);

  const setStatus = async (user: User, action: "disable" | "enable") => {
    const disable = action === "disable";
    const confirmText = disable
      ? t("admin.members.confirmDisable", { name: user.username })
      : t("admin.members.confirmEnable", { name: user.username });
    if (!window.confirm(confirmText)) return;
    try {
      const options = { path: { user_id: user.id }, body: { expected_revision: user.revision } };
      if (action === "disable") await api.post("/admin/members/{user_id}/disable", options);
      else await api.post("/admin/members/{user_id}/enable", options);
      toast.success(disable ? t("admin.toast.disabled") : t("admin.toast.enabled"));
    } catch (err) {
      toast.error(
        disable
          ? t("admin.members.disableFailed", { error: errorText(err) })
          : t("admin.members.enableFailed", { error: errorText(err) }),
      );
    }
    await members.reload();
  };

  const remove = async (user: User) => {
    if (
      !window.confirm(
        t("admin.members.confirmDelete", { name: user.username }),
      )
    )
      return;
    try {
      await api.del("/admin/members/{user_id}", { path: { user_id: user.id } });
      toast.success(t("admin.toast.movedToRecycleBin"));
    } catch (err) {
      toast.error(t("admin.toast.deleteFailed", { error: errorText(err) }));
    }
    await members.reload();
  };

  const issueReset = async (user: User) => {
    if (!window.confirm(t("admin.members.confirmReset", { name: user.username }))) return;
    try {
      const res = await api.post("/admin/members/{user_id}/password-reset", {
        path: { user_id: user.id },
      });
      setReset({ user, link: inviteLink(res.data.token) });
    } catch (err) {
      toast.error(t("admin.members.resetFailed", { error: errorText(err) }));
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{t("admin.members.title")}</CardTitle>
        <CardDescription>{t("admin.members.description")}</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-3">
        {reset && (
          <OneTimeSecret
            title={t("admin.members.resetLinkTitle", { name: reset.user.username })}
            value={reset.link}
            onDismiss={() => setReset(null)}
          />
        )}
        <ErrorAlert error={members.error} />
        {members.loading && !members.data && <Loading />}
        {members.data && (
          <div className="rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t("admin.members.username")}</TableHead>
                  <TableHead>{t("admin.members.displayName")}</TableHead>
                  <TableHead>{t("admin.members.role")}</TableHead>
                  <TableHead>{t("admin.members.status")}</TableHead>
                  <TableHead>{t("admin.members.createdAt")}</TableHead>
                  <TableHead>{t("admin.members.id")}</TableHead>
                  <TableHead />
                </TableRow>
              </TableHeader>
              <TableBody>
                {members.data.map((user) => (
                  <TableRow key={user.id}>
                    <TableCell>{user.username}</TableCell>
                    <TableCell>{user.display_name || "—"}</TableCell>
                    <TableCell>{user.role === "admin" ? t("admin.role.admin") : t("admin.role.member")}</TableCell>
                    <TableCell>
                      {user.status === "active" ? (
                        <Pill tone="good">{t("admin.members.active")}</Pill>
                      ) : (
                        <Pill tone="muted">{t("admin.members.disabledAt", { time: fmtTime(user.disabled_at) })}</Pill>
                      )}
                    </TableCell>
                    <TableCell className="text-xs">{fmtTime(user.created_at)}</TableCell>
                    <TableCell className="font-mono text-xs">{shortId(user.id)}</TableCell>
                    <TableCell className="flex justify-end gap-2">
                      {user.id !== me.id &&
                        (user.status === "active" ? (
                          <Button size="sm" variant="outline" onClick={() => void setStatus(user, "disable")}>
                            {t("admin.actions.disable")}
                          </Button>
                        ) : (
                          <Button size="sm" variant="outline" onClick={() => void setStatus(user, "enable")}>
                            {t("admin.actions.enable")}
                          </Button>
                        ))}
                      <Button size="sm" variant="outline" onClick={() => void issueReset(user)}>
                        {t("admin.members.issueReset")}
                      </Button>
                      {user.id !== me.id && (
                        <Button size="sm" variant="outline" onClick={() => void remove(user)}>
                          {t("admin.actions.delete")}
                        </Button>
                      )}
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

function InvitationsTab() {
  const { capabilities } = useSession();
  const invitations = useLoad(async () => (await api.get("/admin/invitations")).data);
  const [role, setRole] = useState<Role>("member");
  const [note, setNote] = useState("");
  const [hours, setHours] = useState(String(limit(capabilities, "invitation_hours", 72)));
  const [issued, setIssued] = useState<string | null>(null);
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(undefined);
    try {
      const res = await api.post("/admin/invitations", {
        body: {
          role,
          note,
          valid_hours: hours ? Number(hours) : null,
        },
      });
      setIssued(inviteLink(res.data.token));
      setNote("");
      await invitations.reload();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const revoke = async (inv: Invitation) => {
    if (!window.confirm(t("admin.invitations.confirmRevoke"))) return;
    try {
      await api.post("/admin/invitations/{invitation_id}/revoke", {
        path: { invitation_id: inv.id },
      });
      toast.success(t("admin.invitations.revoked"));
    } catch (err) {
      toast.error(t("admin.invitations.revokeFailed", { error: errorText(err) }));
    }
    await invitations.reload();
  };
  const field = (n: string) => (error instanceof ApiError ? error.field(n) : undefined);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{t("admin.invitations.title")}</CardTitle>
        <CardDescription>{t("admin.invitations.description")}</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4">
        {issued && (
          <OneTimeSecret title={t("admin.invitations.linkTitle")} value={issued} onDismiss={() => setIssued(null)} />
        )}
        <SettingsForm onSubmit={submit}>
          <SelectField
            id="inv-role"
            label={t("admin.invitations.role")}
            value={role}
            onChange={(e) => setRole(e.target.value as Role)}
            error={field("role")}
          >
            <option value="member">{t("admin.role.member")}</option>
            <option value="admin">{t("admin.role.admin")}</option>
          </SelectField>
          <TextField
            id="inv-note"
            label={t("admin.invitations.note")}
            value={note}
            onChange={(e) => setNote(e.target.value)}
            error={field("note")}
          />
          <TextField
            id="inv-hours"
            label={t("admin.invitations.validHours")}
            type="number"
            min={1}
            value={hours}
            onChange={(e) => setHours(e.target.value)}
            error={field("valid_hours")}
          />
          <FormActions>
            <Button type="submit" disabled={busy}>
              {busy ? t("admin.invitations.generating") : t("admin.invitations.generate")}
            </Button>
          </FormActions>
        </SettingsForm>
        <ErrorAlert error={error} title={t("admin.invitations.generateFailed")} />
        <ErrorAlert error={invitations.error} />
        {invitations.data && invitations.data.length === 0 && <EmptyNote>{t("admin.invitations.empty")}</EmptyNote>}
        {invitations.data && invitations.data.length > 0 && (
          <div className="rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t("admin.invitations.kind")}</TableHead>
                  <TableHead>{t("admin.invitations.role")}</TableHead>
                  <TableHead>{t("admin.invitations.note")}</TableHead>
                  <TableHead>{t("admin.invitations.status")}</TableHead>
                  <TableHead>{t("admin.invitations.created")}</TableHead>
                  <TableHead>{t("admin.invitations.expires")}</TableHead>
                  <TableHead>{t("admin.invitations.used")}</TableHead>
                  <TableHead />
                </TableRow>
              </TableHeader>
              <TableBody>
                {invitations.data.map((inv) => (
                  <TableRow key={inv.id}>
                    <TableCell>{inv.kind === "invitation" ? t("admin.invitations.kindInvitation") : t("admin.invitations.kindReset")}</TableCell>
                    <TableCell>{inv.role === "admin" ? t("admin.role.admin") : t("admin.role.member")}</TableCell>
                    <TableCell className="text-xs">{inv.note || "—"}</TableCell>
                    <TableCell>
                      <StatusPill value={label(INVITATION_STATE, inv.state)} />
                    </TableCell>
                    <TableCell className="text-xs">{fmtTime(inv.created_at)}</TableCell>
                    <TableCell className="text-xs">{fmtTime(inv.expires_at)}</TableCell>
                    <TableCell className="text-xs">
                      {inv.used_at ? `${fmtTime(inv.used_at)} · ${shortId(inv.used_by)}` : "—"}
                    </TableCell>
                    <TableCell className="text-right">
                      {inv.state === "pending" && (
                        <Button size="sm" variant="outline" onClick={() => void revoke(inv)}>
                          {t("admin.actions.revoke")}
                        </Button>
                      )}
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

const KIND_LABEL: Record<CredentialKind, string> = t("admin.credentials.kindLabel", { returnObjects: true });

function CredentialsTab() {
  const credentials = useLoad(async () => (await api.get("/admin/credentials")).data);
  const [form, setForm] = useState({
    name: "",
    kind: "model_api_key" as CredentialKind,
    provider: "",
    description: "",
    value: "",
  });
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const [replacing, setReplacing] = useState<string | null>(null);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(undefined);
    try {
      await api.post("/admin/credentials", { body: form });
      toast.success(t("admin.credentials.saved"));
      setForm({ ...form, name: "", description: "", value: "" });
      await credentials.reload();
    } catch (err) {
      setError(err);
      setForm((f) => ({ ...f, value: "" }));
    } finally {
      setBusy(false);
    }
  };

  const toggle = async (cred: Credential) => {
    const action = cred.enabled ? "disable" : "enable";
    try {
      const options = {
        path: { credential_id: cred.id },
        body: { expected_revision: cred.revision },
      };
      if (action === "disable")
        await api.post("/admin/credentials/{credential_id}/disable", options);
      else await api.post("/admin/credentials/{credential_id}/enable", options);
      toast.success(cred.enabled ? t("admin.toast.disabled") : t("admin.toast.enabled"));
    } catch (err) {
      toast.error(t("admin.credentials.actionFailed", { error: errorText(err) }));
    }
    await credentials.reload();
  };
  const remove = async (cred: Credential) => {
    if (!window.confirm(t("admin.credentials.confirmDelete", { name: cred.name }))) return;
    try {
      await api.del("/admin/credentials/{credential_id}", { path: { credential_id: cred.id } });
      toast.success(t("admin.toast.movedToRecycleBin"));
    } catch (err) {
      const hint =
        err instanceof ApiError && err.code === "CREDENTIAL_IN_USE"
          ? t("admin.credentials.inUseHint")
          : "";
      toast.error(t("admin.credentials.deleteFailedWithHint", { error: errorText(err), hint }));
    }
    await credentials.reload();
  };
  const field = (n: string) => (error instanceof ApiError ? error.field(n) : undefined);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{t("admin.credentials.title")}</CardTitle>
        <CardDescription>
          {t("admin.credentials.description")}
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4">
        <SettingsForm onSubmit={submit} autoComplete="off">
          <TextField
            id="cred-name"
            label={t("admin.credentials.name")}
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            error={field("name")}
            required
          />
          <SelectField
            id="cred-kind"
            label={t("admin.credentials.kind")}
            value={form.kind}
            onChange={(e) => setForm({ ...form, kind: e.target.value as CredentialKind })}
            error={field("kind")}
          >
            {Object.entries(KIND_LABEL).map(([value, text]) => (
              <option key={value} value={value}>
                {text}
              </option>
            ))}
          </SelectField>
          <TextField
            id="cred-provider"
            label={t("admin.credentials.provider")}
            value={form.provider}
            onChange={(e) => setForm({ ...form, provider: e.target.value })}
            error={field("provider")}
            required
          />
          <TextField
            id="cred-description"
            label={t("admin.credentials.note")}
            value={form.description}
            onChange={(e) => setForm({ ...form, description: e.target.value })}
            error={field("description")}
          />
          <TextField
            id="cred-value"
            label={t("admin.credentials.value")}
            hint={t("admin.credentials.valueHint")}
            type="password"
            autoComplete="new-password"
            value={form.value}
            onChange={(e) => setForm({ ...form, value: e.target.value })}
            error={field("value")}
            required
          />
          <FormActions>
            <Button type="submit" disabled={busy}>
              {busy ? t("admin.actions.saving") : t("admin.credentials.add")}
            </Button>
          </FormActions>
        </SettingsForm>
        <ErrorAlert error={error} title={t("admin.credentials.saveFailed")} />
        <ErrorAlert error={credentials.error} />
        {credentials.data && credentials.data.length === 0 && <EmptyNote>{t("admin.credentials.empty")}</EmptyNote>}
        {credentials.data && credentials.data.length > 0 && (
          <div className="rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t("admin.credentials.name")}</TableHead>
                  <TableHead>{t("admin.credentials.kind")}</TableHead>
                  <TableHead>{t("admin.credentials.provider")}</TableHead>
                  <TableHead>{t("admin.credentials.status")}</TableHead>
                  <TableHead>{t("admin.credentials.revision")}</TableHead>
                  <TableHead>{t("admin.credentials.updated")}</TableHead>
                  <TableHead />
                </TableRow>
              </TableHeader>
              <TableBody>
                {credentials.data.map((cred) => [
                  <TableRow key={cred.id}>
                    <TableCell>
                      {cred.name}
                      {cred.description && (
                        <div className="text-xs text-muted-foreground">{cred.description}</div>
                      )}
                    </TableCell>
                    <TableCell className="text-xs">{KIND_LABEL[cred.kind] ?? cred.kind}</TableCell>
                    <TableCell>{cred.provider}</TableCell>
                    <TableCell>
                      {cred.enabled ? (
                        <Pill tone="good">{t("admin.actions.enable")}</Pill>
                      ) : (
                        <Pill tone="muted">{t("admin.actions.disable")}</Pill>
                      )}
                    </TableCell>
                    <TableCell className="tabular-nums text-xs">
                      r{cred.revision} / k{cred.key_version}
                    </TableCell>
                    <TableCell className="text-xs">{fmtTime(cred.updated_at)}</TableCell>
                    <TableCell className="flex justify-end gap-2">
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={() => setReplacing(replacing === cred.id ? null : cred.id)}
                      >
                        {t("admin.credentials.replaceValue")}
                      </Button>
                      <Button size="sm" variant="outline" onClick={() => void toggle(cred)}>
                        {cred.enabled ? t("admin.actions.disable") : t("admin.actions.enable")}
                      </Button>
                      <Button size="sm" variant="outline" onClick={() => void remove(cred)}>
                        {t("admin.actions.delete")}
                      </Button>
                    </TableCell>
                  </TableRow>,
                  replacing === cred.id && (
                    <TableRow key={`${cred.id}-replace`}>
                      <TableCell colSpan={7}>
                        <ReplaceValue
                          credential={cred}
                          onDone={async () => {
                            setReplacing(null);
                            await credentials.reload();
                          }}
                        />
                      </TableCell>
                    </TableRow>
                  ),
                ])}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function ReplaceValue({ credential, onDone }: { credential: Credential; onDone: () => Promise<void> }) {
  const [value, setValue] = useState("");
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(undefined);
    try {
      await api.put("/admin/credentials/{credential_id}/value", {
        path: { credential_id: credential.id },
        body: {
          value,
          expected_revision: credential.revision,
        },
      });
      setValue("");
      toast.success(t("admin.credentials.replaced"));
      await onDone();
    } catch (err) {
      setValue("");
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  return (
    <SettingsForm onSubmit={submit} autoComplete="off">
      <TextField
        id={`replace-${credential.id}`}
        label={t("admin.credentials.newValue")}
        hint={t("admin.credentials.newValueHint", { name: credential.name })}
        type="password"
        autoComplete="new-password"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        required
      />
      <ErrorAlert error={error} title={t("admin.credentials.replaceFailed")} />
      <FormActions>
        <Button type="submit" size="sm" disabled={busy}>
          {busy ? t("admin.actions.saving") : t("admin.credentials.saveNewValue")}
        </Button>
      </FormActions>
    </SettingsForm>
  );
}

function StorageBadge() {
  const status = useLoad(async () => (await api.get("/system-status")).data);
  const storage = status.data?.detail?.storage as
    | { level: string; free_bytes: number; total_bytes: number }
    | undefined;
  if (!storage) return null;
  const [text, tone] = label(STORAGE_LEVEL, storage.level);
  return (
    <div className="flex items-center gap-2 text-sm" data-testid="storage-badge">
      <span className="text-xs text-muted-foreground">{t("admin.storage.label")}</span>
      <Pill tone={tone}>{text}</Pill>
      <span className="text-xs tabular-nums text-muted-foreground">
        {t("admin.storage.space", { free: fmtBytes(storage.free_bytes), total: fmtBytes(storage.total_bytes) })}
      </span>
    </div>
  );
}

function CapacityTab() {
  const overview = useLoad(async () => (await api.get("/admin/capacity")).data);
  const [form, setForm] = useState({ provider: "", limit: "" });
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);

  const save = async (provider: string, limitText: string) => {
    setBusy(true);
    setError(undefined);
    try {
      const value = limitText.trim() === "" ? null : Number(limitText);
      await api.put("/admin/capacity/providers/{provider}", {
        path: { provider: encodeURIComponent(provider) },
        body: {
          max_concurrent_trials: value,
        },
      });
      toast.success(
        value === null
          ? t("admin.capacity.limitCleared", { provider })
          : t("admin.capacity.limitSet", { provider, value }),
      );
      setForm({ provider: "", limit: "" });
      await overview.reload();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  const submit = (e: FormEvent) => {
    e.preventDefault();
    void save(form.provider.trim(), form.limit);
  };
  const data = overview.data;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{t("admin.capacity.title")}</CardTitle>
        <CardDescription>
          {t("admin.capacity.description")}
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4 text-sm">
        <StorageBadge />
        <ErrorAlert error={overview.error} />
        {overview.loading && !data && <Loading />}
        {data && (
          <div className="grid gap-2 sm:grid-cols-3">
            <Usage label={t("admin.capacity.slots")} used={data.in_use.slots} total={data.host.slots} />
            <Usage label={t("admin.capacity.cpus")} used={data.in_use.cpus} total={data.host.cpus} />
            <Usage label={t("admin.capacity.memory")} used={data.in_use.memory_mb} total={data.host.memory_mb} />
          </div>
        )}
        {data && data.providers.length === 0 && <EmptyNote>{t("admin.capacity.empty")}</EmptyNote>}
        {data && data.providers.length > 0 && (
          <div className="rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t("admin.capacity.provider")}</TableHead>
                  <TableHead>{t("admin.capacity.limit")}</TableHead>
                  <TableHead>{t("admin.capacity.inUse")}</TableHead>
                  <TableHead />
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.providers.map((p) => (
                  <TableRow key={p.provider}>
                    <TableCell className="font-mono text-xs">{p.provider}</TableCell>
                    <TableCell className="tabular-nums">{p.max_concurrent_trials ?? t("admin.capacity.unlimited")}</TableCell>
                    <TableCell className="tabular-nums">{p.in_use}</TableCell>
                    <TableCell className="text-right">
                      {p.max_concurrent_trials != null && (
                        <Button size="sm" variant="outline" disabled={busy} onClick={() => void save(p.provider, "")}>
                          {t("admin.capacity.clearLimit")}
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
        <SettingsForm onSubmit={submit}>
          <TextField
            id="capacity-provider"
            label={t("admin.capacity.provider")}
            value={form.provider}
            onChange={(e) => setForm({ ...form, provider: e.target.value })}
            required
          />
          <TextField
            id="capacity-limit"
            label={t("admin.capacity.limit")}
            hint={t("admin.capacity.limitHint")}
            type="number"
            min={1}
            value={form.limit}
            onChange={(e) => setForm({ ...form, limit: e.target.value })}
            error={error instanceof ApiError ? error.field("max_concurrent_trials") : undefined}
          />
          <FormActions>
            <Button type="submit" disabled={busy || !form.provider.trim()}>
              {t("admin.actions.save")}
            </Button>
          </FormActions>
        </SettingsForm>
        {error !== undefined && !(error instanceof ApiError && error.field("max_concurrent_trials")) && (
          <ErrorAlert error={error} />
        )}
      </CardContent>
    </Card>
  );
}

function Usage({ label, used, total }: { label: string; used: number; total: number }) {
  const ratio = total ? Math.min(1, used / total) : 0;
  return (
    <div className="grid gap-1">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="tabular-nums">
        {Number(used.toFixed(1))} / {Number(total.toFixed(1))}
      </div>
      <div className="h-1.5 rounded bg-muted">
        <div className="h-1.5 rounded bg-sky-600" style={{ width: `${ratio * 100}%` }} />
      </div>
    </div>
  );
}

function MasterKeyTab() {
  const status = useLoad(async () => (await api.get("/admin/master-key")).data);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<MasterKeyStatus | null>(null);

  const rotate = async () => {
    const current = status.data;
    if (!current || current.generation == null) return;
    if (
      !window.confirm(
        t("admin.masterKey.confirmRotate"),
      )
    )
      return;
    setBusy(true);
    try {
      const res = await api.post("/admin/master-key/rotations", {
        body: {
          expected_generation: current.generation,
        },
      });
      setResult(res.data);
      toast.success(t("admin.masterKey.rotated"));
    } catch (err) {
      toast.error(t("admin.masterKey.rotateFailed", { error: errorText(err) }));
    } finally {
      setBusy(false);
      await status.reload();
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{t("admin.masterKey.title")}</CardTitle>
        <CardDescription>{t("admin.masterKey.description")}</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-3 text-sm">
        <ErrorAlert error={status.error} />
        {status.loading && !status.data && <Loading />}
        {status.data && (
          <div className="grid gap-2 sm:grid-cols-4">
            <div>
              <div className="text-xs text-muted-foreground">{t("admin.masterKey.initialized")}</div>
              {status.data.initialized ? t("admin.masterKey.yes") : t("admin.masterKey.no")}
            </div>
            <div>
              <div className="text-xs text-muted-foreground">{t("admin.masterKey.available")}</div>
              {status.data.available ? (
                <Pill tone="good">{t("admin.masterKey.available")}</Pill>
              ) : (
                <Pill tone="bad">{t("admin.masterKey.unavailable")}</Pill>
              )}
            </div>
            <div>
              <div className="text-xs text-muted-foreground">{t("admin.masterKey.generation")}</div>
              {status.data.generation ?? "—"}
            </div>
            <div>
              <div className="text-xs text-muted-foreground">{t("admin.masterKey.rotatedAt")}</div>
              {fmtTime(status.data.rotated_at)}
            </div>
          </div>
        )}
        {result?.reencrypted_credentials != null && (
          <p className="text-sm">
            {t("admin.masterKey.reencrypted", { count: result.reencrypted_credentials })}
          </p>
        )}
        <div>
          <Button
            variant="destructive"
            disabled={busy || !status.data?.available || status.data?.generation == null}
            onClick={() => void rotate()}
          >
            {busy ? t("admin.masterKey.rotating") : t("admin.masterKey.rotate")}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function AuditTab() {
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState<unknown>();
  const [loading, setLoading] = useState(true);

  const load = useCallback(async (after: string | null) => {
    setLoading(true);
    try {
      const res = await api.get("/admin/audit-events", { query: { cursor: after } });
      setEvents((prev) => (after ? [...prev, ...res.data] : res.data));
      setCursor(res.meta.next_cursor ?? null);
      setError(undefined);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(null);
  }, [load]);

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle className="text-base">{t("admin.audit.title")}</CardTitle>
        <Button size="sm" variant="outline" onClick={() => void load(null)}>
          {t("admin.actions.refresh")}
        </Button>
      </CardHeader>
      <CardContent className="grid gap-3">
        <ErrorAlert error={error} />
        {events.length > 0 && (
          <div className="rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t("admin.audit.time")}</TableHead>
                  <TableHead>{t("admin.audit.action")}</TableHead>
                  <TableHead>{t("admin.audit.outcome")}</TableHead>
                  <TableHead>{t("admin.audit.actor")}</TableHead>
                  <TableHead>{t("admin.audit.object")}</TableHead>
                  <TableHead>{t("admin.audit.detail")}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {events.map((ev) => (
                  <TableRow key={ev.id}>
                    <TableCell className="text-xs">{fmtTime(ev.created_at)}</TableCell>
                    <TableCell className="font-mono text-xs">{ev.action}</TableCell>
                    <TableCell>
                      <Pill tone={ev.outcome === "success" || ev.outcome === "ok" ? "good" : "neutral"}>
                        {ev.outcome}
                      </Pill>
                    </TableCell>
                    <TableCell className="text-xs">
                      {shortId(ev.actor_user_id)}
                      {ev.auth_channel && (
                        <div className="text-muted-foreground">
                          {ev.auth_channel}
                          {ev.pat_id && ` · ${shortId(ev.pat_id)}`}
                        </div>
                      )}
                    </TableCell>
                    <TableCell className="text-xs">
                      {ev.object_type ?? "—"} {ev.object_id && shortId(ev.object_id)}
                    </TableCell>
                    <TableCell className="max-w-xs truncate font-mono text-xs" title={JSON.stringify(ev.detail)}>
                      {ev.detail ? JSON.stringify(ev.detail) : "—"}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
        {!loading && events.length === 0 && !error && <EmptyNote>{t("admin.audit.empty")}</EmptyNote>}
        {loading && <Loading />}
        {cursor && !loading && (
          <div className="flex justify-center">
            <Button variant="outline" onClick={() => void load(cursor)}>
              {t("admin.actions.loadMore")}
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

const RECYCLE_TYPES = ["run", "artifact", "dataset", "dataset_version", "credential", "user", "pat"];

function RecycleBinTab() {
  const [state, setState] = useState<"recycled" | "purged">("recycled");
  const [objectType, setObjectType] = useState<RecycleObjectType | "">("");
  const [entries, setEntries] = useState<RecycleEntry[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState<unknown>();
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(
    async (after: string | null) => {
      setLoading(true);
      try {
        const res = await api.get("/admin/recycle-bin", {
          query: {
            state,
            object_type: objectType || undefined,
            cursor: after,
          },
        });
        setEntries((prev) => (after ? [...prev, ...res.data] : res.data));
        setCursor(res.meta.next_cursor ?? null);
        setError(undefined);
      } catch (err) {
        setError(err);
      } finally {
        setLoading(false);
      }
    },
    [state, objectType],
  );

  useEffect(() => {
    void load(null);
  }, [load]);

  const restore = async (entry: RecycleEntry) => {
    if (
      !window.confirm(
        t("admin.recycleBin.confirmRestore", {
          type: RECYCLE_TYPE[entry.object_type] ?? entry.object_type,
          name: entry.label,
        }),
      )
    )
      return;
    setBusy(entry.id);
    try {
      await api.post("/admin/recycle-bin/{entry_id}/restore", { path: { entry_id: entry.id } });
      toast.success(t("admin.recycleBin.restored"));
    } catch (err) {
      toast.error(t("admin.recycleBin.restoreFailed", { error: errorText(err) }));
    } finally {
      setBusy(null);
    }
    await load(null);
  };

  const purge = async (entry: RecycleEntry) => {
    const typed = window.prompt(
      t("admin.recycleBin.purgePrompt", {
        type: RECYCLE_TYPE[entry.object_type] ?? entry.object_type,
        name: entry.label,
        id: entry.object_id,
      }),
    );
    if (typed === null) return;
    if (typed.trim() !== entry.object_id) {
      toast.error(t("admin.recycleBin.idMismatch"));
      return;
    }
    setBusy(entry.id);
    try {
      await api.post("/admin/recycle-bin/{entry_id}/purge", {
        path: { entry_id: entry.id },
        body: { confirm_object_id: typed.trim() },
      });
      toast.success(t("admin.recycleBin.purged"));
    } catch (err) {
      toast.error(t("admin.recycleBin.purgeFailed", { error: errorText(err) }));
    } finally {
      setBusy(null);
    }
    await load(null);
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{t("admin.recycleBin.title")}</CardTitle>
        <CardDescription>
          {t("admin.recycleBin.description")}
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex rounded-md border bg-background p-0.5 text-sm">
            {(["recycled", "purged"] as const).map((value) => (
              <button
                key={value}
                type="button"
                className={cn(
                  "rounded px-3 py-1",
                  state === value ? "bg-primary text-primary-foreground" : "text-muted-foreground",
                )}
                onClick={() => setState(value)}
              >
                {value === "recycled" ? t("admin.recycleBin.viewRecycled") : t("admin.recycleBin.viewPurged")}
              </button>
            ))}
          </div>
          <select
            aria-label={t("admin.recycleBin.objectType")}
            className="h-8 rounded-md border border-input bg-background px-2 text-sm"
            value={objectType}
            onChange={(e) => setObjectType(e.target.value as RecycleObjectType | "")}
          >
            <option value="">{t("admin.recycleBin.allTypes")}</option>
            {RECYCLE_TYPES.map((type) => (
              <option key={type} value={type}>
                {RECYCLE_TYPE[type]}
              </option>
            ))}
          </select>
          <Button size="sm" variant="outline" onClick={() => void load(null)}>
            {t("admin.actions.refresh")}
          </Button>
        </div>
        <ErrorAlert error={error} />
        {!loading && entries.length === 0 && !error && (
          <EmptyNote>{state === "recycled" ? t("admin.recycleBin.emptyRecycled") : t("admin.recycleBin.emptyPurged")}</EmptyNote>
        )}
        {entries.length > 0 && (
          <div className="rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t("admin.recycleBin.type")}</TableHead>
                  <TableHead>{t("admin.recycleBin.name")}</TableHead>
                  <TableHead>{t("admin.recycleBin.owner")}</TableHead>
                  <TableHead>{t("admin.recycleBin.deletedBy")}</TableHead>
                  <TableHead>{t("admin.recycleBin.deletedAt")}</TableHead>
                  {state === "purged" && <TableHead>{t("admin.recycleBin.purgedAt")}</TableHead>}
                  <TableHead />
                </TableRow>
              </TableHeader>
              <TableBody>
                {entries.map((entry) => (
                  <TableRow key={entry.id} data-testid="recycle-row">
                    <TableCell>
                      <Pill tone="neutral">
                        {RECYCLE_TYPE[entry.object_type] ?? entry.object_type}
                      </Pill>
                    </TableCell>
                    <TableCell>
                      {entry.label}
                      <div className="font-mono text-xs text-muted-foreground">
                        {entry.object_id}
                      </div>
                    </TableCell>
                    <TableCell className="text-xs">{entry.owner_username ?? "—"}</TableCell>
                    <TableCell className="text-xs">
                      {entry.deleted_by_username ?? shortId(entry.deleted_by)}
                    </TableCell>
                    <TableCell className="text-xs">{fmtTime(entry.deleted_at)}</TableCell>
                    {state === "purged" && (
                      <TableCell className="text-xs">{fmtTime(entry.purged_at)}</TableCell>
                    )}
                    <TableCell className="flex justify-end gap-2">
                      {entry.state === "recycled" && (
                        <>
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={busy === entry.id}
                            onClick={() => void restore(entry)}
                          >
                            {t("admin.actions.restore")}
                          </Button>
                          <Button
                            size="sm"
                            variant="destructive"
                            disabled={busy === entry.id}
                            onClick={() => void purge(entry)}
                          >
                            {t("admin.actions.purge")}
                          </Button>
                        </>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
        {loading && <Loading />}
        {cursor && !loading && (
          <div className="flex justify-center">
            <Button variant="outline" onClick={() => void load(cursor)}>
              {t("admin.actions.loadMore")}
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

const OPERATION_STATUS_TONE: Record<string, "good" | "bad" | "warn" | "info"> = {
  succeeded: "good",
  failed: "bad",
  interrupted: "bad",
  cancelled: "warn",
  accepted: "info",
  running: "info",
};

/** Import and first preparation happen on the datasets page; here: what ran and how it ended. */
function DatasetImportsTab() {
  const ops = useLoad(async () => {
    const [imports, firsts] = await Promise.all([
      api.get("/operations", { query: { kind: "dataset_import", limit: 50 } }),
      api.get("/operations", { query: { kind: "preparation_first", limit: 50 } }),
    ]);
    return [...imports.data, ...firsts.data].sort((a, b) =>
      b.created_at.localeCompare(a.created_at),
    );
  });
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{t("admin.datasetImports.title")}</CardTitle>
        <CardDescription>
          <Trans
            i18nKey="admin.datasetImports.description"
            components={{ link: <Link className="mx-1 text-primary underline" to="/datasets" /> }}
          />
        </CardDescription>
      </CardHeader>
      <CardContent>
        <ErrorAlert error={ops.error} />
        {!ops.data && !ops.error && <Loading />}
        {ops.data && ops.data.length === 0 && <EmptyNote>{t("admin.datasetImports.empty")}</EmptyNote>}
        {ops.data && ops.data.length > 0 && (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t("admin.datasetImports.operation")}</TableHead>
                <TableHead>{t("admin.datasetImports.status")}</TableHead>
                <TableHead>{t("admin.datasetImports.phase")}</TableHead>
                <TableHead>{t("admin.datasetImports.error")}</TableHead>
                <TableHead>{t("admin.datasetImports.time")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {ops.data.map((op) => (
                <TableRow key={op.id}>
                  <TableCell className="text-xs">
                    {op.kind === "dataset_import"
                      ? t("admin.datasetImports.kindImport")
                      : t("admin.datasetImports.kindFirstPreparation")}{" "}
                    <code className="text-muted-foreground">{shortId(op.id)}</code>
                  </TableCell>
                  <TableCell>
                    <Pill tone={OPERATION_STATUS_TONE[op.status] ?? "neutral"}>{op.status}</Pill>
                  </TableCell>
                  <TableCell className="text-xs">{op.phase ?? "—"}</TableCell>
                  <TableCell className="max-w-80 text-xs break-all">
                    {op.error ? `${op.error.code} ${op.error.message ?? ""}` : "—"}
                  </TableCell>
                  <TableCell className="text-xs">
                    {fmtTime(op.finished_at ?? op.created_at)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}

async function healthProbe(path: string): Promise<Health | { error: string }> {
  try {
    const res = await fetch(path, { credentials: "same-origin" });
    return res.ok ? ((await res.json()) as Health) : { error: `HTTP ${res.status}` };
  } catch (err) {
    return { error: err instanceof Error ? err.message : String(err) };
  }
}

/**
 * 11 section 5: process alive, accepting runs and a specific run being blocked are separate.
 * P0 has no update, drain, backup or restore here; those are not shown as buttons.
 */
function MaintenanceTab() {
  const { capabilities } = useSession();
  const data = useLoad(async () => {
    const [live, ready, status] = await Promise.all([
      healthProbe("/health/live"),
      healthProbe("/health/ready"),
      api.get("/system-status"),
    ]);
    return { live, ready, status: status.data };
  });
  const detail = data.data?.status.detail;
  const probe = (value: Health | { error: string } | undefined) =>
    !value ? "—" : "error" in value ? <Pill tone="bad">{value.error}</Pill> : <code>{value.status}</code>;
  return (
    <div className="grid gap-4">
      <Card data-testid="maintenance">
        <CardHeader className="flex-row items-center justify-between">
          <CardTitle className="text-base">{t("admin.maintenance.healthTitle")}</CardTitle>
          <Button size="sm" variant="outline" onClick={() => void data.reload()}>
            {t("admin.actions.refresh")}
          </Button>
        </CardHeader>
        <CardContent className="grid gap-2 text-sm sm:grid-cols-2">
          <ErrorAlert error={data.error} />
          <Field label={t("admin.maintenance.live")} value={probe(data.data?.live)} />
          <Field label={t("admin.maintenance.ready")} value={probe(data.data?.ready)} />
          <Field
            label={t("admin.maintenance.acceptingRuns")}
            value={
              data.data?.status.accepting_runs ? (
                <Pill tone="good">{t("admin.maintenance.accepting")}</Pill>
              ) : (
                <Pill tone="warn">{t("admin.maintenance.notAccepting")}</Pill>
              )
            }
          />
          <Field
            label={t("admin.maintenance.worker")}
            value={
              detail?.worker_running ? (
                <Pill tone="good">{t("admin.maintenance.workerRunning")}</Pill>
              ) : (
                <Pill tone="bad">{t("admin.maintenance.workerStopped")}</Pill>
              )
            }
          />
          <Field
            label={t("admin.maintenance.executor")}
            value={detail ? (detail.executor ?? t("admin.maintenance.executorNone")) : "—"}
          />
          <Field
            label={t("admin.maintenance.masterKey")}
            value={
              detail?.master_key_available ? (
                <Pill tone="good">{t("admin.maintenance.available")}</Pill>
              ) : (
                <Pill tone="bad">{t("admin.maintenance.unavailable")}</Pill>
              )
            }
          />
          <Field
            label={t("admin.maintenance.schema")}
            value={
              detail ? (
                <span>
                  <Trans
                    i18nKey="admin.maintenance.schemaValue"
                    values={{ current: detail.schema.current ?? "—", head: detail.schema.head }}
                    components={{ code: <code /> }}
                  />
                  {detail.schema.compatible === false && (
                    <Pill tone="bad">{t("admin.maintenance.incompatible")}</Pill>
                  )}
                </span>
              ) : (
                "—"
              )
            }
          />
          <div className="self-end">
            <StorageBadge />
          </div>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t("admin.maintenance.versionsTitle")}</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-2 text-sm sm:grid-cols-2">
          <Field label={t("admin.maintenance.serviceVersion")} value={<code>{capabilities?.service_version ?? "—"}</code>} />
          <Field label={t("admin.maintenance.apiVersion")} value={<code>{capabilities?.api_version ?? "—"}</code>} />
          <Field label={t("admin.maintenance.schemaRevision")} value={<code>{capabilities?.schema_revision ?? "—"}</code>} />
          <Field
            label={t("admin.maintenance.policyVersion")}
            value={<code>{capabilities?.execution.policy_version ?? "—"}</code>}
          />
          <Field
            label={t("admin.maintenance.contractDigest")}
            value={
              <code className="break-all text-xs">{capabilities?.contract_digest ?? "—"}</code>
            }
          />
          <Field label={t("admin.maintenance.teamPolicyRevision")} value={capabilities?.team_policy_revision ?? "—"} />
        </CardContent>
      </Card>
      <Card>
        <CardContent className="pt-6 text-sm text-muted-foreground">
          {t("admin.maintenance.notInWeb")}
          <JsonDetails value={data.data?.status.detail} />
        </CardContent>
      </Card>
    </div>
  );
}

function Field({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="break-all">{value}</div>
    </div>
  );
}

function JsonDetails({ value }: { value: unknown }) {
  return (
    <details className="mt-1">
      <summary className="cursor-pointer text-xs">{t("admin.maintenance.statusDetails")}</summary>
      <pre className="mt-1 overflow-auto text-xs">{JSON.stringify(value ?? null, null, 2)}</pre>
    </details>
  );
}
