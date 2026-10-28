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
      <PageTitle title={`管理 · ${current.label}`} />
      <div className="grid min-w-0 gap-8">{RENDER[current.id]()}</div>
    </div>
  );
}

function MembersTab() {
  const { me } = useSession();
  const members = useLoad(async () => (await api.get("/admin/members")).data);
  const [reset, setReset] = useState<{ user: User; link: string } | null>(null);

  const setStatus = async (user: User, action: "disable" | "enable") => {
    const verb = action === "disable" ? "停用" : "启用";
    if (!window.confirm(`${verb}账户「${user.username}」？`)) return;
    try {
      const options = { path: { user_id: user.id }, body: { expected_revision: user.revision } };
      if (action === "disable") await api.post("/admin/members/{user_id}/disable", options);
      else await api.post("/admin/members/{user_id}/enable", options);
      toast.success(`已${verb}`);
    } catch (err) {
      toast.error(`${verb}失败：${errorText(err)}`);
    }
    await members.reload();
  };

  const remove = async (user: User) => {
    if (
      !window.confirm(
        `删除账户「${user.username}」？其会话和访问令牌立即失效，账户移入回收站；历史运行保留提交者名称。`,
      )
    )
      return;
    try {
      await api.del("/admin/members/{user_id}", { path: { user_id: user.id } });
      toast.success("已移入回收站");
    } catch (err) {
      toast.error(`删除失败：${errorText(err)}`);
    }
    await members.reload();
  };

  const issueReset = async (user: User) => {
    if (!window.confirm(`为「${user.username}」生成重置密码链接？`)) return;
    try {
      const res = await api.post("/admin/members/{user_id}/password-reset", {
        path: { user_id: user.id },
      });
      setReset({ user, link: inviteLink(res.data.token) });
    } catch (err) {
      toast.error(`生成失败：${errorText(err)}`);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">成员</CardTitle>
        <CardDescription>不开放注册；新成员通过“邀请”加入。</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-3">
        {reset && (
          <OneTimeSecret
            title={`「${reset.user.username}」的重置密码链接`}
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
                  <TableHead>用户名</TableHead>
                  <TableHead>显示名称</TableHead>
                  <TableHead>角色</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead>创建时间</TableHead>
                  <TableHead>ID</TableHead>
                  <TableHead />
                </TableRow>
              </TableHeader>
              <TableBody>
                {members.data.map((user) => (
                  <TableRow key={user.id}>
                    <TableCell>{user.username}</TableCell>
                    <TableCell>{user.display_name || "—"}</TableCell>
                    <TableCell>{user.role === "admin" ? "管理员" : "成员"}</TableCell>
                    <TableCell>
                      {user.status === "active" ? (
                        <Pill tone="good">正常</Pill>
                      ) : (
                        <Pill tone="muted">已停用 {fmtTime(user.disabled_at)}</Pill>
                      )}
                    </TableCell>
                    <TableCell className="text-xs">{fmtTime(user.created_at)}</TableCell>
                    <TableCell className="font-mono text-xs">{shortId(user.id)}</TableCell>
                    <TableCell className="flex justify-end gap-2">
                      {user.id !== me.id &&
                        (user.status === "active" ? (
                          <Button size="sm" variant="outline" onClick={() => void setStatus(user, "disable")}>
                            停用
                          </Button>
                        ) : (
                          <Button size="sm" variant="outline" onClick={() => void setStatus(user, "enable")}>
                            启用
                          </Button>
                        ))}
                      <Button size="sm" variant="outline" onClick={() => void issueReset(user)}>
                        生成重置密码链接
                      </Button>
                      {user.id !== me.id && (
                        <Button size="sm" variant="outline" onClick={() => void remove(user)}>
                          删除
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
    if (!window.confirm("撤销这个链接？")) return;
    try {
      await api.post("/admin/invitations/{invitation_id}/revoke", {
        path: { invitation_id: inv.id },
      });
      toast.success("已撤销");
    } catch (err) {
      toast.error(`撤销失败：${errorText(err)}`);
    }
    await invitations.reload();
  };
  const field = (n: string) => (error instanceof ApiError ? error.field(n) : undefined);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">邀请</CardTitle>
        <CardDescription>生成一次性邀请链接，通过可信渠道发给受邀人。</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4">
        {issued && (
          <OneTimeSecret title="邀请链接" value={issued} onDismiss={() => setIssued(null)} />
        )}
        <SettingsForm onSubmit={submit}>
          <SelectField
            id="inv-role"
            label="角色"
            value={role}
            onChange={(e) => setRole(e.target.value as Role)}
            error={field("role")}
          >
            <option value="member">成员</option>
            <option value="admin">管理员</option>
          </SelectField>
          <TextField
            id="inv-note"
            label="备注"
            value={note}
            onChange={(e) => setNote(e.target.value)}
            error={field("note")}
          />
          <TextField
            id="inv-hours"
            label="有效小时数"
            type="number"
            min={1}
            value={hours}
            onChange={(e) => setHours(e.target.value)}
            error={field("valid_hours")}
          />
          <FormActions>
            <Button type="submit" disabled={busy}>
              {busy ? "生成中…" : "生成邀请链接"}
            </Button>
          </FormActions>
        </SettingsForm>
        <ErrorAlert error={error} title="生成失败" />
        <ErrorAlert error={invitations.error} />
        {invitations.data && invitations.data.length === 0 && <EmptyNote>还没有邀请。</EmptyNote>}
        {invitations.data && invitations.data.length > 0 && (
          <div className="rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>类型</TableHead>
                  <TableHead>角色</TableHead>
                  <TableHead>备注</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead>创建</TableHead>
                  <TableHead>到期</TableHead>
                  <TableHead>使用</TableHead>
                  <TableHead />
                </TableRow>
              </TableHeader>
              <TableBody>
                {invitations.data.map((inv) => (
                  <TableRow key={inv.id}>
                    <TableCell>{inv.kind === "invitation" ? "邀请" : "重置密码"}</TableCell>
                    <TableCell>{inv.role === "admin" ? "管理员" : "成员"}</TableCell>
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
                          撤销
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

const KIND_LABEL: Record<CredentialKind, string> = {
  model_api_key: "模型 API key",
  hub_token: "Hub token",
  generic_secret: "通用密钥",
};

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
      toast.success("凭据已保存");
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
      toast.success(cred.enabled ? "已停用" : "已启用");
    } catch (err) {
      toast.error(`操作失败：${errorText(err)}`);
    }
    await credentials.reload();
  };
  const remove = async (cred: Credential) => {
    if (!window.confirm(`删除凭据「${cred.name}」？它会移入回收站，之后的运行不能再使用它。`)) return;
    try {
      await api.del("/admin/credentials/{credential_id}", { path: { credential_id: cred.id } });
      toast.success("已移入回收站");
    } catch (err) {
      const hint =
        err instanceof ApiError && err.code === "CREDENTIAL_IN_USE"
          ? "。仍有运行在使用该凭据：可先停用，待这些运行结束后再删除"
          : "";
      toast.error(`删除失败：${errorText(err)}${hint}`);
    }
    await credentials.reload();
  };
  const field = (n: string) => (error instanceof ApiError ? error.field(n) : undefined);

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">团队凭据</CardTitle>
        <CardDescription>
          值只写不读：保存后不会再显示，也不会回传到浏览器。替换值只影响之后的运行。
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4">
        <SettingsForm onSubmit={submit} autoComplete="off">
          <TextField
            id="cred-name"
            label="名称"
            value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })}
            error={field("name")}
            required
          />
          <SelectField
            id="cred-kind"
            label="类型"
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
            label="提供商"
            value={form.provider}
            onChange={(e) => setForm({ ...form, provider: e.target.value })}
            error={field("provider")}
            required
          />
          <TextField
            id="cred-description"
            label="说明"
            value={form.description}
            onChange={(e) => setForm({ ...form, description: e.target.value })}
            error={field("description")}
          />
          <TextField
            id="cred-value"
            label="值"
            hint="只写：保存后不再显示"
            type="password"
            autoComplete="new-password"
            value={form.value}
            onChange={(e) => setForm({ ...form, value: e.target.value })}
            error={field("value")}
            required
          />
          <FormActions>
            <Button type="submit" disabled={busy}>
              {busy ? "保存中…" : "添加凭据"}
            </Button>
          </FormActions>
        </SettingsForm>
        <ErrorAlert error={error} title="保存失败" />
        <ErrorAlert error={credentials.error} />
        {credentials.data && credentials.data.length === 0 && <EmptyNote>还没有凭据。</EmptyNote>}
        {credentials.data && credentials.data.length > 0 && (
          <div className="rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>名称</TableHead>
                  <TableHead>类型</TableHead>
                  <TableHead>提供商</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead>修订 / 密钥代</TableHead>
                  <TableHead>更新</TableHead>
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
                      {cred.enabled ? <Pill tone="good">启用</Pill> : <Pill tone="muted">停用</Pill>}
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
                        替换值
                      </Button>
                      <Button size="sm" variant="outline" onClick={() => void toggle(cred)}>
                        {cred.enabled ? "停用" : "启用"}
                      </Button>
                      <Button size="sm" variant="outline" onClick={() => void remove(cred)}>
                        删除
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
      toast.success("已替换；之后的运行使用新值");
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
        label="新值"
        hint={`替换「${credential.name}」的值；只写，保存后不再显示`}
        type="password"
        autoComplete="new-password"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        required
      />
      <ErrorAlert error={error} title="替换失败" />
      <FormActions>
        <Button type="submit" size="sm" disabled={busy}>
          {busy ? "保存中…" : "保存新值"}
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
      <span className="text-xs text-muted-foreground">数据存储</span>
      <Pill tone={tone}>{text}</Pill>
      <span className="text-xs tabular-nums text-muted-foreground">
        剩余 {fmtBytes(storage.free_bytes)} / 共 {fmtBytes(storage.total_bytes)}
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
      toast.success(value === null ? `已取消 ${provider} 的并发上限` : `${provider} 上限已设为 ${value}`);
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
        <CardTitle className="text-base">执行容量</CardTitle>
        <CardDescription>
          运行按“并发 × 单试次资源”整体占用容量，资源确认释放后才归还；清理失败的运行会继续占用。
          主机总量由部署配置决定，这里只设置各模型提供商同时运行的试次上限。
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4 text-sm">
        <StorageBadge />
        <ErrorAlert error={overview.error} />
        {overview.loading && !data && <Loading />}
        {data && (
          <div className="grid gap-2 sm:grid-cols-3">
            <Usage label="试次槽位" used={data.in_use.slots} total={data.host.slots} />
            <Usage label="CPU（核）" used={data.in_use.cpus} total={data.host.cpus} />
            <Usage label="内存（MiB）" used={data.in_use.memory_mb} total={data.host.memory_mb} />
          </div>
        )}
        {data && data.providers.length === 0 && <EmptyNote>还没有设置任何提供商上限。</EmptyNote>}
        {data && data.providers.length > 0 && (
          <div className="rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>提供商</TableHead>
                  <TableHead>同时运行的试次上限</TableHead>
                  <TableHead>当前占用</TableHead>
                  <TableHead />
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.providers.map((p) => (
                  <TableRow key={p.provider}>
                    <TableCell className="font-mono text-xs">{p.provider}</TableCell>
                    <TableCell className="tabular-nums">{p.max_concurrent_trials ?? "不限"}</TableCell>
                    <TableCell className="tabular-nums">{p.in_use}</TableCell>
                    <TableCell className="text-right">
                      {p.max_concurrent_trials != null && (
                        <Button size="sm" variant="outline" disabled={busy} onClick={() => void save(p.provider, "")}>
                          取消上限
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
            label="提供商"
            value={form.provider}
            onChange={(e) => setForm({ ...form, provider: e.target.value })}
            required
          />
          <TextField
            id="capacity-limit"
            label="同时运行的试次上限"
            hint="留空表示不限"
            type="number"
            min={1}
            value={form.limit}
            onChange={(e) => setForm({ ...form, limit: e.target.value })}
            error={error instanceof ApiError ? error.field("max_concurrent_trials") : undefined}
          />
          <FormActions>
            <Button type="submit" disabled={busy || !form.provider.trim()}>
              保存
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
        "轮换主密钥会生成新一代主密钥并重新加密全部凭据。请先确认已有可用备份方案。继续？",
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
      toast.success("主密钥已轮换");
    } catch (err) {
      toast.error(`轮换失败：${errorText(err)}`);
    } finally {
      setBusy(false);
      await status.reload();
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">主密钥</CardTitle>
        <CardDescription>主密钥保存在数据库之外，不可删除；这里只显示状态。</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-3 text-sm">
        <ErrorAlert error={status.error} />
        {status.loading && !status.data && <Loading />}
        {status.data && (
          <div className="grid gap-2 sm:grid-cols-4">
            <div>
              <div className="text-xs text-muted-foreground">已初始化</div>
              {status.data.initialized ? "是" : "否"}
            </div>
            <div>
              <div className="text-xs text-muted-foreground">可用</div>
              {status.data.available ? <Pill tone="good">可用</Pill> : <Pill tone="bad">不可用</Pill>}
            </div>
            <div>
              <div className="text-xs text-muted-foreground">代（generation）</div>
              {status.data.generation ?? "—"}
            </div>
            <div>
              <div className="text-xs text-muted-foreground">上次轮换</div>
              {fmtTime(status.data.rotated_at)}
            </div>
          </div>
        )}
        {result?.reencrypted_credentials != null && (
          <p className="text-sm">本次重新加密了 {result.reencrypted_credentials} 条凭据。</p>
        )}
        <div>
          <Button
            variant="destructive"
            disabled={busy || !status.data?.available || status.data?.generation == null}
            onClick={() => void rotate()}
          >
            {busy ? "轮换中…" : "轮换主密钥"}
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
        <CardTitle className="text-base">审计事件</CardTitle>
        <Button size="sm" variant="outline" onClick={() => void load(null)}>
          刷新
        </Button>
      </CardHeader>
      <CardContent className="grid gap-3">
        <ErrorAlert error={error} />
        {events.length > 0 && (
          <div className="rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>时间</TableHead>
                  <TableHead>动作</TableHead>
                  <TableHead>结果</TableHead>
                  <TableHead>操作者</TableHead>
                  <TableHead>对象</TableHead>
                  <TableHead>详情</TableHead>
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
        {!loading && events.length === 0 && !error && <EmptyNote>没有审计事件。</EmptyNote>}
        {loading && <Loading />}
        {cursor && !loading && (
          <div className="flex justify-center">
            <Button variant="outline" onClick={() => void load(cursor)}>
              加载更多
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
    if (!window.confirm(`恢复${RECYCLE_TYPE[entry.object_type] ?? entry.object_type}「${entry.label}」？`))
      return;
    setBusy(entry.id);
    try {
      await api.post("/admin/recycle-bin/{entry_id}/restore", { path: { entry_id: entry.id } });
      toast.success("已恢复");
    } catch (err) {
      toast.error(`恢复失败：${errorText(err)}`);
    } finally {
      setBusy(null);
    }
    await load(null);
  };

  const purge = async (entry: RecycleEntry) => {
    const typed = window.prompt(
      `彻底删除${RECYCLE_TYPE[entry.object_type] ?? entry.object_type}「${entry.label}」？\n` +
        "此操作不可撤销：内容与文件会被永久删除，只留下删除回执。\n\n" +
        `请输入对象 ID 确认：${entry.object_id}`,
    );
    if (typed === null) return;
    if (typed.trim() !== entry.object_id) {
      toast.error("输入的对象 ID 不一致，未删除");
      return;
    }
    setBusy(entry.id);
    try {
      await api.post("/admin/recycle-bin/{entry_id}/purge", {
        path: { entry_id: entry.id },
        body: { confirm_object_id: typed.trim() },
      });
      toast.success("已彻底删除");
    } catch (err) {
      toast.error(`彻底删除失败：${errorText(err)}`);
    } finally {
      setBusy(null);
    }
    await load(null);
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">回收站</CardTitle>
        <CardDescription>
          删除的对象先进入这里，可以恢复；“彻底删除”会永久移除内容和文件，不可撤销，只保留删除回执。
          主密钥不可删除。
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
                {value === "recycled" ? "回收站" : "删除回执"}
              </button>
            ))}
          </div>
          <select
            aria-label="对象类型"
            className="h-8 rounded-md border border-input bg-background px-2 text-sm"
            value={objectType}
            onChange={(e) => setObjectType(e.target.value as RecycleObjectType | "")}
          >
            <option value="">全部类型</option>
            {RECYCLE_TYPES.map((type) => (
              <option key={type} value={type}>
                {RECYCLE_TYPE[type]}
              </option>
            ))}
          </select>
          <Button size="sm" variant="outline" onClick={() => void load(null)}>
            刷新
          </Button>
        </div>
        <ErrorAlert error={error} />
        {!loading && entries.length === 0 && !error && (
          <EmptyNote>{state === "recycled" ? "回收站是空的。" : "没有删除回执。"}</EmptyNote>
        )}
        {entries.length > 0 && (
          <div className="rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>类型</TableHead>
                  <TableHead>名称</TableHead>
                  <TableHead>所有者</TableHead>
                  <TableHead>删除者</TableHead>
                  <TableHead>删除时间</TableHead>
                  {state === "purged" && <TableHead>彻底删除时间</TableHead>}
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
                            恢复
                          </Button>
                          <Button
                            size="sm"
                            variant="destructive"
                            disabled={busy === entry.id}
                            onClick={() => void purge(entry)}
                          >
                            彻底删除
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
              加载更多
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
        <CardTitle className="text-base">数据集导入与首次准备</CardTitle>
        <CardDescription>
          从数据源检索、添加版本和重试首次准备都在
          <Link className="mx-1 text-primary underline" to="/datasets">
            数据集
          </Link>
          页进行（只有管理员网页会话可以）。下面是最近的导入与首次准备操作。
        </CardDescription>
      </CardHeader>
      <CardContent>
        <ErrorAlert error={ops.error} />
        {!ops.data && !ops.error && <Loading />}
        {ops.data && ops.data.length === 0 && <EmptyNote>还没有导入过数据集。</EmptyNote>}
        {ops.data && ops.data.length > 0 && (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>操作</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>阶段</TableHead>
                <TableHead>错误</TableHead>
                <TableHead>时间</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {ops.data.map((op) => (
                <TableRow key={op.id}>
                  <TableCell className="text-xs">
                    {op.kind === "dataset_import" ? "导入" : "首次准备"}{" "}
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
          <CardTitle className="text-base">健康与可受理</CardTitle>
          <Button size="sm" variant="outline" onClick={() => void data.reload()}>
            刷新
          </Button>
        </CardHeader>
        <CardContent className="grid gap-2 text-sm sm:grid-cols-2">
          <ErrorAlert error={data.error} />
          <Field label="进程存活（/health/live）" value={probe(data.data?.live)} />
          <Field label="就绪（/health/ready）" value={probe(data.data?.ready)} />
          <Field
            label="受理新运行"
            value={
              data.data?.status.accepting_runs ? (
                <Pill tone="good">可受理</Pill>
              ) : (
                <Pill tone="warn">暂不受理</Pill>
              )
            }
          />
          <Field
            label="执行进程"
            value={
              detail?.worker_running ? (
                <Pill tone="good">运行中</Pill>
              ) : (
                <Pill tone="bad">未运行</Pill>
              )
            }
          />
          <Field label="执行器" value={detail ? (detail.executor ?? "未配置") : "—"} />
          <Field
            label="主密钥"
            value={
              detail?.master_key_available ? (
                <Pill tone="good">可用</Pill>
              ) : (
                <Pill tone="bad">不可用</Pill>
              )
            }
          />
          <Field
            label="数据库结构"
            value={
              detail ? (
                <span>
                  <code>{detail.schema.current ?? "—"}</code>（最新{" "}
                  <code>{detail.schema.head}</code>）
                  {detail.schema.compatible === false && <Pill tone="bad">不兼容</Pill>}
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
          <CardTitle className="text-base">版本</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-2 text-sm sm:grid-cols-2">
          <Field label="服务版本" value={<code>{capabilities?.service_version ?? "—"}</code>} />
          <Field label="API 版本" value={<code>{capabilities?.api_version ?? "—"}</code>} />
          <Field label="结构修订" value={<code>{capabilities?.schema_revision ?? "—"}</code>} />
          <Field
            label="错误策略版本"
            value={<code>{capabilities?.execution.policy_version ?? "—"}</code>}
          />
          <Field
            label="契约摘要（OpenAPI）"
            value={
              <code className="break-all text-xs">{capabilities?.contract_digest ?? "—"}</code>
            }
          />
          <Field label="团队策略修订" value={capabilities?.team_policy_revision ?? "—"} />
        </CardContent>
      </Card>
      <Card>
        <CardContent className="pt-6 text-sm text-muted-foreground">
          更新、排空、备份与恢复不在网页中提供（P0 未实现），由部署脚本处理；完整的服务状态见
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
      <summary className="cursor-pointer text-xs">system-status 详情</summary>
      <pre className="mt-1 overflow-auto text-xs">{JSON.stringify(value ?? null, null, 2)}</pre>
    </details>
  );
}
