import { Fragment, useState } from "react";
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
import { fmtTime, OPERATION_STATUS, shortId } from "@/lib/format";
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
  const [chosen, setVersionId] = useState<string>("");
  const visible = datasets.data?.some((d) => d.versions.some((v) => v.id === chosen));
  const versionId =
    (visible ? chosen : "") ||
    datasets.data?.find((d) => d.versions.length > 0)?.versions[0].id ||
    "";

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
      {isAdmin && (
        <AddDataset
          onImported={async (id) => {
            await datasets.reload();
            if (id) setVersionId(id);
          }}
        />
      )}
      <ErrorAlert error={datasets.error} />
      {datasets.loading && !datasets.data && <Loading />}
      {datasets.data && datasets.data.length === 0 && <EmptyNote>还没有数据集。</EmptyNote>}
      <div className="grid gap-4 lg:grid-cols-[minmax(260px,1fr)_3fr]">
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
          <VersionTasks
            key={versionId}
            versionId={versionId}
            isAdmin={isAdmin}
            onChange={datasets.reload}
          />
        )}
      </div>
    </div>
  );
}

function AddDataset({ onImported }: { onImported: (versionId?: string) => Promise<void> }) {
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
  const [versions, setVersions] = useState<SourceVersion[]>();
  const [preview, setPreview] = useState<SourceVersionDetail>();
  const [prepare, setPrepare] = useState(true);
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const [operation, setOperation] = useState<string>();
  const sourceKind = kind || sources.data?.[0]?.kind || "hub";
  const common = { source_kind: sourceKind, credential_id: credentialId || undefined };

  const run = async (work: () => Promise<void>) => {
    setBusy(true);
    setError(undefined);
    try {
      await work();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  const search = () =>
    run(async () => {
      setTarget(undefined);
      setVersions(undefined);
      setPreview(undefined);
      setFound((await api.get("/dataset-search", { query: { q: query, ...common } })).data);
    });
  const pick = (item: SourceDataset) =>
    run(async () => {
      setTarget(item);
      setPreview(undefined);
      const query = { namespace: item.namespace, name: item.name, ...common };
      setVersions((await api.get("/dataset-search/versions", { query: query })).data);
    });
  const look = (ref: string) =>
    run(async () => {
      if (!target) return;
      const query = { namespace: target.namespace, name: target.name, ref, ...common };
      setPreview((await api.get("/dataset-search/version", { query: query })).data);
    });
  const add = (accept = false) =>
    run(async () => {
      if (!preview) return;
      const op = await api.post("/admin/dataset-imports", {
        body: {
          source_kind: sourceKind,
          namespace: preview.namespace,
          name: preview.name,
          ref: preview.upstream_ref,
          credential_id: credentialId || null,
          prepare,
          accept_changed_content: accept,
        },
      });
      setOperation(op.data.id);
    });

  if (!open) {
    return (
      <div className="mb-4">
        <Button variant="outline" onClick={() => setOpen(true)}>
          从数据源添加版本
        </Button>
      </div>
    );
  }
  return (
    <Card className="mb-4">
      <CardHeader>
        <CardTitle className="text-base">从数据源添加版本</CardTitle>
        <p className="text-xs text-muted-foreground">
          搜索只读取外部描述；添加时引用固定为确切摘要，任务材料下载并校验后登记，再按需进行首次准备。
        </p>
      </CardHeader>
      <CardContent className="grid gap-3">
        <div className="grid gap-3 md:grid-cols-[160px_1fr_1fr]">
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
                  onClick={() => void pick(item)}
                >
                  {item.namespace}/{item.name}
                  {item.visibility && item.visibility !== "public" && ` · ${item.visibility}`}
                </Button>
              );
            })}
          </div>
        )}
        {versions && (
          <div className="rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>修订</TableHead>
                  <TableHead>标签</TableHead>
                  <TableHead>摘要</TableHead>
                  <TableHead>发布</TableHead>
                  <TableHead />
                </TableRow>
              </TableHeader>
              <TableBody>
                {versions.map((v) => (
                  <TableRow key={v.ref}>
                    <TableCell>{v.ref}</TableCell>
                    <TableCell className="text-xs">{v.tags.join(", ") || "—"}</TableCell>
                    <TableCell className="font-mono text-xs" title={v.resolved_digest}>
                      {shortId(v.resolved_digest, 19)}
                    </TableCell>
                    <TableCell className="text-xs">
                      {fmtTime(v.published_at)} {v.yanked && <Pill tone="warn">已撤回</Pill>}
                    </TableCell>
                    <TableCell>
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={busy}
                        onClick={() => void look(v.ref)}
                      >
                        查看
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
        {preview && (
          <div className="grid gap-2 rounded-md border p-3 text-sm">
            <div>
              {preview.namespace}/{preview.name}@{preview.upstream_ref}（{preview.ref_kind}）→{" "}
              <code title={preview.resolved_digest}>{shortId(preview.resolved_digest, 23)}</code> ·{" "}
              {preview.tasks.length} 个任务
              {preview.existing_version_id && <Pill tone="info">已添加过此内容</Pill>}
            </div>
            {preview.description && <p className="text-muted-foreground">{preview.description}</p>}
            <div className="max-h-40 overflow-auto font-mono text-xs text-muted-foreground">
              {preview.tasks.map((t) => (
                <div key={t.task_key}>
                  {t.task_key} {shortId(t.content_digest, 19)}
                </div>
              ))}
            </div>
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={prepare}
                onChange={(e) => setPrepare(e.target.checked)}
              />
              添加后立即进行首次准备（构建或拉取环境并保存原件）
            </label>
            <div className="flex gap-2">
              <Button disabled={busy || !!operation} onClick={() => void add()}>
                添加
              </Button>
              <Button variant="ghost" onClick={() => setOpen(false)}>
                收起
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
                void onImported(imported?.dataset_version_id);
              } else if (op.error?.code === "CONTENT_REFERENCE_CHANGED") {
                const ok = window.confirm(
                  "此引用现在指向不同的内容。作为新的内部版本添加？旧版本保持不变。",
                );
                if (ok) void add(true);
              } else {
                toast.error(`添加失败：${op.error?.message ?? op.status}`);
              }
            }}
          />
        )}
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
                      <TableCell className="font-mono text-xs">
                        <button
                          type="button"
                          className="text-left underline-offset-2 hover:underline"
                          onClick={() => setOpen(open === task.id ? undefined : task.id)}
                        >
                          {task.task_key}
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
