import { useId, useState, type FormEvent, type ReactNode } from "react";
import { Link, useHref, useNavigate, useParams } from "react-router-dom";
import { ChevronRight, Copy, Database, History, Library, Paperclip, Pencil, Play, Plus, RefreshCw, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Textarea } from "@/components/ui/textarea";
import {
  EmptyNote, ErrorAlert, Loading, PageTitle, Pill, ScoreCoverage, SelectField,
  StatusPill, TextField,
} from "@/components/common";
import { Markdown } from "@/components/Markdown";
import { DirectoryCheck, MoreItems, useDirectorySelection } from "@/components/TopicDirectory";
import { RecordEvidence, WorkflowRecords } from "@/components/WorkflowRecords";
import { t } from "@/i18n";
import { datasetPath } from "@/lib/datasets";
import { api, errorText } from "@/lib/api";
import { fmtTime, label, RUN_STATE } from "@/lib/format";
import { useCursorList, useLoad, useSession } from "@/lib/hooks";
import { scopedPath, useTopicScope } from "@/lib/topics";
import type { Topic, TopicMaterial, TopicOverview, TopicReference, TopicReferenceKind, TopicRunSummary } from "@/lib/types";

const referenceKinds = ["run", "collection", "dataset", "attachment_snapshot"] as const;
const materialKinds = ["dataset_version", "attachment_snapshot"] as const;

export function TopicsPage() {
  const [q, setQuery] = useState("");
  const [creating, setCreating] = useState(false);
  const { me } = useSession();
  const navigate = useNavigate();
  const page = useCursorList(
    (cursor) => api.get("/topics", { query: { q: q.trim() || undefined, cursor } }),
    q.trim(),
  );
  return (
    <div className="grid gap-4">
      <PageTitle title={t("topics.title")}>
        {(!me.pat_permissions || me.pat_permissions.includes("experiment")) && (
          <Button size="sm" onClick={() => setCreating(true)} disabled={creating}>
            <Plus data-icon="inline-start" />{t("topics.create")}
          </Button>
        )}
      </PageTitle>
      {creating && <TopicForm
        onCancel={() => setCreating(false)}
        onSave={async (body) => {
          const result = await api.post("/topics", { body });
          navigate(result.data.web_path);
        }}
      />}
      <div className="max-w-sm">
        <TextField id="topic-search" label={t("topics.searchTopics")} value={q} onChange={(e) => setQuery(e.target.value)} />
      </div>
      <ErrorAlert error={page.error} />
      {page.loading && !page.data && <Loading />}
      {page.data?.length === 0 && <EmptyNote>{t("topics.empty")}</EmptyNote>}
      {page.data?.length ? (
        <Card className="overflow-hidden">
          <ul className="divide-y">
            {page.data.map((topic) => (
              <li key={topic.id} className="relative flex flex-col gap-1 px-5 py-4 transition-colors hover:bg-muted/50 sm:flex-row sm:items-start sm:justify-between sm:gap-6">
                <div className="grid min-w-0 gap-1">
                  {/* The whole row opens the topic; the link keeps its own name for keyboard and screen readers. */}
                  <Link to={topic.web_path} className="break-words font-medium text-primary after:absolute after:inset-0 hover:underline">
                    {topic.name}
                  </Link>
                  {topic.purpose && <p className="line-clamp-2 whitespace-pre-line text-sm text-muted-foreground">{topic.purpose}</p>}
                </div>
                <div className="grid shrink-0 gap-0.5 text-xs text-muted-foreground sm:text-right">
                  <span>{t("topics.updatedAt", { time: fmtTime(topic.updated_at) })}</span>
                  <code className="break-all">{topic.id}</code>
                </div>
              </li>
            ))}
          </ul>
        </Card>
      ) : null}
      <MoreItems page={page} />
    </div>
  );
}

function TopicForm({ topic, onSave, onCancel }: {
  topic?: Topic;
  onSave: (body: { name: string; purpose: string; settings: Topic["settings"] }) => Promise<void>;
  onCancel: () => void;
}) {
  const id = useId();
  const [name, setName] = useState(topic?.name ?? "");
  const [purpose, setPurpose] = useState(topic?.purpose ?? "");
  const [expanded, setExpanded] = useState(topic?.settings.materials_expanded ?? true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const save = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError(undefined);
    try {
      await onSave({ name: name.trim(), purpose, settings: { materials_expanded: expanded } });
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card>
      <CardHeader><CardTitle className="text-base">{t(topic ? "topics.edit" : "topics.create")}</CardTitle></CardHeader>
      <CardContent>
        <form onSubmit={(e) => void save(e)}>
          <FieldGroup>
            <TextField id={`${id}-name`} label={t("topics.name")} value={name}
              onChange={(e) => setName(e.target.value)} maxLength={128} required />
            <Field>
              <FieldLabel htmlFor={`${id}-purpose`}>{t("topics.purpose")}</FieldLabel>
              <Textarea id={`${id}-purpose`} value={purpose} maxLength={4096} onChange={(e) => setPurpose(e.target.value)} />
            </Field>
            <label className="flex items-center gap-2 text-sm">
              <input type="checkbox" checked={expanded} onChange={(e) => setExpanded(e.target.checked)} />
              {t("topics.expandMaterials")}
            </label>
            <ErrorAlert error={error} />
            <div className="flex flex-wrap gap-2">
              <Button type="submit" size="sm" disabled={busy || !name.trim()}>{t("topics.save")}</Button>
              <Button type="button" size="sm" variant="outline" onClick={onCancel}>{t("topics.cancel")}</Button>
            </div>
          </FieldGroup>
        </form>
      </CardContent>
    </Card>
  );
}

export function TopicPage() {
  const { topicId = "" } = useParams();
  return <TopicView key={topicId} topicId={topicId} />;
}

function TopicView({ topicId }: { topicId: string }) {
  const path = { topic_id: topicId };
  const overview = useLoad(async () => (await api.get("/topics/{topic_id}/overview", { path })).data, [topicId]);
  const navigate = useNavigate();
  const scope = useTopicScope();
  const [editing, setEditing] = useState(false);
  const [showRecords, setShowRecords] = useState(false);
  const url = useHref(`/topics/${topicId}`);
  const data = overview.data;
  const topic = data?.topic;
  const back = { to: "/topics", label: t("topics.title") };
  if (!data || !topic) return (
    <div>
      <PageTitle title={t("topics.overview")} back={back} />
      <ErrorAlert error={overview.error} />
      {overview.loading && <Loading />}
    </div>
  );
  const link = (path: string) => scopedPath(path, topicId);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(new URL(url, window.location.origin).href);
      toast.success(t("topics.copied"));
    } catch {
      toast.error(t("topics.copyFailed"));
    }
  };
  const remove = async () => {
    if (!window.confirm(t("topics.confirmDelete", { name: topic.name }))) return;
    try {
      await api.del("/topics/{topic_id}", { path });
      navigate(scope.topicId === topicId ? "/topics?topic=" : "/topics");
    } catch (err) {
      toast.error(errorText(err));
    }
  };
  const browse = [
    { to: "/runs", label: t("topics.allRuns"), icon: Play },
    { to: "/collections", label: t("topics.allCollections"), icon: Library },
    { to: "/datasets", label: t("topics.allDatasets"), icon: Database },
    { to: "/attachments", label: t("topics.allAttachments"), icon: Paperclip },
  ];

  return (
    <div className="grid gap-4">
      <div>
        <PageTitle title={topic.name} back={back}>
          <Button variant="ghost" size="icon" className="size-8" aria-label={t("topics.copyLink")} title={t("topics.copyLink")} onClick={() => void copy()}>
            <Copy />
          </Button>
          {topic.allowed_actions.includes("delete") && (
            <Button variant="ghost" size="icon" className="size-8 text-muted-foreground hover:text-destructive" aria-label={t("topics.delete")} title={t("topics.delete")} onClick={() => void remove()}>
              <Trash2 />
            </Button>
          )}
          {topic.allowed_actions.includes("edit") && (
            <Button variant="outline" size="sm" onClick={() => setEditing(true)} disabled={editing}>
              <Pencil data-icon="inline-start" />{t("topics.edit")}
            </Button>
          )}
          {topic.allowed_actions.includes("submit") && (
            <Button size="sm" asChild><Link to={link("/runs/new")}><Plus data-icon="inline-start" />{t("topics.newRun")}</Link></Button>
          )}
        </PageTitle>
        <div className="-mt-2 grid gap-2">
          {topic.deletion && <div><Pill tone="warn">{t("topics.recycled")}</Pill></div>}
          {!editing && topic.purpose && (
            <div className="max-w-3xl text-sm text-muted-foreground"><Markdown>{topic.purpose}</Markdown></div>
          )}
          <p className="text-xs text-muted-foreground">{t("topics.updatedAt", { time: fmtTime(topic.updated_at) })}</p>
        </div>
      </div>
      <ErrorAlert error={overview.error} />
      {editing && (
        <TopicForm topic={topic} onCancel={() => setEditing(false)} onSave={async (body) => {
          await api.put("/topics/{topic_id}", { path, body });
          setEditing(false);
          await Promise.all([overview.reload(), scope.topicId === topic.id ? scope.reload() : undefined]);
        }} />
      )}
      <nav aria-label={t("topics.scope")} className="flex flex-wrap items-center gap-1 rounded-lg border bg-card px-2 py-1.5 shadow-sm">
        <span className="px-2 text-xs text-muted-foreground">{t("topics.browseIn")}</span>
        {browse.map(({ to, label: text, icon: Icon }) => (
          <Button key={to} variant="ghost" size="sm" asChild>
            <Link to={link(to)}><Icon data-icon="inline-start" />{text}</Link>
          </Button>
        ))}
        <Button variant="ghost" size="icon" className="ml-auto size-8" aria-label={t("runs.list.refresh")} title={t("runs.list.refresh")}
          disabled={overview.loading} onClick={() => void overview.reload()}>
          <RefreshCw className={overview.loading ? "animate-spin" : undefined} />
        </Button>
      </nav>
      <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,1fr)_22rem] xl:grid-cols-[minmax(0,1fr)_26rem]">
        <div className="grid min-w-0 gap-4">
          <Card>
            <SectionHeader title={t("topics.latestHandoff")} action={
              <Button size="sm" variant={showRecords ? "secondary" : "outline"} aria-expanded={showRecords} onClick={() => setShowRecords(!showRecords)}>
                <History data-icon="inline-start" />{t("topics.handoffHistory")}
              </Button>
            } />
            <CardContent className="grid gap-1">
              {data.latest_handoff ? (
                <>
                  <p className="break-words font-medium">{data.latest_handoff.title}</p>
                  <p className="text-xs text-muted-foreground">
                    {data.latest_handoff.author_username} · {fmtTime(data.latest_handoff.created_at)}
                  </p>
                  {data.latest_handoff.text && <div className="mt-2 text-sm"><Markdown>{data.latest_handoff.text}</Markdown></div>}
                  <RecordEvidence record={data.latest_handoff} />
                </>
              ) : <p className="text-sm text-muted-foreground">{t("topics.noHandoff")}</p>}
            </CardContent>
          </Card>
          {showRecords && <TopicRecords topic={topic} onChanged={overview.reload} />}
          <RunsCard active={data.active_runs} activeCount={data.active_run_count} recent={data.recent_runs} topicId={topicId} />
        </div>
        <div className="grid min-w-0 gap-4">
          <ReferencesCard data={data} topic={topic} onChanged={overview.reload} />
          <MaterialsCard data={data} topic={topic} onChanged={overview.reload} />
        </div>
      </div>
    </div>
  );
}

function SectionHeader({ title, badge, action }: { title: ReactNode; badge?: ReactNode; action?: ReactNode }) {
  return (
    <CardHeader className="flex-row flex-wrap items-center justify-between gap-2 space-y-0 pb-4">
      <div className="flex min-w-0 items-center gap-2">
        <CardTitle className="text-base">{title}</CardTitle>
        {badge}
      </div>
      {action}
    </CardHeader>
  );
}

/** Active runs first (they may be older than the recent ones), then the rest of the recent runs. */
function RunsCard({ active, activeCount, recent, topicId }: {
  active: TopicRunSummary[];
  activeCount: number;
  recent: TopicRunSummary[];
  topicId: string;
}) {
  const shown = new Set(active.map((run) => run.id));
  const runs = [...active, ...recent.filter((run) => !shown.has(run.id))];
  return (
    <Card>
      <SectionHeader
        title={t("topics.runs")}
        badge={activeCount > 0 ? <Pill tone="info">{t("topics.activeCount", { count: activeCount })}</Pill> : undefined}
      />
      <CardContent>
        {runs.length === 0 ? <p className="text-sm text-muted-foreground">{t("topics.noRuns")}</p> : (
          <ul className="-mx-2 divide-y">
            {runs.map((run) => (
              <li key={run.id} className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 gap-y-0.5 px-2 py-2 sm:grid-cols-[minmax(0,1fr)_auto_auto] md:grid-cols-[minmax(0,1fr)_auto_auto_auto]">
                <Link to={scopedPath(`/runs/${run.id}`, topicId)} className="truncate text-sm font-medium text-primary hover:underline" title={run.name}>
                  {run.name}
                </Link>
                <span className="justify-self-end sm:order-last"><StatusPill value={label(RUN_STATE, run.state)} /></span>
                <span className="text-sm"><ScoreCoverage scoring={run.scoring} /></span>
                <span className="hidden text-xs tabular-nums text-muted-foreground md:inline">{fmtTime(run.accepted_at)}</span>
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

function ReferencesCard({ data, topic, onChanged }: { data: TopicOverview; topic: Topic; onChanged: () => Promise<void> }) {
  const [paging, setPaging] = useState(false);
  return (
    <Card>
      <SectionHeader title={t("topics.selected")} action={
        <Button variant="outline" size="sm" aria-expanded={paging} onClick={() => setPaging(!paging)}>
          {paging ? t("topics.collapse") : t("topics.references")}
        </Button>
      } />
      <CardContent>
        {paging ? <TopicObjectDirectory mode="references" topic={topic} onChanged={onChanged} /> : (
          <div className="grid gap-4">
            <ObjectPreview heading={t("topics.kind.collection")} items={data.collections} topicId={topic.id} />
            <ObjectPreview heading={t("topics.kind.dataset")} items={data.datasets} topicId={topic.id} />
            <ObjectPreview heading={t("topics.kind.attachment_snapshot")} items={data.attachments} topicId={topic.id} />
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function MaterialsCard({ data, topic, onChanged }: { data: TopicOverview; topic: Topic; onChanged: () => Promise<void> }) {
  const [paging, setPaging] = useState(false);
  return (
    <Card>
      <details key={String(topic.settings.materials_expanded)} open={topic.settings.materials_expanded} className="group">
        <summary className="flex cursor-pointer list-none items-center gap-2 p-6 [&::-webkit-details-marker]:hidden">
          <ChevronRight className="size-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-90" />
          <span className="font-semibold leading-none tracking-tight">{t("topics.actual")}</span>
        </summary>
        <div className="grid gap-4 px-6 pb-6">
          {paging ? <TopicObjectDirectory mode="materials" topic={topic} onChanged={onChanged} /> : (
            <>
              <ObjectPreview heading={t("topics.kind.dataset_version")} items={data.used_dataset_versions} topicId={topic.id} />
              <ObjectPreview heading={t("topics.kind.attachment_snapshot")} items={data.used_attachment_snapshots} topicId={topic.id} />
            </>
          )}
          <div>
            <Button variant="outline" size="sm" aria-expanded={paging} onClick={() => setPaging(!paging)}>
              {paging ? t("topics.collapse") : t("topics.materials")}
            </Button>
          </div>
        </div>
      </details>
    </Card>
  );
}

function objectPath(item: TopicReference | TopicMaterial, topicId: string) {
  switch (item.kind) {
    case "run": return scopedPath(`/runs/${item.id}`, topicId);
    case "collection": return scopedPath(`/collections/${item.id}`, topicId);
    case "dataset": return scopedPath(datasetPath(item.id), topicId);
    case "dataset_version": return scopedPath(`/datasets?version=${item.id}`, topicId);
    case "attachment_snapshot": return scopedPath(`/attachments?snapshot=${item.id}`, topicId);
  }
}

function ObjectRow({ item, topicId, children }: {
  item: TopicReference | TopicMaterial;
  topicId: string;
  children?: ReactNode;
}) {
  const title = item.label ? `${item.name} · ${item.label}` : item.name;
  const canOpen = item.deletion?.state !== "purged";
  return (
    <li className="min-w-0 py-2 first:pt-0 last:pb-0">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm">
        {children}
        {canOpen ? <Link className="break-all text-primary hover:underline" to={objectPath(item, topicId)}>{title}</Link> : <span className="break-all">{title}</span>}
        {item.deletion && <Pill tone="muted">{t(item.deletion.state === "purged" ? "topics.purged" : "topics.objectRecycled")}</Pill>}
      </div>
      {"source_run_count" in item && (
        <div className="mt-1 flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-xs text-muted-foreground">
          <Link className="text-primary hover:underline" to={scopedPath(
            `/runs?${item.kind === "dataset_version" ? "dataset_version_id" : "attachment_version_id"}=${item.id}`,
            topicId,
          )}>{t("topics.sourceCount", { count: item.source_run_count })}</Link>
          {item.source_run_ids.map((id) => (
            <Link key={id} className="font-mono text-primary hover:underline" title={id} to={scopedPath(`/runs/${id}`, topicId)}>
              {id.slice(-8)}
            </Link>
          ))}
        </div>
      )}
    </li>
  );
}

function ObjectPreview({ heading, items, topicId }: {
  heading: string;
  items: (TopicReference | TopicMaterial)[];
  topicId: string;
}) {
  return (
    <section className="min-w-0">
      <h3 className="mb-1.5 flex items-baseline gap-2 text-xs font-medium text-muted-foreground">
        {heading}
        {items.length > 0 && <span className="tabular-nums">{items.length}</span>}
      </h3>
      {items.length === 0 ? <p className="text-sm text-muted-foreground/70">{t("topics.noObjects")}</p> : (
        <ul className="divide-y">{items.map((item) => <ObjectRow key={item.id} item={item} topicId={topicId} />)}</ul>
      )}
    </section>
  );
}

function TopicObjectDirectory({ mode, topic, onChanged }: {
  mode: "references" | "materials";
  topic: Topic;
  onChanged: () => Promise<void>;
}) {
  const id = useId();
  const [kind, setKind] = useState<TopicReferenceKind | "dataset_version">(mode === "references" ? "run" : "dataset_version");
  const selection = useDirectorySelection(kind);
  const path = { topic_id: topic.id };
  const page = useCursorList<TopicReference | TopicMaterial>((cursor) => {
    if (mode === "materials") {
      return api.get("/topics/{topic_id}/materials", {
        path, query: { kind: kind === "attachment_snapshot" ? kind : "dataset_version", cursor },
      });
    }
    return api.get("/topics/{topic_id}/references", {
      path, query: { kind: kind === "dataset_version" ? "run" : kind, cursor },
    });
  }, kind);
  const [removing, setRemoving] = useState(false);
  const canRemove = mode === "references" && topic.allowed_actions.includes("manage_references");
  const remove = async () => {
    if (kind === "dataset_version") return;
    setRemoving(true);
    try {
      await api.post("/topics/{topic_id}/references", {
        path, body: { remove: [...selection.selected].map((id) => ({ kind, id })) },
      });
      selection.setSelected(new Set());
      await Promise.all([page.reload(), onChanged()]);
      toast.success(t("topics.removed"));
    } catch (err) {
      toast.error(errorText(err));
    } finally {
      setRemoving(false);
    }
  };
  return (
    <div className="grid gap-3">
        <div className="flex flex-wrap items-end gap-2">
          <SelectField id={id} label={t("topics.evidenceType")} value={kind} onChange={(e) => setKind(e.target.value as typeof kind)}>
            {(mode === "references" ? referenceKinds : materialKinds).map((value) => (
              <option key={value} value={value}>{t(`topics.kind.${value}`)}</option>
            ))}
          </SelectField>
          {canRemove && selection.selected.size > 0 && (
            <Button size="sm" variant="outline" disabled={removing || selection.selected.size > 200} onClick={() => void remove()}>
              {t("topics.removeSelected", { count: selection.selected.size })}
            </Button>
          )}
        </div>
        <ErrorAlert error={page.error} />
        {page.loading && !page.data && <Loading />}
        {page.data?.length === 0 && <EmptyNote>{t("topics.noObjects")}</EmptyNote>}
        <ul className="divide-y">
          {page.data?.map((item) => <ObjectRow key={item.id} item={item} topicId={topic.id}>
            {canRemove && <DirectoryCheck name={item.name} checked={selection.selected.has(item.id)} onChange={() => selection.toggle(item.id)} />}
          </ObjectRow>)}
        </ul>
        <MoreItems page={page} />
    </div>
  );
}

function TopicRecords({ topic, onChanged }: { topic: Topic; onChanged: () => Promise<void> }) {
  const records = useCursorList((cursor) => api.get("/records", {
    query: { target_type: "topic", target_id: topic.id, cursor, limit: 25 },
  }), topic.id);
  return (
    <>
      <WorkflowRecords
        targetType="topic"
        targetId={topic.id}
        readOnly={!!topic.deletion}
        records={{
          data: records.data,
          error: records.error,
          loading: records.loading,
          reload: async () => { await Promise.all([records.reload(), onChanged()]); },
        }}
      />
      <MoreItems page={records} />
    </>
  );
}
