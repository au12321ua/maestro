import { Fragment, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
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
  LongName,
  ErrorAlert,
  Loading,
  PageTitle,
  Pill,
  SelectField,
  StatusPill,
  TextField,
} from "@/components/common";
import { TaskStatusCells } from "@/components/TaskStatusCells";
import { t, Trans } from "@/i18n";
import { api, errorText } from "@/lib/api";
import { commonPrefix, fmtTime, OPERATION_STATUS, shortId } from "@/lib/format";
import { useInterval, useLoad, useSession } from "@/lib/hooks";
import type {
  Dataset,
  DatasetVersion,
  DatasetVersionDetail,
  Operation,
  SourceDataset,
  SourceVersion,
  SourceVersionDetail,
  Task,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import { SetupNetworkRevision } from "@/pages/admin/SetupNetworkTab";

const PHASES: Record<string, string> = t("datasets.phase", { returnObjects: true });

const TERMINAL = new Set(["succeeded", "failed", "cancelled", "interrupted"]);

/** Polls one Operation until it ends, then calls `onDone` once. */
function OperationLine({
  id,
  label,
  onDone,
}: {
  id: string;
  label: string;
  onDone?: (op: Operation) => void;
}) {
  const [notified, setNotified] = useState(false);
  const op = useLoad(
    async () => (await api.get("/operations/{operation_id}", { path: { operation_id: id } })).data,
    [id],
  );
  const running = !op.data || !TERMINAL.has(op.data.status);
  useInterval(() => void op.reload(), 1000, running);
  if (op.data && !running && !notified) {
    setNotified(true);
    onDone?.(op.data);
  }
  if (!op.data) return <Loading text={`${label}…`} />;
  // Imports and first preparations count tasks; other kinds have no task count.
  const progress =
    op.data.kind === "dataset_import" || op.data.kind === "preparation_first"
      ? op.data.progress
      : null;
  const prepared = op.data.kind === "preparation_first" ? op.data.progress : null;
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-md border px-3 py-2 text-sm">
      <span className="font-medium">{label}</span>
      <StatusPill value={OPERATION_STATUS[op.data.status] ?? [op.data.status, "neutral"]} />
      {op.data.phase && running && (
        <span className="text-muted-foreground">{PHASES[op.data.phase] ?? op.data.phase}</span>
      )}
      {progress?.tasks_total != null && (
        <span className="text-xs text-muted-foreground">
          {t("datasets.operation.tasks", {
            done: progress.tasks_done ?? 0,
            count: progress.tasks_total,
          })}
          {prepared?.admitted != null &&
            t("datasets.operation.admitted", { n: prepared.admitted })}
          {prepared?.failed ? t("datasets.operation.failed", { n: prepared.failed }) : ""}
        </span>
      )}
      {op.data.error && (
        <span className="text-xs text-destructive">
          {t("datasets.operation.error", {
            message: op.data.error.message ?? op.data.error.code,
            code: op.data.error.code,
          })}
        </span>
      )}
      <code className="ml-auto text-xs text-muted-foreground">{shortId(op.data.id, 16)}</code>
    </div>
  );
}

export function DatasetsPage() {
  const { me } = useSession();
  const isAdmin = me.role === "admin" && me.auth_channel === "web_session";
  const datasets = useLoad(
    async () => (await api.get("/datasets", { query: { limit: 200 } })).data,
  );
  // The selected version is in the address (?version=), so a link opens it, including a
  // deleted version that runs still reference.
  const [params, setParams] = useSearchParams();
  const chosen = params.get("version") ?? "";
  const listed = datasets.data?.some((d) => d.versions.some((v) => v.id === chosen));
  const versionId =
    chosen || datasets.data?.find((d) => d.versions.length > 0)?.versions[0].id || "";
  const setVersionId = (id: string) => {
    const next = new URLSearchParams(params);
    next.set("version", id);
    setParams(next, { replace: true });
  };
  const [upstreamOf, setUpstreamOf] = useState<string>();
  const upstreamDataset = datasets.data?.find((d) => d.id === upstreamOf);
  const imported = async (id?: string) => {
    await datasets.reload();
    if (id) setVersionId(id);
  };

  const removeDataset = async (dataset: Dataset) => {
    if (
      !window.confirm(
        t("datasets.page.deleteDatasetConfirm", { name: `${dataset.namespace}/${dataset.name}` }),
      )
    )
      return;
    try {
      await api.del("/admin/datasets/{dataset_id}", { path: { dataset_id: dataset.id } });
      toast.success(t("datasets.page.deleted"));
    } catch (err) {
      toast.error(t("datasets.page.deleteFailed", { error: errorText(err) }));
    }
    await datasets.reload();
  };

  const removeVersion = async (dataset: Dataset, version: DatasetVersion) => {
    if (
      !window.confirm(
        t("datasets.page.deleteVersionConfirm", {
          dataset: dataset.name,
          ref: version.upstream_ref,
        }),
      )
    )
      return;
    try {
      await api.del("/admin/dataset-versions/{version_id}", { path: { version_id: version.id } });
      toast.success(t("datasets.page.deleted"));
    } catch (err) {
      toast.error(t("datasets.page.deleteFailed", { error: errorText(err) }));
    }
    await datasets.reload();
  };

  return (
    <div>
      <PageTitle title={t("datasets.page.title")} />
      <p className="mb-4 text-sm text-muted-foreground">
        {t("datasets.page.intro")}
      </p>
      {isAdmin && <AddDataset datasets={datasets.data ?? []} onImported={imported} />}
      {isAdmin && upstreamDataset && (
        <UpstreamVersions
          key={upstreamDataset.id}
          dataset={upstreamDataset}
          onImported={imported}
          onClose={() => setUpstreamOf(undefined)}
        />
      )}
      <ErrorAlert error={datasets.error} />
      {datasets.loading && !datasets.data && <Loading />}
      {datasets.data && datasets.data.length === 0 && <EmptyNote>{t("datasets.page.empty")}</EmptyNote>}
      <div className="grid gap-4 lg:grid-cols-[minmax(260px,1fr)_minmax(0,3fr)]">
        <div className="grid content-start gap-3">
          {datasets.data?.map((dataset) => (
            <Card key={dataset.id}>
              <CardHeader className="pb-2">
                <CardTitle className="flex items-center justify-between gap-2 text-base">
                  {dataset.namespace}/{dataset.name}
                  {isAdmin && (
                    <Button
                      size="sm"
                      variant="ghost"
                      title={t("datasets.page.deleteDataset")}
                      aria-label={t("datasets.page.deleteDatasetLabel", { name: dataset.name })}
                      onClick={() => void removeDataset(dataset)}
                    >
                      <Trash2 />
                    </Button>
                  )}
                </CardTitle>
                <div className="text-xs text-muted-foreground">
                  {t("datasets.page.source", { kind: dataset.source_kind })}
                  {dataset.credential_id
                    ? t("datasets.page.teamCredential")
                    : t("datasets.page.anonymous")}
                  {dataset.description && ` · ${dataset.description}`}
                </div>
                {isAdmin && (
                  <div>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => setUpstreamOf(dataset.id)}
                    >
                      {t("datasets.page.checkUpstream")}
                    </Button>
                  </div>
                )}
              </CardHeader>
              <CardContent className="grid gap-1">
                {dataset.versions.length === 0 && (
                  <span className="text-xs text-muted-foreground">{t("datasets.page.noVersions")}</span>
                )}
                {dataset.versions.map((version) => (
                  <div key={version.id} className="flex items-center gap-1">
                    <button
                      type="button"
                      onClick={() => setVersionId(version.id)}
                      className={cn(
                        "grid flex-1 gap-0.5 rounded-md border px-2 py-1 text-left text-sm",
                        versionId === version.id ? "border-primary bg-accent" : "hover:bg-muted",
                      )}
                    >
                      <span>
                        {version.upstream_ref}
                        <span className="ml-1 text-xs text-muted-foreground">
                          ({version.ref_kind}) {shortId(version.resolved_digest, 15)}
                        </span>
                      </span>
                      <span className="text-xs text-muted-foreground">
                        {t("datasets.page.versionCounts", {
                          submittable: version.submittable_task_count,
                          total: version.task_count,
                          ready: version.cache_ready_task_count,
                        })}
                      </span>
                    </button>
                    {isAdmin && (
                      <Button
                        size="sm"
                        variant="ghost"
                        title={t("datasets.page.deleteVersion")}
                        aria-label={t("datasets.page.deleteVersionLabel", { ref: version.upstream_ref })}
                        onClick={() => void removeVersion(dataset, version)}
                      >
                        <Trash2 />
                      </Button>
                    )}
                  </div>
                ))}
              </CardContent>
            </Card>
          ))}
        </div>
        {versionId && (
          <div className="grid min-w-0 content-start gap-2">
            {datasets.data && !listed && chosen && (
              <p className="text-sm text-amber-800">
                {t("datasets.page.notListed")}
              </p>
            )}
            <VersionTasks
              key={versionId}
              versionId={versionId}
              isAdmin={isAdmin}
              onChange={datasets.reload}
            />
          </div>
        )}
      </div>
    </div>
  );
}

/** Where a dataset's versions come from: the source and the credential used to read it. */
interface SourceTarget {
  source_kind: string;
  namespace: string;
  name: string;
  credential_id: string | null;
}

/** An upstream version against what the platform holds: imported (same content), not
 * imported, or carrying a tag that an imported version used and that moved since. */
interface UpstreamRow {
  version: SourceVersion;
  imported?: DatasetVersion;
  movedTags: { tag: string; from: DatasetVersion }[];
}

function classify(upstream: SourceVersion[], local: DatasetVersion[]): UpstreamRow[] {
  return upstream.map((version) => ({
    version,
    imported: local.find((v) => v.resolved_digest === version.resolved_digest),
    movedTags: local.flatMap((from) =>
      from.ref_kind === "tag" &&
      version.tags.includes(from.upstream_ref) &&
      from.resolved_digest !== version.resolved_digest
        ? [{ tag: from.upstream_ref, from }]
        : [],
    ),
  }));
}

function newestFirst(versions: DatasetVersion[]): DatasetVersion[] {
  return [...versions].sort((a, b) => b.created_at.localeCompare(a.created_at));
}

/** Tasks added, removed and changed (same key, other content) against a local version. */
function TaskDiff({ preview, local }: { preview: SourceVersionDetail; local: DatasetVersion[] }) {
  const [baseId, setBaseId] = useState(newestFirst(local)[0]?.id ?? "");
  const base = useLoad(
    async () =>
      baseId
        ? (await api.get("/dataset-versions/{version_id}/tasks", { path: { version_id: baseId } }))
            .data
        : undefined,
    [baseId],
  );
  if (local.length === 0) return null;
  const before = new Map((base.data ?? []).map((t) => [t.task_key, t.content.digest]));
  const after = new Map(preview.tasks.map((t) => [t.task_key, t.content_digest]));
  const added = [...after.keys()].filter((k) => !before.has(k)).sort();
  const removed = [...before.keys()].filter((k) => !after.has(k)).sort();
  const changed = [...after.keys()].filter((k) => before.has(k) && before.get(k) !== after.get(k)).sort();
  const groups: { id: string; title: string; heading: string; keys: string[] }[] = [
    {
      id: "added",
      title: t("datasets.diff.added"),
      heading: t("datasets.diff.addedTasks", { count: added.length }),
      keys: added,
    },
    {
      id: "removed",
      title: t("datasets.diff.removed"),
      heading: t("datasets.diff.removedTasks", { count: removed.length }),
      keys: removed,
    },
    {
      id: "changed",
      title: t("datasets.diff.changed"),
      heading: t("datasets.diff.changedTasks", { count: changed.length }),
      keys: changed,
    },
  ];
  return (
    <div className="grid gap-1">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <label htmlFor="diff-base" className="shrink-0 text-muted-foreground">
          {t("datasets.diff.compareWith")}
        </label>
        <select
          id="diff-base"
          className="h-8 min-w-0 max-w-full rounded-md border border-input bg-background px-2 text-sm"
          value={baseId}
          onChange={(e) => setBaseId(e.target.value)}
        >
          {newestFirst(local).map((v) => (
            <option key={v.id} value={v.id}>
              {t("datasets.diff.localOption", { ref: v.upstream_ref, time: fmtTime(v.created_at) })}
            </option>
          ))}
        </select>
      </div>
      <ErrorAlert error={base.error} />
      {base.loading && !base.data && <Loading />}
      {base.data && (
        <div className="grid gap-1 text-xs">
          <div>
            {[
              ...groups.map(({ title, keys }) => `${title} ${keys.length}`),
              t("datasets.diff.unchanged", {
                count: preview.tasks.length - added.length - changed.length,
              }),
            ].join(" · ")}
          </div>
          {groups
            .filter(({ keys }) => keys.length > 0)
            .map(({ id, heading, keys }) => (
              <details key={id}>
                <summary className="cursor-pointer">{heading}</summary>
                <div className="max-h-40 overflow-auto font-mono text-muted-foreground">
                  {keys.map((k) => (
                    <div key={k} className="break-all">
                      {k}
                    </div>
                  ))}
                </div>
              </details>
            ))}
        </div>
      )}
    </div>
  );
}

/** Upstream versions of one dataset, a preview of one of them and its import. Used both
 * when adding a dataset and from an existing dataset's card ("检查上游更新"). */
function SourceVersions({
  target,
  local,
  onImported,
}: {
  target: SourceTarget;
  local: DatasetVersion[];
  onImported: (versionId?: string) => Promise<void>;
}) {
  const common = {
    source_kind: target.source_kind,
    namespace: target.namespace,
    name: target.name,
    credential_id: target.credential_id ?? undefined,
  };
  const upstream = useLoad(
    async () => (await api.get("/dataset-search/versions", { query: common })).data,
    [target.source_kind, target.namespace, target.name, target.credential_id],
  );
  const [ref, setRef] = useState("");
  const [preview, setPreview] = useState<SourceVersionDetail>();
  const [prepare, setPrepare] = useState(true);
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const [operation, setOperation] = useState<string>();
  const rows = upstream.data ? classify(upstream.data, local) : undefined;
  // The preview's reference is a tag an imported version used, now on other content.
  const movedFrom = preview
    ? local.find(
        (v) =>
          v.ref_kind === "tag" &&
          v.upstream_ref === preview.upstream_ref &&
          v.resolved_digest !== preview.resolved_digest,
      )
    : undefined;

  const look = async (value: string) => {
    setBusy(true);
    setError(undefined);
    setPreview(undefined);
    try {
      setPreview((await api.get("/dataset-search/version", { query: { ...common, ref: value } })).data);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  const add = async (accept: boolean) => {
    if (!preview) return;
    setBusy(true);
    setError(undefined);
    try {
      const op = await api.post("/admin/dataset-imports", {
        body: {
          source_kind: target.source_kind,
          namespace: preview.namespace,
          name: preview.name,
          ref: preview.upstream_ref,
          credential_id: target.credential_id,
          prepare,
          accept_changed_content: accept,
        },
      });
      setOperation(op.data.id);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  const confirmMoved = (tag: string) =>
    window.confirm(t("datasets.source.confirmMoved", { tag }));

  return (
    <div className="grid gap-3">
      <ErrorAlert error={upstream.error} title={t("datasets.source.upstreamError")} />
      {upstream.loading && !upstream.data && <Loading text={t("datasets.source.upstreamLoading")} />}
      {rows && rows.length === 0 && (
        <EmptyNote>{t("datasets.source.noUpstream")}</EmptyNote>
      )}
      {rows && rows.length > 0 && (
        <div className="rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t("datasets.source.columns.revision")}</TableHead>
                <TableHead>{t("datasets.source.columns.tags")}</TableHead>
                <TableHead>{t("datasets.source.columns.digest")}</TableHead>
                <TableHead>{t("datasets.source.columns.published")}</TableHead>
                <TableHead>{t("datasets.source.columns.onPlatform")}</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map(({ version: v, imported, movedTags }) => (
                <TableRow key={v.ref}>
                  <TableCell>{v.ref}</TableCell>
                  <TableCell className="text-xs">{v.tags.join(", ") || "—"}</TableCell>
                  <TableCell className="font-mono text-xs" title={v.resolved_digest}>
                    {shortId(v.resolved_digest, 19)}
                  </TableCell>
                  <TableCell className="text-xs">
                    {fmtTime(v.published_at)} {v.yanked && <Pill tone="warn">{t("datasets.source.yanked")}</Pill>}
                  </TableCell>
                  <TableCell className="text-xs">
                    {imported ? (
                      <Link
                        className="text-primary underline"
                        to={`/datasets?version=${imported.id}`}
                      >
                        {t("datasets.source.imported", { ref: imported.upstream_ref })}
                      </Link>
                    ) : (
                      <Pill tone="info">{t("datasets.source.notImported")}</Pill>
                    )}
                    {movedTags.map(({ tag, from }) => (
                      <div key={tag} className="mt-1">
                        <Pill
                          tone="warn"
                          title={t("datasets.source.localVersion", {
                            digest: shortId(from.resolved_digest, 19),
                          })}
                        >
                          {t("datasets.source.tagMoved", { tag })}
                        </Pill>
                      </div>
                    ))}
                  </TableCell>
                  <TableCell>
                    {!imported && (
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={busy}
                        // A moved tag is looked up by the tag, so the new version keeps it.
                        onClick={() => void look(movedTags[0]?.tag ?? v.ref)}
                      >
                        {t("datasets.source.addAsNewEllipsis")}
                      </Button>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
      <form
        className="flex items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          void look(ref.trim());
        }}
      >
        <TextField
          id={`ref-${target.namespace}-${target.name}`}
          label={t("datasets.source.addRef")}
          value={ref}
          placeholder={t("datasets.source.refPlaceholder")}
          onChange={(e) => setRef(e.target.value)}
        />
        <Button type="submit" variant="outline" disabled={busy || !ref.trim()}>
          {t("datasets.source.look")}
        </Button>
      </form>
      <ErrorAlert error={error} />
      {preview && (
        <div className="grid gap-2 rounded-md border p-3 text-sm">
          <div>
            <Trans
              i18nKey="datasets.source.preview"
              count={preview.tasks.length}
              values={{
                ref: `${preview.namespace}/${preview.name}@${preview.upstream_ref}`,
                kind: preview.ref_kind,
                digest: shortId(preview.resolved_digest, 23),
                count: preview.tasks.length,
              }}
              components={{ code: <code title={preview.resolved_digest} /> }}
            />
            {preview.existing_version_id && <Pill tone="info">{t("datasets.source.alreadyAdded")}</Pill>}
            {movedFrom && <Pill tone="warn">{t("datasets.source.tagMovedShort")}</Pill>}
          </div>
          {preview.description && <p className="text-muted-foreground">{preview.description}</p>}
          {local.length > 0 ? (
            <TaskDiff key={preview.resolved_digest} preview={preview} local={local} />
          ) : (
            <div className="max-h-40 overflow-auto font-mono text-xs text-muted-foreground">
              {preview.tasks.map((t) => (
                <div key={t.task_key} className="break-all">
                  {t.task_key} {shortId(t.content_digest, 19)}
                </div>
              ))}
            </div>
          )}
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={prepare}
              onChange={(e) => setPrepare(e.target.checked)}
            />
            {t("datasets.source.prepareAfter")}
          </label>
          <div className="flex gap-2">
            <Button
              disabled={busy || !!operation}
              onClick={() => {
                if (movedFrom && !confirmMoved(preview.upstream_ref)) return;
                void add(!!movedFrom);
              }}
            >
              {preview.existing_version_id ? t("datasets.source.recheck") : t("datasets.source.addAsNew")}
            </Button>
          </div>
        </div>
      )}
      {operation && (
        <OperationLine
          id={operation}
          label={t("datasets.source.operation")}
          onDone={(op) => {
            setOperation(undefined);
            const imported = op.kind === "dataset_import" ? op.result : null;
            if (op.status === "succeeded") {
              toast.success(
                imported?.existing ? t("datasets.source.existing") : t("datasets.source.added"),
              );
              setPreview(undefined);
              void upstream.reload();
              void onImported(imported?.dataset_version_id);
            } else if (op.error?.code === "CONTENT_REFERENCE_CHANGED" && preview) {
              if (confirmMoved(preview.upstream_ref)) void add(true);
            } else {
              toast.error(t("datasets.source.addFailed", { error: op.error?.message ?? op.status }));
            }
          }}
        />
      )}
    </div>
  );
}

function AddDataset({
  datasets,
  onImported,
}: {
  datasets: Dataset[];
  onImported: (versionId?: string) => Promise<void>;
}) {
  const sources = useLoad(async () => (await api.get("/dataset-sources")).data);
  const credentials = useLoad(async () =>
    (await api.get("/admin/credentials")).data.filter((c) => c.kind === "hub_token" && c.enabled),
  );
  const [open, setOpen] = useState(false);
  const [kind, setKind] = useState("");
  const [credentialId, setCredentialId] = useState("");
  const [query, setQuery] = useState("");
  const [found, setFound] = useState<SourceDataset[]>();
  const [target, setTarget] = useState<SourceDataset>();
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const sourceKind = kind || sources.data?.[0]?.kind || "hub";

  const search = async () => {
    setBusy(true);
    setError(undefined);
    setTarget(undefined);
    try {
      const common = { source_kind: sourceKind, credential_id: credentialId || undefined };
      setFound((await api.get("/dataset-search", { query: { q: query, ...common } })).data);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  // Picking a dataset the platform already has compares against its versions.
  const existing =
    target &&
    datasets.find(
      (d) =>
        d.source_kind === sourceKind &&
        d.namespace === target.namespace &&
        d.name === target.name,
    );

  if (!open) {
    return (
      <div className="mb-4">
        <Button variant="outline" onClick={() => setOpen(true)}>
          {t("datasets.add.title")}
        </Button>
      </div>
    );
  }
  return (
    <Card className="mb-4">
      <CardHeader>
        <CardTitle className="flex items-center justify-between gap-2 text-base">
          {t("datasets.add.title")}
          <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>
            {t("datasets.collapse")}
          </Button>
        </CardTitle>
        <p className="text-xs text-muted-foreground">
          {t("datasets.add.intro")}
        </p>
      </CardHeader>
      <CardContent className="grid gap-3">
        <div className="grid gap-3 md:grid-cols-[160px_minmax(0,1fr)_minmax(0,1fr)]">
          <SelectField
            id="source"
            label={t("datasets.add.source")}
            value={sourceKind}
            onChange={(e) => setKind(e.target.value)}
          >
            {sources.data?.map((s) => (
              <option key={s.kind} value={s.kind}>
                {s.kind}
              </option>
            ))}
          </SelectField>
          <SelectField
            id="credential"
            label={t("datasets.add.credential")}
            value={credentialId}
            onChange={(e) => setCredentialId(e.target.value)}
          >
            <option value="">{t("datasets.add.anonymous")}</option>
            {credentials.data?.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </SelectField>
          <form
            className="flex items-end gap-2"
            onSubmit={(e) => {
              e.preventDefault();
              void search();
            }}
          >
            <TextField
              id="query"
              label={t("datasets.add.query")}
              value={query}
              placeholder={t("datasets.add.queryPlaceholder")}
              onChange={(e) => setQuery(e.target.value)}
            />
            <Button type="submit" disabled={busy || !query.trim()}>
              {t("datasets.add.search")}
            </Button>
          </form>
        </div>
        <ErrorAlert error={error} />
        {found && found.length === 0 && <EmptyNote>{t("datasets.add.notFound")}</EmptyNote>}
        {found && found.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {found.map((item) => {
              const picked = target?.name === item.name && target.namespace === item.namespace;
              return (
                <Button
                  key={`${item.namespace}/${item.name}`}
                  size="sm"
                  variant={picked ? "default" : "outline"}
                  onClick={() => setTarget(item)}
                >
                  {item.namespace}/{item.name}
                  {item.visibility && item.visibility !== "public" && ` · ${item.visibility}`}
                </Button>
              );
            })}
          </div>
        )}
        {target && (
          <SourceVersions
            key={`${sourceKind}/${target.namespace}/${target.name}/${credentialId}`}
            target={{
              source_kind: sourceKind,
              namespace: target.namespace,
              name: target.name,
              credential_id: credentialId || null,
            }}
            local={existing?.versions ?? []}
            onImported={onImported}
          />
        )}
      </CardContent>
    </Card>
  );
}

/** "检查上游更新" on an existing dataset: its source, credential and versions. */
function UpstreamVersions({
  dataset,
  onImported,
  onClose,
}: {
  dataset: Dataset;
  onImported: (versionId?: string) => Promise<void>;
  onClose: () => void;
}) {
  return (
    <Card className="mb-4">
      <CardHeader>
        <CardTitle className="flex items-center justify-between gap-2 text-base">
          {t("datasets.upstream.title", { name: `${dataset.namespace}/${dataset.name}` })}
          <Button size="sm" variant="ghost" onClick={onClose}>
            {t("datasets.collapse")}
          </Button>
        </CardTitle>
        <p className="text-xs text-muted-foreground">
          {t("datasets.upstream.intro")}
        </p>
      </CardHeader>
      <CardContent>
        <SourceVersions
          target={{
            source_kind: dataset.source_kind,
            namespace: dataset.namespace,
            name: dataset.name,
            credential_id: dataset.credential_id,
          }}
          local={dataset.versions}
          onImported={onImported}
        />
      </CardContent>
    </Card>
  );
}

function VersionTasks({
  versionId,
  isAdmin,
  onChange,
}: {
  versionId: string;
  isAdmin: boolean;
  onChange: () => Promise<void>;
}) {
  const detail = useLoad(
    async () =>
      (await api.get("/dataset-versions/{version_id}", { path: { version_id: versionId } })).data,
    [versionId],
  );
  const tasks = useLoad(
    async () =>
      (await api.get("/dataset-versions/{version_id}/tasks", { path: { version_id: versionId } }))
        .data,
    [versionId],
  );
  const taskPrefix = commonPrefix(tasks.data?.map((t) => t.task_key) ?? []);
  const [open, setOpen] = useState<string>();
  const [operations, setOperations] = useState<{ id: string; label: string }[]>([]);
  const restoring = tasks.data?.some((t) => t.local_preparation.cache_state === "restoring");
  useInterval(() => void tasks.reload(), 2000, !!restoring);
  const firstOp = detail.data?.first_preparation_operation_id;
  const preparable = tasks.data?.filter(
    (t) => t.admission && ["pending", "first_failed"].includes(t.admission.state),
  );

  const refresh = async () => {
    await Promise.all([detail.reload(), tasks.reload(), onChange()]);
  };
  const act = async (label: string, call: () => Promise<{ data: Operation }>) => {
    try {
      const op = await call();
      setOperations((list) => [...list, { id: op.data.id, label }]);
    } catch (err) {
      toast.error(errorText(err));
    }
  };
  const revoke = async (task: Task) => {
    const reason = window.prompt(t("datasets.tasks.revokePrompt", { task: task.task_key }));
    if (!reason || !task.admission) return;
    try {
      await api.post("/admin/task-admissions/{admission_id}/revoke", {
        path: { admission_id: task.admission.id },
        body: {
          reason,
          expected_revision: task.admission.revision,
        },
      });
      toast.success(t("datasets.tasks.revoked"));
      await refresh();
    } catch (err) {
      toast.error(errorText(err));
    }
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          {detail.data
            ? `${detail.data.namespace}/${detail.data.name}@${detail.data.upstream_ref}`
            : t("datasets.tasks.title")}
        </CardTitle>
        {detail.data && (
          <div className="grid gap-1 text-xs text-muted-foreground">
            <div>
              <Trans
                i18nKey="datasets.tasks.digests"
                values={{
                  upstream: shortId(detail.data.resolved_digest, 23),
                  material: shortId(detail.data.materialization_digest, 23),
                  time: fmtTime(detail.data.created_at),
                }}
                components={{
                  upstream: <code title={detail.data.resolved_digest} />,
                  material: <code title={detail.data.materialization_digest ?? ""} />,
                }}
              />
            </div>
            <div className="text-sm text-foreground">
              {t("datasets.tasks.counts", {
                count: detail.data.task_count,
                admitted: detail.data.admitted_task_count,
                submittable: detail.data.submittable_task_count,
                ready: detail.data.cache_ready_task_count,
              })}
            </div>
            <ScoringContract
              versionId={versionId}
              contract={detail.data.scoring_contract}
              editable={isAdmin && !detail.data.deletion}
              onSaved={() => void detail.reload()}
            />
            {detail.data.deletion && (
              <div className="text-sm text-amber-800">
                {t("datasets.tasks.deleted", {
                  state:
                    detail.data.deletion.state === "purged"
                      ? t("datasets.tasks.purged")
                      : t("datasets.tasks.inRecycleBin"),
                  time: fmtTime(detail.data.deletion.deleted_at),
                })}
              </div>
            )}
          </div>
        )}
        {isAdmin && preparable && preparable.length > 0 && !firstOp && (
          <div>
            <Button
              size="sm"
              onClick={() =>
                void act(t("datasets.tasks.firstPreparation"), () =>
                  api.post("/admin/dataset-versions/{version_id}/preparations", {
                    path: { version_id: versionId },
                    body: {},
                  }),
                )
              }
            >
              {preparable.some((t) => t.admission?.state === "first_failed")
                ? t("datasets.tasks.retry", { count: preparable.length })
                : t("datasets.tasks.prepare", { count: preparable.length })}
            </Button>
          </div>
        )}
        {firstOp && <OperationLine
            id={firstOp}
            label={t("datasets.tasks.firstPreparation")}
            onDone={() => void refresh()}
          />}
        {operations.map((op) => (
          <OperationLine
            key={op.id}
            id={op.id}
            label={op.label}
            onDone={() => {
              setOperations((list) => list.filter((x) => x.id !== op.id));
              void refresh();
            }}
          />
        ))}
      </CardHeader>
      <CardContent>
        <ErrorAlert error={detail.error ?? tasks.error} />
        {tasks.loading && !tasks.data && <Loading />}
        {tasks.data && tasks.data.length === 0 && <EmptyNote>{t("datasets.tasks.empty")}</EmptyNote>}
        {tasks.data && tasks.data.length > 0 && (
          <div className="rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t("datasets.tasks.columns.task")}</TableHead>
                  <TableHead>{t("datasets.tasks.columns.admission")}</TableHead>
                  <TableHead>{t("datasets.tasks.columns.content")}</TableHead>
                  <TableHead>{t("datasets.tasks.columns.cache")}</TableHead>
                  <TableHead>{t("datasets.tasks.columns.submittable")}</TableHead>
                  <TableHead>{t("datasets.tasks.columns.notes")}</TableHead>
                  {isAdmin && <TableHead />}
                </TableRow>
              </TableHeader>
              <TableBody>
                {tasks.data.map((task) => (
                  <Fragment key={task.id}>
                    <TableRow>
                      <TableCell className="max-w-[24rem] font-mono text-xs">
                        <button
                          type="button"
                          className="flex max-w-full text-left underline-offset-2 hover:underline"
                          onClick={() => setOpen(open === task.id ? undefined : task.id)}
                        >
                          <LongName value={task.task_key} prefix={taskPrefix} copy={false} />
                        </button>
                      </TableCell>
                      <TaskStatusCells task={task} />
                      {isAdmin && (
                        <TableCell className="whitespace-nowrap">
                          {task.local_preparation.cache_state === "ready" &&
                            task.local_preparation.id && (
                              <Button
                                size="sm"
                                variant="ghost"
                                title={t("datasets.tasks.evictTitle")}
                                onClick={() => {
                                  const preparationId = task.local_preparation.id;
                                  if (!preparationId) return;
                                  void act(t("datasets.tasks.evictOperation", { task: task.task_key }), () =>
                                    api.post("/admin/preparations/{preparation_id}/evict", {
                                      path: { preparation_id: preparationId },
                                    }),
                                  );
                                }}
                              >
                                {t("datasets.tasks.evict")}
                              </Button>
                            )}
                          {task.admission?.state === "admitted" && (
                            <Button size="sm" variant="ghost" onClick={() => void revoke(task)}>
                              {t("datasets.tasks.revoke")}
                            </Button>
                          )}
                        </TableCell>
                      )}
                    </TableRow>
                    {open === task.id && (
                      <TableRow>
                        <TableCell colSpan={isAdmin ? 7 : 6} className="bg-muted/40">
                          <TaskDetail task={task} />
                        </TableCell>
                      </TableRow>
                    )}
                  </Fragment>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function ScoringContract({
  versionId,
  contract,
  editable,
  onSaved,
}: {
  versionId: string;
  contract: DatasetVersionDetail["scoring_contract"];
  editable: boolean;
  onSaved: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [direction, setDirection] = useState(contract.direction);
  const [threshold, setThreshold] = useState(String(contract.passed?.threshold ?? ""));
  const [error, setError] = useState<unknown>();
  const save = async () => {
    setError(undefined);
    try {
      await api.put("/admin/dataset-versions/{version_id}/scoring-contract", {
        path: { version_id: versionId },
        body: {
          name: contract.name,
          direction,
          pass_threshold: threshold.trim() === "" ? null : Number(threshold),
        },
      });
      toast.success(t("datasets.scoring.saved"));
      setEditing(false);
      onSaved();
    } catch (err) {
      setError(err);
    }
  };
  return (
    <div className="grid gap-1">
      <div>
        {t("datasets.scoring.summary", {
          name: contract.name,
          direction:
            contract.direction === "higher_is_better"
              ? t("datasets.scoring.higherIsBetter")
              : t("datasets.scoring.lowerIsBetter"),
          passed: contract.passed
            ? t("datasets.scoring.passAt", { threshold: contract.passed.threshold })
            : t("datasets.scoring.noPassRate"),
          source:
            contract.source === "dataset_version"
              ? t("datasets.scoring.confirmed")
              : t("datasets.scoring.platformDefault"),
        })}
        {editable && !editing && (
          <Button size="sm" variant="ghost" onClick={() => setEditing(true)}>
            {t("datasets.scoring.edit")}
          </Button>
        )}
      </div>
      {editing && (
        <div className="flex flex-wrap items-end gap-2">
          <SelectField
            id="direction"
            label={t("datasets.scoring.direction")}
            value={direction}
            onChange={(e) => setDirection(e.target.value as typeof direction)}
          >
            <option value="higher_is_better">{t("datasets.scoring.higherIsBetter")}</option>
            <option value="lower_is_better">{t("datasets.scoring.lowerIsBetter")}</option>
          </SelectField>
          <TextField
            id="threshold"
            label={t("datasets.scoring.threshold")}
            value={threshold}
            onChange={(e) => setThreshold(e.target.value)}
          />
          <Button size="sm" onClick={() => void save()}>
            {t("datasets.scoring.save")}
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
            {t("datasets.scoring.cancel")}
          </Button>
          <ErrorAlert error={error} />
        </div>
      )}
    </div>
  );
}

function TaskDetail({ task }: { task: Task }) {
  const resources = task.resources;
  const timeouts = task.timeouts;
  return (
    <div className="grid gap-3 text-xs">
      <div className="grid gap-1">
        <div>
          {t("datasets.detail.resources", {
            cpus: String(resources.cpus ?? "—"),
            memory: String(resources.memory_mb ?? "—"),
            storage:
              resources.storage_mb != null
                ? t("datasets.detail.storage", { storage: String(resources.storage_mb) })
                : "",
          })}
        </div>
        {resources.environments.map((e) => (
          <div key={e.role}>
            {t("datasets.detail.environment", {
              role: e.role === "verifier" ? t("datasets.detail.verifier") : e.role,
              cpus: String(e.cpus ?? t("datasets.detail.teamDefault")),
              memory:
                e.memory_mb == null ? t("datasets.detail.teamDefault") : `${String(e.memory_mb)} MB`,
            })}
          </div>
        ))}
        <div>
          {t("datasets.detail.timeouts", {
            agent: String(timeouts.agent_sec ?? "—"),
            verifier: String(timeouts.verifier_sec ?? "—"),
            build: String(timeouts.build_sec ?? "—"),
          })}
        </div>
        {task.content.resources.map((r) => (
          <div key={r.resource_kind}>
            <Trans
              i18nKey="datasets.detail.resource"
              values={{
                kind:
                  r.resource_kind === "task_content"
                    ? t("datasets.detail.taskContent")
                    : t("datasets.detail.environmentImage"),
                digest: r.digest,
                availability: r.availability,
                time: fmtTime(r.checked_at),
              }}
              components={{ code: <code /> }}
            />
          </div>
        ))}
        {task.content.material_digest && (
          <div>
            <Trans
              i18nKey="datasets.detail.material"
              values={{ digest: task.content.material_digest }}
              components={{ code: <code /> }}
            />
          </div>
        )}
        {task.spec_digest && (
          <div>
            <Trans
              i18nKey="datasets.detail.spec"
              values={{ digest: task.spec_digest }}
              components={{ code: <code /> }}
            />
          </div>
        )}
        {task.local_preparation.setup_network_revision != null && (
          <SetupNetworkRevision
            revision={task.local_preparation.setup_network_revision}
            prefix={t("datasets.detail.setupNetwork")}
          />
        )}
      </div>
      <div>
        <div className="mb-1 font-medium">{t("datasets.detail.parameters")}</div>
        {task.parameters.length === 0 ? (
          <span className="text-muted-foreground">{t("datasets.detail.none")}</span>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t("datasets.detail.columns.name")}</TableHead>
                <TableHead>{t("datasets.detail.columns.default")}</TableHead>
                <TableHead>{t("datasets.detail.columns.required")}</TableHead>
                <TableHead>{t("datasets.detail.columns.usedIn")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {task.parameters.map((p) => (
                <TableRow key={p.name}>
                  <TableCell className="font-mono">
                    {p.name} {p.sensitive && <Pill tone="warn">{t("datasets.detail.sensitive")}</Pill>}
                  </TableCell>
                  <TableCell className="font-mono">{p.default ?? "—"}</TableCell>
                  <TableCell>{p.required ? t("datasets.detail.yes") : t("datasets.detail.no")}</TableCell>
                  <TableCell>{p.used_in.join(", ")}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </div>
      {task.instruction && (
        <details>
          <summary className="cursor-pointer">{t("datasets.detail.instruction")}</summary>
          <pre className="mt-1 max-h-60 overflow-auto whitespace-pre-wrap rounded border bg-background p-2">
            {task.instruction}
          </pre>
        </details>
      )}
    </div>
  );
}
