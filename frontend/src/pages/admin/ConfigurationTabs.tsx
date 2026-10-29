// Administrator tabs for model configurations, harness versions and the team policy (03).
import { useRef, useState, type FormEvent } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
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
  FormRow,
  FormSection,
  Loading,
  Pill,
  SelectField,
  SettingsForm,
  TextAreaField,
  TextField,
} from "@/components/common";
import { t } from "@/i18n";
import { ApiError, api } from "@/lib/api";
import { fmtTime } from "@/lib/format";
import { useLoad } from "@/lib/hooks";
import type {
  Harness,
  ModelConfig,
  ModelProtocol,
  PolicySetting,
  UpstreamVersions,
} from "@/lib/types";

const PROTOCOLS: [ModelProtocol, string][] = [
  ["openai-compatible", t("adminConfig.models.protocolOpenAICompatible")],
  ["openai", "OpenAI Responses"],
  ["anthropic", "Anthropic Messages"],
];

function protocolLabel(value: string) {
  return PROTOCOLS.find(([key]) => key === value)?.[1] ?? value;
}

function parseJson(text: string, what: string): Record<string, unknown> {
  if (!text.trim()) return {};
  try {
    const value = JSON.parse(text);
    if (value && typeof value === "object" && !Array.isArray(value)) return value;
  } catch {
    // fall through
  }
  throw new Error(t("adminConfig.shared.notJsonObject", { what }));
}

// --- model configurations -----------------------------------------------------------------

interface ModelForm {
  name: string;
  provider: string;
  protocol: ModelProtocol;
  endpoint: string;
  model_id: string;
  params: string;
  credential_id: string;
  description: string;
}

const EMPTY_MODEL: ModelForm = {
  name: "",
  provider: "",
  protocol: "openai-compatible",
  endpoint: "",
  model_id: "",
  params: "{}",
  credential_id: "",
  description: "",
};

export function ModelsTab() {
  const models = useLoad(
    async () => (await api.get("/model-configs", { query: { include_disabled: true } })).data,
  );
  const credentials = useLoad(async () => (await api.get("/admin/credentials")).data);
  const [editing, setEditing] = useState<ModelConfig | null>(null);
  const [form, setForm] = useState<ModelForm>(EMPTY_MODEL);
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const keys = (credentials.data ?? []).filter((c) => c.kind === "model_api_key");

  const edit = (model: ModelConfig | null) => {
    setEditing(model);
    setError(undefined);
    setForm(
      model
        ? {
            name: model.name,
            provider: model.provider,
            protocol: model.protocol as ModelProtocol,
            endpoint: model.endpoint,
            model_id: model.model_id,
            params: JSON.stringify(model.params, null, 2),
            credential_id: model.credential_id ?? "",
            description: model.description,
          }
        : EMPTY_MODEL,
    );
  };

  const save = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(undefined);
    try {
      const body = {
        provider: form.provider.trim(),
        protocol: form.protocol,
        endpoint: form.endpoint.trim(),
        model_id: form.model_id.trim(),
        params: parseJson(form.params, t("adminConfig.models.params")),
        credential_id: form.credential_id || null,
        description: form.description,
      };
      if (editing) {
        await api.put("/admin/model-configs/{config_id}", {
          path: { config_id: editing.id },
          body: {
            ...body,
            expected_revision: editing.revision,
          },
        });
        toast.success(t("adminConfig.models.savedRevision", { name: editing.name }));
      } else {
        await api.post("/admin/model-configs", { body: { ...body, name: form.name.trim() } });
        toast.success(t("adminConfig.models.created"));
      }
      edit(null);
      await models.reload();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const act = async (fn: () => Promise<unknown>, done: string) => {
    setError(undefined);
    try {
      await fn();
      toast.success(done);
      await models.reload();
    } catch (err) {
      setError(err);
    }
  };
  const fieldErr = (name: string) => (error instanceof ApiError ? error.field(name) : undefined);

  return (
    <div className="grid gap-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t("adminConfig.models.title")}</CardTitle>
          <CardDescription>{t("adminConfig.models.description")}</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 text-sm">
          {models.loading && !models.data && <Loading />}
          <ErrorAlert error={models.error} />
          {models.data && models.data.length === 0 && (
            <EmptyNote>{t("adminConfig.models.empty")}</EmptyNote>
          )}
          {models.data && models.data.length > 0 && (
            <div className="overflow-auto rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t("adminConfig.shared.name")}</TableHead>
                    <TableHead>{t("adminConfig.models.columnProviderProtocol")}</TableHead>
                    <TableHead>{t("adminConfig.models.columnModel")}</TableHead>
                    <TableHead>{t("adminConfig.models.columnCredential")}</TableHead>
                    <TableHead>{t("adminConfig.models.columnRevision")}</TableHead>
                    <TableHead>{t("adminConfig.shared.state")}</TableHead>
                    <TableHead />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {models.data.map((m) => (
                    <TableRow key={m.id}>
                      <TableCell className="font-medium">{m.name}</TableCell>
                      <TableCell>
                        <div className="font-mono text-xs">{m.provider}</div>
                        <div className="text-xs text-muted-foreground">
                          {protocolLabel(m.protocol)}
                        </div>
                      </TableCell>
                      <TableCell>
                        <div className="font-mono text-xs">{m.model_id}</div>
                        <div
                          className="max-w-64 truncate text-xs text-muted-foreground"
                          title={m.endpoint}
                        >
                          {m.endpoint}
                        </div>
                      </TableCell>
                      <TableCell>
                        {m.credential_name ??
                          (m.credential_bound
                            ? t("adminConfig.models.credentialBound")
                            : t("adminConfig.shared.none"))}
                        {m.credential_problem && (
                          <Pill tone="bad" title={t("adminConfig.models.credentialProblemTitle")}>
                            {m.credential_problem}
                          </Pill>
                        )}
                      </TableCell>
                      <TableCell className="tabular-nums">{m.revision}</TableCell>
                      <TableCell>
                        <Pill tone={m.enabled ? "good" : "muted"}>
                          {m.enabled
                            ? t("adminConfig.shared.enabled")
                            : t("adminConfig.shared.disabled")}
                        </Pill>
                      </TableCell>
                      <TableCell className="whitespace-nowrap text-right">
                        <Button size="sm" variant="outline" onClick={() => edit(m)}>
                          {t("adminConfig.models.edit")}
                        </Button>{" "}
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() =>
                            void act(
                              () =>
                                api.post("/admin/model-configs/{config_id}/enabled", {
                                  path: { config_id: m.id },
                                  body: {
                                    enabled: !m.enabled,
                                  },
                                }),
                              m.enabled
                                ? t("adminConfig.models.disabledToast")
                                : t("adminConfig.shared.enabledToast"),
                            )
                          }
                        >
                          {m.enabled
                            ? t("adminConfig.shared.disable")
                            : t("adminConfig.shared.enable")}
                        </Button>{" "}
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() =>
                            void act(
                              () =>
                                api.del("/admin/model-configs/{config_id}", {
                                  path: { config_id: m.id },
                                }),
                              t("adminConfig.shared.movedToRecycleBin"),
                            )
                          }
                        >
                          {t("adminConfig.shared.delete")}
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

      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            {editing
              ? t("adminConfig.models.editTitle", { name: editing.name })
              : t("adminConfig.models.newTitle")}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <SettingsForm onSubmit={save}>
            {!editing && (
              <TextField
                id="model-name"
                label={t("adminConfig.shared.name")}
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                error={fieldErr("name")}
                required
              />
            )}
            <TextField
              id="model-provider"
              label={t("adminConfig.models.provider")}
              hint={t("adminConfig.models.providerHint")}
              value={form.provider}
              onChange={(e) => setForm({ ...form, provider: e.target.value })}
              error={fieldErr("provider")}
              required
            />
            <SelectField
              id="model-protocol"
              label={t("adminConfig.models.protocol")}
              value={form.protocol}
              onChange={(e) => setForm({ ...form, protocol: e.target.value as ModelProtocol })}
            >
              {PROTOCOLS.map(([value, text]) => (
                <option key={value} value={value}>
                  {text}
                </option>
              ))}
            </SelectField>
            <TextField
              id="model-endpoint"
              label={t("adminConfig.models.endpoint")}
              hint={t("adminConfig.models.endpointHint")}
              value={form.endpoint}
              onChange={(e) => setForm({ ...form, endpoint: e.target.value })}
              error={fieldErr("endpoint")}
              required
            />
            <TextField
              id="model-id"
              label={t("adminConfig.models.modelId")}
              value={form.model_id}
              onChange={(e) => setForm({ ...form, model_id: e.target.value })}
              error={fieldErr("model_id")}
              required
            />
            <SelectField
              id="model-credential"
              label={t("adminConfig.models.credential")}
              hint={t("adminConfig.models.credentialHint")}
              value={form.credential_id}
              onChange={(e) => setForm({ ...form, credential_id: e.target.value })}
              error={fieldErr("credential_id")}
            >
              <option value="">{t("adminConfig.models.noCredential")}</option>
              {keys.map((c) => (
                <option key={c.id} value={c.id} disabled={!c.enabled}>
                  {c.name}
                  {c.enabled ? "" : t("adminConfig.models.credentialDisabled")}
                </option>
              ))}
            </SelectField>
            <TextAreaField
              id="model-params"
              label={t("adminConfig.models.params")}
              hint={t("adminConfig.models.paramsHint", { example: '{"temperature": 0}' })}
              value={form.params}
              onChange={(e) => setForm({ ...form, params: e.target.value })}
              error={fieldErr("params")}
            />
            <TextField
              id="model-description"
              label={t("adminConfig.shared.descriptionOptional")}
              value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
            />
            <FormActions>
              <Button type="submit" disabled={busy}>
                {editing ? t("adminConfig.models.saveRevision") : t("adminConfig.models.create")}
              </Button>
              {editing && (
                <Button type="button" variant="outline" onClick={() => edit(null)}>
                  {t("adminConfig.shared.cancel")}
                </Button>
              )}
            </FormActions>
          </SettingsForm>
          <div className="mt-3">
            <ErrorAlert error={error} />
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

// --- harness versions -----------------------------------------------------------------------

/** Numeric comparison of dotted versions; pre-release suffixes are ignored. */
function newer(a: string, b: string): boolean {
  const parts = (v: string) => v.split(/[-+]/)[0].split(".").map(Number);
  const [x, y] = [parts(a), parts(b)];
  for (let i = 0; i < Math.max(x.length, y.length); i++) {
    if ((x[i] ?? 0) !== (y[i] ?? 0)) return (x[i] ?? 0) > (y[i] ?? 0);
  }
  return false;
}

function day(value: string | null | undefined): string {
  return value ? fmtTime(value).slice(0, 10) : "";
}

/** Versions to choose an upstream version from (N-09): the package's release tags and
 * recent releases, the version this Maestro was tested with, and what the team has. */
function UpstreamHints({
  found,
  loading,
  published,
  chosen,
  onPick,
  onRefresh,
}: {
  found: UpstreamVersions | undefined;
  loading: boolean;
  published: Harness[];
  chosen: string;
  onPick: (version: string) => void;
  onRefresh: () => void;
}) {
  const tested = found?.tested_version;
  const pick = (version: string) => (
    <Button type="button" size="sm" variant="outline" onClick={() => onPick(version)}>
      {t("adminConfig.upstream.fill")}
    </Button>
  );
  const row = (left: string, version: string, note: string) => (
    <div key={left + version} className="flex flex-wrap items-center gap-2">
      <span className="w-24 text-muted-foreground">{left}</span>
      <span className="font-mono">{version}</span>
      <span className="text-muted-foreground">{note}</span>
      {pick(version)}
      {tested && newer(version, tested) && (
        <Pill tone="warn">{t("adminConfig.upstream.newerThanTested")}</Pill>
      )}
    </div>
  );
  return (
    <div className="grid gap-2 rounded-md border p-3 text-xs">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">{t("adminConfig.upstream.title")}</span>
        {found?.registry && (
          <span className="text-muted-foreground">
            {t("adminConfig.upstream.checked", {
              package: found.package,
              registry: found.registry,
              time: fmtTime(found.checked_at),
            })}
          </span>
        )}
        <Button type="button" size="sm" variant="ghost" disabled={loading} onClick={onRefresh}>
          {loading ? t("adminConfig.upstream.checking") : t("adminConfig.upstream.refresh")}
        </Button>
      </div>
      {found?.error && (
        <p className="text-muted-foreground">
          {t("adminConfig.upstream.lookupFailed", { error: found.error })}
        </p>
      )}
      {found?.tags.map((tag) =>
        row(
          tag.tag,
          tag.version,
          t("adminConfig.upstream.publishedOn", { date: day(tag.published_at) }),
        ),
      )}
      {tested &&
        row(t("adminConfig.upstream.testedLabel"), tested, t("adminConfig.upstream.testedNote"))}
      {published.map((h) => (
        <div key={h.id} className="flex flex-wrap items-center gap-2">
          <span className="w-24 text-muted-foreground">
            {t("adminConfig.upstream.teamPublished")}
          </span>
          <span className="font-mono">{h.upstream_version}</span>
          <span className="text-muted-foreground">
            {t("adminConfig.upstream.teamPublishedNote", {
              name: h.name,
              version: h.version,
              state: h.enabled
                ? t("adminConfig.upstream.inUse")
                : t("adminConfig.upstream.disabled"),
            })}
          </span>
        </div>
      ))}
      {found && found.recent.length > 0 && (
        <details>
          <summary className="cursor-pointer text-muted-foreground">
            {t("adminConfig.upstream.recent", { count: found.recent.length })}
          </summary>
          <div className="mt-2 grid gap-1">
            {found.recent.map((r) =>
              row(
                "",
                r.version,
                t("adminConfig.upstream.publishedOn", { date: day(r.published_at) }),
              ),
            )}
          </div>
        </details>
      )}
      {chosen && tested && newer(chosen, tested) && (
        <p className="text-amber-700 dark:text-amber-400">
          {t("adminConfig.upstream.untested", { version: chosen })}
        </p>
      )}
    </div>
  );
}

export function HarnessesTab() {
  const harnesses = useLoad(async () => (await api.get("/harness-versions")).data);
  const adapters = useLoad(async () => (await api.get("/admin/harness-adapters")).data);
  const refreshing = useRef(false);
  // Upstream versions of every adapter that installs a program, for the form and the list.
  const upstream = useLoad(
    async () => {
      const found: Record<string, UpstreamVersions> = {};
      await Promise.all(
        (adapters.data ?? [])
          .filter((a) => a.package)
          .map(async (a) => {
            found[a.agent] = (
              await api.get("/admin/harness-adapters/{agent}/upstream-versions", {
                path: { agent: a.agent },
                query: { refresh: refreshing.current },
              })
            ).data;
          }),
      );
      refreshing.current = false;
      return found;
    },
    [adapters.data],
  );
  const [form, setForm] = useState({
    name: "",
    version: "",
    upstream: "",
    agent: "",
    protocols: [] as ModelProtocol[],
    schema: '{\n  "parameters": {}\n}',
    description: "",
  });
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const agent = form.agent || adapters.data?.[0]?.agent || "";
  const adapter = adapters.data?.find((a) => a.agent === agent);
  // The contract types protocols as plain strings; the adapters list only these three.
  const supported = adapter?.protocols ?? [];

  const create = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(undefined);
    try {
      await api.post("/admin/harness-versions", {
        body: {
          name: form.name.trim(),
          // The name's version defaults to the program's version.
          version: form.version.trim() || form.upstream.trim(),
          upstream_version: adapter?.package ? form.upstream.trim() : null,
          agent,
          protocols: form.protocols.filter((p) => supported.includes(p)),
          parameter_schema: parseJson(form.schema, t("adminConfig.harness.schema")),
          description: form.description,
        },
      });
      toast.success(t("adminConfig.harness.published"));
      setForm({ ...form, version: "", upstream: "" });
      await harnesses.reload();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  const act = async (fn: () => Promise<unknown>, done: string) => {
    setError(undefined);
    try {
      await fn();
      toast.success(done);
      await harnesses.reload();
    } catch (err) {
      setError(err);
    }
  };
  const fieldErr = (name: string) => (error instanceof ApiError ? error.field(name) : undefined);

  return (
    <div className="grid gap-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t("adminConfig.harness.title")}</CardTitle>
          <CardDescription>{t("adminConfig.harness.description")}</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 text-sm">
          {harnesses.loading && !harnesses.data && <Loading />}
          <ErrorAlert error={harnesses.error} />
          {harnesses.data && harnesses.data.length === 0 && (
            <EmptyNote>{t("adminConfig.harness.empty")}</EmptyNote>
          )}
          {harnesses.data && harnesses.data.length > 0 && (
            <div className="overflow-auto rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t("adminConfig.harness.columnNameVersion")}</TableHead>
                    <TableHead>{t("adminConfig.harness.columnAdapter")}</TableHead>
                    <TableHead>{t("adminConfig.harness.columnUpstream")}</TableHead>
                    <TableHead>{t("adminConfig.harness.columnProtocols")}</TableHead>
                    <TableHead>{t("adminConfig.harness.columnParams")}</TableHead>
                    <TableHead>{t("adminConfig.shared.state")}</TableHead>
                    <TableHead />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {harnesses.data.map((h) => (
                    <TableRow key={h.id}>
                      <TableCell>
                        <span className="font-medium">{h.name}</span>{" "}
                        <span className="font-mono text-xs">{h.version}</span>
                        <div
                          className="font-mono text-[11px] text-muted-foreground"
                          title={h.content_digest}
                        >
                          {h.content_digest.slice(0, 19)}…
                        </div>
                      </TableCell>
                      <TableCell className="font-mono text-xs">{h.agent}</TableCell>
                      <TableCell className="text-xs">
                        <span className="font-mono">{h.upstream_version ?? "—"}</span>
                        {h.upstream_version &&
                          upstream.data?.[h.agent]?.tags
                            .filter((tag) => newer(tag.version, h.upstream_version ?? ""))
                            .slice(0, 1)
                            .map((tag) => (
                              <div key={tag.tag} className="text-muted-foreground">
                                {t("adminConfig.harness.upstreamHas", { version: tag.version })}
                              </div>
                            ))}
                      </TableCell>
                      <TableCell className="text-xs">
                        {h.protocols.map(protocolLabel).join(t("adminConfig.shared.listSeparator"))}
                      </TableCell>
                      <TableCell className="font-mono text-xs">
                        {Object.keys(h.parameter_schema.parameters).join(", ") || "—"}
                      </TableCell>
                      <TableCell>
                        <Pill tone={h.enabled ? "good" : "muted"}>
                          {h.enabled
                            ? t("adminConfig.shared.enabled")
                            : t("adminConfig.shared.disabled")}
                        </Pill>
                      </TableCell>
                      <TableCell className="whitespace-nowrap text-right">
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() =>
                            void act(
                              () =>
                                api.post("/admin/harness-versions/{harness_id}/enabled", {
                                  path: { harness_id: h.id },
                                  body: { enabled: !h.enabled },
                                }),
                              h.enabled
                                ? t("adminConfig.shared.disabledToast")
                                : t("adminConfig.shared.enabledToast"),
                            )
                          }
                        >
                          {h.enabled
                            ? t("adminConfig.shared.disable")
                            : t("adminConfig.shared.enable")}
                        </Button>{" "}
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() =>
                            void act(
                              () =>
                                api.del("/admin/harness-versions/{harness_id}", {
                                  path: { harness_id: h.id },
                                }),
                              t("adminConfig.shared.movedToRecycleBin"),
                            )
                          }
                        >
                          {t("adminConfig.shared.delete")}
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

      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t("adminConfig.harness.publishTitle")}</CardTitle>
          {adapters.data && adapters.data.length === 0 && (
            <CardDescription>{t("adminConfig.harness.noAdapters")}</CardDescription>
          )}
        </CardHeader>
        <CardContent>
          <SettingsForm onSubmit={create}>
            <TextField
              id="harness-name"
              label={t("adminConfig.shared.name")}
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              error={fieldErr("name")}
              required
            />
            <TextField
              id="harness-version"
              label={t("adminConfig.harness.version")}
              value={form.version}
              onChange={(e) => setForm({ ...form, version: e.target.value })}
              error={fieldErr("version")}
              placeholder={
                adapter?.package ? t("adminConfig.harness.versionPlaceholder") : undefined
              }
              hint={t("adminConfig.harness.versionHint")}
              required={!adapter?.package}
            />
            <SelectField
              id="harness-agent"
              label={t("adminConfig.harness.agent")}
              value={agent}
              onChange={(e) => setForm({ ...form, agent: e.target.value, protocols: [] })}
              error={fieldErr("agent")}
            >
              {(adapters.data ?? []).map((a) => (
                <option key={a.agent} value={a.agent}>
                  {a.agent}
                </option>
              ))}
            </SelectField>
            {adapter?.package && (
              <FormRow
                label={t("adminConfig.harness.upstream")}
                htmlFor="harness-upstream"
                error={fieldErr("upstream_version")}
                hint={t("adminConfig.harness.upstreamHint", { package: adapter.package })}
              >
                <Input
                  id="harness-upstream"
                  value={form.upstream}
                  onChange={(e) => setForm({ ...form, upstream: e.target.value.trim() })}
                  aria-invalid={fieldErr("upstream_version") ? true : undefined}
                  placeholder={t("adminConfig.harness.upstreamPlaceholder")}
                  required
                />
                <UpstreamHints
                  found={upstream.data?.[agent]}
                  loading={upstream.loading}
                  published={(harnesses.data ?? []).filter((h) => h.agent === agent)}
                  chosen={form.upstream}
                  onPick={(version) => setForm({ ...form, upstream: version })}
                  onRefresh={() => {
                    refreshing.current = true;
                    void upstream.reload();
                  }}
                />
              </FormRow>
            )}
            <FormRow label={t("adminConfig.harness.protocols")} error={fieldErr("protocols")}>
              <div className="grid gap-1 text-sm md:pt-2">
                {supported.map((p) => (
                  <label key={p} className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={form.protocols.includes(p)}
                      onChange={() =>
                        setForm({
                          ...form,
                          protocols: form.protocols.includes(p)
                            ? form.protocols.filter((x) => x !== p)
                            : [...form.protocols, p],
                        })
                      }
                    />
                    {protocolLabel(p)}
                  </label>
                ))}
              </div>
            </FormRow>
            <TextAreaField
              id="harness-schema"
              label={t("adminConfig.harness.schema")}
              hint={
                <div className="grid gap-0.5">
                  <span>
                    {t("adminConfig.harness.schemaHint", {
                      example: '{"parameters": {"max_turns": {"type": "integer", "default": 30}}}',
                    })}
                  </span>
                  {adapter?.parameters && (
                    <span>
                      {t("adminConfig.harness.adapterParams", {
                        list:
                          adapter.parameters.join(t("adminConfig.shared.listSeparator")) ||
                          t("adminConfig.shared.none"),
                      })}
                    </span>
                  )}
                  {adapter?.model_parameters && (
                    <span>
                      {t("adminConfig.harness.modelParams", {
                        list:
                          adapter.model_parameters.join(t("adminConfig.shared.listSeparator")) ||
                          t("adminConfig.shared.none"),
                      })}
                    </span>
                  )}
                </div>
              }
              value={form.schema}
              onChange={(e) => setForm({ ...form, schema: e.target.value })}
            />
            <TextField
              id="harness-description"
              label={t("adminConfig.shared.descriptionOptional")}
              value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
            />
            <FormActions>
              <Button type="submit" disabled={busy || !agent || form.protocols.length === 0}>
                {t("adminConfig.harness.publish")}
              </Button>
            </FormActions>
          </SettingsForm>
          <div className="mt-3">
            <ErrorAlert error={error} />
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

// --- team policy ------------------------------------------------------------------------------

const GROUP_TITLE = t("adminConfig.policy.group", { returnObjects: true });
const GROUPS: [PolicySetting["group"], string][] = [
  ["runs", GROUP_TITLE.runs],
  ["access", GROUP_TITLE.access],
  ["retention", GROUP_TITLE.retention],
];

const SETTING_LABEL: Record<string, string> = t("adminConfig.policy.setting", {
  returnObjects: true,
});

export function PolicyTab() {
  const policy = useLoad(async () => (await api.get("/team-policy")).data);
  const [draft, setDraft] = useState<Record<string, string>>({});
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const data = policy.data;

  const changed = data
    ? Object.fromEntries(
        Object.entries(draft)
          .filter(([name, text]) => text.trim() !== "" && Number(text) !== data.values[name])
          .map(([name, text]) => [name, Number(text)]),
      )
    : {};

  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (!data) return;
    setBusy(true);
    setError(undefined);
    try {
      await api.put("/admin/team-policy", {
        body: { values: changed, expected_revision: data.revision },
      });
      toast.success(t("adminConfig.policy.saved"));
      setDraft({});
      await policy.reload();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{t("adminConfig.policy.title")}</CardTitle>
        <CardDescription>{t("adminConfig.policy.description")}</CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4 text-sm">
        {policy.loading && !data && <Loading />}
        <ErrorAlert error={policy.error} />
        {data && (
          <SettingsForm onSubmit={save}>
            <p className="text-muted-foreground">
              {t("adminConfig.policy.currentRevision", { revision: data.revision })}
              {data.revision === 0 ? t("adminConfig.policy.builtInRevision") : ""}
            </p>
            {GROUPS.map(([group, title]) => (
              <div key={group} className="grid gap-4">
                <FormSection title={title} />
                <div className="grid gap-4">
                  {data.settings
                    .filter((s) => s.group === group)
                    .map((s) => (
                      <TextField
                        key={s.name}
                        id={`policy-${s.name}`}
                        label={SETTING_LABEL[s.name] ?? s.description}
                        type="number"
                        min={s.min}
                        max={s.max}
                        value={draft[s.name] ?? String(data.values[s.name])}
                        onChange={(e) => setDraft({ ...draft, [s.name]: e.target.value })}
                        hint={t("adminConfig.policy.settingHint", {
                          builtIn: s.built_in,
                          min: s.min,
                          max: s.max,
                        })}
                        error={error instanceof ApiError ? error.field(s.name) : undefined}
                      />
                    ))}
                </div>
              </div>
            ))}
            <FormActions>
              <Button type="submit" disabled={busy || Object.keys(changed).length === 0}>
                {t("adminConfig.policy.save", { count: Object.keys(changed).length })}
              </Button>
              {Object.keys(draft).length > 0 && (
                <Button type="button" variant="outline" onClick={() => setDraft({})}>
                  {t("adminConfig.shared.discardChanges")}
                </Button>
              )}
            </FormActions>
            <ErrorAlert error={error} />
          </SettingsForm>
        )}
        <PolicyHistory revision={data?.revision} />
      </CardContent>
    </Card>
  );
}

function PolicyHistory({ revision }: { revision?: number }) {
  const history = useLoad(
    async () => (await api.get("/admin/team-policy/revisions")).data,
    [revision],
  );
  if (!history.data || history.data.length === 0) return null;
  return (
    <details>
      <summary className="cursor-pointer font-medium">
        {t("adminConfig.policy.history", { n: history.data.length })}
      </summary>
      <ul className="mt-2 grid gap-1 text-xs text-muted-foreground">
        {history.data.map((h) => (
          <li key={h.revision}>
            {t("adminConfig.policy.historyItem", {
              revision: h.revision,
              user: h.created_by_username,
              time: fmtTime(h.created_at),
            })}
          </li>
        ))}
      </ul>
    </details>
  );
}
