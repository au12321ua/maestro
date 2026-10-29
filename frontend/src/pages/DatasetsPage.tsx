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

const PHASES: Record<string, string> = {
  queued: "排队",
  resolving: "解析引用",
  downloading: "下载任务",
  recording: "登记版本",
  preparing: "准备环境",
  building: "构建镜像",
  pulling: "拉取镜像",
  loading: "载入原件",
  exporting: "导出原件",
  checking: "校验",
  done: "完成",
};

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
          {progress.tasks_done ?? 0}/{progress.tasks_total} 个任务
          {prepared?.admitted != null && ` · 准入 ${prepared.admitted}`}
          {prepared?.failed ? ` · 失败 ${prepared.failed}` : ""}
        </span>
      )}
      {op.data.error && (
        <span className="text-xs text-destructive">
          {op.data.error.message ?? op.data.error.code}（{op.data.error.code}）
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
        `删除数据集「${dataset.namespace}/${dataset.name}」及其全部版本？它会移入回收站；已有运行记录保留，但不能再用它提交或重跑。`,
      )
    )
      return;
    try {
      await api.del("/admin/datasets/{dataset_id}", { path: { dataset_id: dataset.id } });
      toast.success("已移入回收站");
    } catch (err) {
      toast.error(`删除失败：${errorText(err)}`);
    }
    await datasets.reload();
  };

  const removeVersion = async (dataset: Dataset, version: DatasetVersion) => {
    if (
      !window.confirm(
        `删除版本「${dataset.name} ${version.upstream_ref}」？它会移入回收站；已有运行记录保留，但不能再用它提交或重跑。`,
      )
    )
      return;
    try {
      await api.del("/admin/dataset-versions/{version_id}", { path: { version_id: version.id } });
      toast.success("已移入回收站");
    } catch (err) {
      toast.error(`删除失败：${errorText(err)}`);
    }
    await datasets.reload();
  };

  return (
    <div>
      <PageTitle title="数据集" />
      <p className="mb-4 text-sm text-muted-foreground">
        准入、内容可得性与本机缓存分开显示：缓存被回收的已准入任务仍可提交，运行时按原摘要自动恢复。
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
      {datasets.data && datasets.data.length === 0 && <EmptyNote>还没有数据集。</EmptyNote>}
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
                      title="删除数据集"
                      aria-label={`删除数据集 ${dataset.name}`}
                      onClick={() => void removeDataset(dataset)}
                    >
                      <Trash2 />
                    </Button>
                  )}
                </CardTitle>
                <div className="text-xs text-muted-foreground">
                  来源：{dataset.source_kind}
                  {dataset.credential_id ? " · 使用团队凭据" : " · 匿名读取"}
                  {dataset.description && ` · ${dataset.description}`}
                </div>
                {isAdmin && (
                  <div>
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => setUpstreamOf(dataset.id)}
                    >
                      检查上游更新
                    </Button>
                  </div>
                )}
              </CardHeader>
              <CardContent className="grid gap-1">
                {dataset.versions.length === 0 && (
                  <span className="text-xs text-muted-foreground">无版本</span>
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
                        可提交 {version.submittable_task_count}/{version.task_count} · 缓存就绪{" "}
                        {version.cache_ready_task_count}
                      </span>
                    </button>
                    {isAdmin && (
                      <Button
                        size="sm"
                        variant="ghost"
                        title="删除该版本"
                        aria-label={`删除版本 ${version.upstream_ref}`}
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
                这个版本不在数据集列表中：它已删除或不存在；已删除的版本仍可查看，但不能再用来提交。
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
  const groups: [string, string[]][] = [
    ["新增", added],
    ["删除", removed],
    ["内容变化", changed],
  ];
  return (
    <div className="grid gap-1">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <label htmlFor="diff-base" className="shrink-0 text-muted-foreground">
          与本地版本比较
        </label>
        <select
          id="diff-base"
          className="h-8 min-w-0 max-w-full rounded-md border border-input bg-background px-2 text-sm"
          value={baseId}
          onChange={(e) => setBaseId(e.target.value)}
        >
          {newestFirst(local).map((v) => (
            <option key={v.id} value={v.id}>
              {v.upstream_ref}（{fmtTime(v.created_at)} 导入）
            </option>
          ))}
        </select>
      </div>
      <ErrorAlert error={base.error} />
      {base.loading && !base.data && <Loading />}
      {base.data && (
        <div className="grid gap-1 text-xs">
          <div>
            {groups.map(([title, keys]) => `${title} ${keys.length}`).join(" · ")} · 未变{" "}
            {preview.tasks.length - added.length - changed.length}
          </div>
          {groups
            .filter(([, keys]) => keys.length > 0)
            .map(([title, keys]) => (
              <details key={title}>
                <summary className="cursor-pointer">
                  {title}的任务（{keys.length}）
                </summary>
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
    window.confirm(`标签「${tag}」已指向新内容，将作为新版本添加；旧版本保留，已有运行不受影响。`);

  return (
    <div className="grid gap-3">
      <ErrorAlert error={upstream.error} title="无法读取上游版本" />
      {upstream.loading && !upstream.data && <Loading text="读取上游版本…" />}
      {rows && rows.length === 0 && (
        <EmptyNote>数据源没有列出版本；可在下方按标签、修订或摘要添加。</EmptyNote>
      )}
      {rows && rows.length > 0 && (
        <div className="rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>修订</TableHead>
                <TableHead>标签</TableHead>
                <TableHead>摘要</TableHead>
                <TableHead>发布</TableHead>
                <TableHead>平台中</TableHead>
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
                    {fmtTime(v.published_at)} {v.yanked && <Pill tone="warn">已撤回</Pill>}
                  </TableCell>
                  <TableCell className="text-xs">
                    {imported ? (
                      <Link
                        className="text-primary underline"
                        to={`/datasets?version=${imported.id}`}
                      >
                        已导入（{imported.upstream_ref}）
                      </Link>
                    ) : (
                      <Pill tone="info">未导入</Pill>
                    )}
                    {movedTags.map(({ tag, from }) => (
                      <div key={tag} className="mt-1">
                        <Pill tone="warn" title={`本地版本 ${shortId(from.resolved_digest, 19)}`}>
                          标签 {tag} 已移动
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
                        添加为新版本…
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
          label="添加指定版本"
          value={ref}
          placeholder="标签、修订号或 sha256 摘要"
          onChange={(e) => setRef(e.target.value)}
        />
        <Button type="submit" variant="outline" disabled={busy || !ref.trim()}>
          查看
        </Button>
      </form>
      <ErrorAlert error={error} />
      {preview && (
        <div className="grid gap-2 rounded-md border p-3 text-sm">
          <div>
            {preview.namespace}/{preview.name}@{preview.upstream_ref}（{preview.ref_kind}）→{" "}
            <code title={preview.resolved_digest}>{shortId(preview.resolved_digest, 23)}</code> ·{" "}
            {preview.tasks.length} 个任务
            {preview.existing_version_id && <Pill tone="info">已添加过此内容</Pill>}
            {movedFrom && <Pill tone="warn">标签已移动</Pill>}
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
            添加后立即进行首次准备（构建或拉取环境并保存原件）
          </label>
          <div className="flex gap-2">
            <Button
              disabled={busy || !!operation}
              onClick={() => {
                if (movedFrom && !confirmMoved(preview.upstream_ref)) return;
                void add(!!movedFrom);
              }}
            >
              {preview.existing_version_id ? "重新核对" : "添加为新版本"}
            </Button>
          </div>
        </div>
      )}
      {operation && (
        <OperationLine
          id={operation}
          label="添加版本"
          onDone={(op) => {
            setOperation(undefined);
            const imported = op.kind === "dataset_import" ? op.result : null;
            if (op.status === "succeeded") {
              toast.success(imported?.existing ? "该内容已登记，未重复创建" : "版本已添加");
              setPreview(undefined);
              void upstream.reload();
              void onImported(imported?.dataset_version_id);
            } else if (op.error?.code === "CONTENT_REFERENCE_CHANGED" && preview) {
              if (confirmMoved(preview.upstream_ref)) void add(true);
            } else {
              toast.error(`添加失败：${op.error?.message ?? op.status}`);
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
          添加数据集
        </Button>
      </div>
    );
  }
  return (
    <Card className="mb-4">
      <CardHeader>
        <CardTitle className="flex items-center justify-between gap-2 text-base">
          添加数据集
          <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>
            收起
          </Button>
        </CardTitle>
        <p className="text-xs text-muted-foreground">
          搜索只读取外部描述；添加时引用固定为确切摘要，任务材料下载并校验后登记，再按需进行首次准备。已有数据集的新版本可以直接在它的卡片上“检查上游更新”。
        </p>
      </CardHeader>
      <CardContent className="grid gap-3">
        <div className="grid gap-3 md:grid-cols-[160px_minmax(0,1fr)_minmax(0,1fr)]">
          <SelectField
            id="source"
            label="来源"
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
            label="来源凭据"
            value={credentialId}
            onChange={(e) => setCredentialId(e.target.value)}
          >
            <option value="">匿名（公开数据）</option>
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
              label="作者 / 名称"
              value={query}
              placeholder="org/name 或关键字"
              onChange={(e) => setQuery(e.target.value)}
            />
            <Button type="submit" disabled={busy || !query.trim()}>
              搜索
            </Button>
          </form>
        </div>
        <ErrorAlert error={error} />
        {found && found.length === 0 && <EmptyNote>没有找到数据集。</EmptyNote>}
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
          {dataset.namespace}/{dataset.name} 的上游版本
          <Button size="sm" variant="ghost" onClick={onClose}>
            收起
          </Button>
        </CardTitle>
        <p className="text-xs text-muted-foreground">
          按内容摘要对照平台中的版本。添加新版本不影响已有版本和引用它们的运行；结果只在此显示，不保存。
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
    const reason = window.prompt(`撤销 ${task.task_key} 的准入原因（已受理的运行不受影响）`);
    if (!reason || !task.admission) return;
    try {
      await api.post("/admin/task-admissions/{admission_id}/revoke", {
        path: { admission_id: task.admission.id },
        body: {
          reason,
          expected_revision: task.admission.revision,
        },
      });
      toast.success("已撤销准入");
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
            : "版本任务"}
        </CardTitle>
        {detail.data && (
          <div className="grid gap-1 text-xs text-muted-foreground">
            <div>
              上游摘要{" "}
              <code title={detail.data.resolved_digest}>
                {shortId(detail.data.resolved_digest, 23)}
              </code>{" "}
              · 本地材料摘要{" "}
              <code title={detail.data.materialization_digest ?? ""}>
                {shortId(detail.data.materialization_digest, 23)}
              </code>{" "}
              · 登记于 {fmtTime(detail.data.created_at)}
            </div>
            <div className="text-sm text-foreground">
              共 {detail.data.task_count} 个任务 · 已准入 {detail.data.admitted_task_count} · 可提交{" "}
              {detail.data.submittable_task_count} · 缓存就绪 {detail.data.cache_ready_task_count}
            </div>
            <ScoringContract
              versionId={versionId}
              contract={detail.data.scoring_contract}
              editable={isAdmin && !detail.data.deletion}
              onSaved={() => void detail.reload()}
            />
            {detail.data.deletion && (
              <div className="text-sm text-amber-800">
                该版本已删除（
                {detail.data.deletion.state === "purged" ? "已彻底删除" : "在回收站中"}，
                {fmtTime(detail.data.deletion.deleted_at)}）。
              </div>
            )}
          </div>
        )}
        {isAdmin && preparable && preparable.length > 0 && !firstOp && (
          <div>
            <Button
              size="sm"
              onClick={() =>
                void act("首次准备", () =>
                  api.post("/admin/dataset-versions/{version_id}/preparations", {
                    path: { version_id: versionId },
                    body: {},
                  }),
                )
              }
            >
              {preparable.some((t) => t.admission?.state === "first_failed")
                ? `重试未准入的 ${preparable.length} 个任务`
                : `首次准备 ${preparable.length} 个任务`}
            </Button>
          </div>
        )}
        {firstOp && <OperationLine id={firstOp} label="首次准备" onDone={() => void refresh()} />}
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
        {tasks.data && tasks.data.length === 0 && <EmptyNote>该版本没有任务。</EmptyNote>}
        {tasks.data && tasks.data.length > 0 && (
          <div className="rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>任务</TableHead>
                  <TableHead>准入</TableHead>
                  <TableHead>内容</TableHead>
                  <TableHead>本机缓存</TableHead>
                  <TableHead>可提交</TableHead>
                  <TableHead>说明</TableHead>
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
                                title="删除本机镜像；准入与原件保留"
                                onClick={() => {
                                  const preparationId = task.local_preparation.id;
                                  if (!preparationId) return;
                                  void act(`回收 ${task.task_key} 的缓存`, () =>
                                    api.post("/admin/preparations/{preparation_id}/evict", {
                                      path: { preparation_id: preparationId },
                                    }),
                                  );
                                }}
                              >
                                回收缓存
                              </Button>
                            )}
                          {task.admission?.state === "admitted" && (
                            <Button size="sm" variant="ghost" onClick={() => void revoke(task)}>
                              撤销准入
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
      toast.success("评分规则已确认，之后提交的运行使用它");
      setEditing(false);
      onSaved();
    } catch (err) {
      setError(err);
    }
  };
  return (
    <div className="grid gap-1">
      <div>
        评分规则：{contract.name}，
        {contract.direction === "higher_is_better" ? "越高越好" : "越低越好"}，
        {contract.passed ? `达到 ${contract.passed.threshold} 记为通过` : "不计通过率"}（
        {contract.source === "dataset_version" ? "管理员已确认" : "平台默认"}）
        {editable && !editing && (
          <Button size="sm" variant="ghost" onClick={() => setEditing(true)}>
            确认 / 修改
          </Button>
        )}
      </div>
      {editing && (
        <div className="flex flex-wrap items-end gap-2">
          <SelectField
            id="direction"
            label="方向"
            value={direction}
            onChange={(e) => setDirection(e.target.value as typeof direction)}
          >
            <option value="higher_is_better">越高越好</option>
            <option value="lower_is_better">越低越好</option>
          </SelectField>
          <TextField
            id="threshold"
            label="通过阈值（留空则不计通过率）"
            value={threshold}
            onChange={(e) => setThreshold(e.target.value)}
          />
          <Button size="sm" onClick={() => void save()}>
            保存
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setEditing(false)}>
            取消
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
          资源建议：CPU {String(resources.cpus ?? "—")} · 内存 {String(resources.memory_mb ?? "—")}{" "}
          MB
          {resources.storage_mb != null && ` · 磁盘 ${String(resources.storage_mb)} MB`}
          （作者建议值，约束强度未知）
        </div>
        {resources.environments.map((e) => (
          <div key={e.role}>
            {e.role === "verifier" ? "独立验证器" : e.role}：CPU{" "}
            {String(e.cpus ?? "团队默认")} · 内存{" "}
            {e.memory_mb == null ? "团队默认" : `${String(e.memory_mb)} MB`}
          </div>
        ))}
        <div>
          时限：Agent {String(timeouts.agent_sec ?? "—")} 秒 · 验证{" "}
          {String(timeouts.verifier_sec ?? "—")} 秒 · 构建 {String(timeouts.build_sec ?? "—")} 秒
        </div>
        {task.content.resources.map((r) => (
          <div key={r.resource_kind}>
            {r.resource_kind === "task_content" ? "任务内容" : "环境镜像"}：<code>{r.digest}</code>（
            {r.availability}，{fmtTime(r.checked_at)}）
          </div>
        ))}
        {task.content.material_digest && (
          <div>
            本地材料：<code>{task.content.material_digest}</code>
          </div>
        )}
        {task.spec_digest && (
          <div>
            准备规格：<code>{task.spec_digest}</code>
          </div>
        )}
        {task.local_preparation.setup_network_revision != null && (
          <SetupNetworkRevision
            revision={task.local_preparation.setup_network_revision}
            prefix="镜像构建时用的安装网络："
          />
        )}
      </div>
      <div>
        <div className="mb-1 font-medium">作者参数</div>
        {task.parameters.length === 0 ? (
          <span className="text-muted-foreground">无</span>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>名称</TableHead>
                <TableHead>默认值</TableHead>
                <TableHead>必填</TableHead>
                <TableHead>出现位置</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {task.parameters.map((p) => (
                <TableRow key={p.name}>
                  <TableCell className="font-mono">
                    {p.name} {p.sensitive && <Pill tone="warn">敏感</Pill>}
                  </TableCell>
                  <TableCell className="font-mono">{p.default ?? "—"}</TableCell>
                  <TableCell>{p.required ? "是" : "否"}</TableCell>
                  <TableCell>{p.used_in.join(", ")}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </div>
      {task.instruction && (
        <details>
          <summary className="cursor-pointer">任务说明</summary>
          <pre className="mt-1 max-h-60 overflow-auto whitespace-pre-wrap rounded border bg-background p-2">
            {task.instruction}
          </pre>
        </details>
      )}
    </div>
  );
}
