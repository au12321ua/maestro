import { useId, useState, type FormEvent, type ReactNode } from "react";
import { Link, useHref, useNavigate, useParams } from "react-router-dom";
import { Copy, Pencil, Plus, RefreshCw, Trash2 } from "lucide-react";
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
import { api, errorText } from "@/lib/api";
import { fmtTime, label, RUN_STATE } from "@/lib/format";
import { useCursorList, useLoad, useSession } from "@/lib/hooks";
import { scopedPath, useTopicScope } from "@/lib/topics";
import type { Topic, TopicMaterial, TopicReference, TopicReferenceKind, TopicRunSummary } from "@/lib/types";

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
      <div className="max-w-md">
        <TextField id="topic-search" label={t("topics.searchTopics")} value={q} onChange={(e) => setQuery(e.target.value)} />
      </div>
      <ErrorAlert error={page.error} />
      {page.loading && !page.data && <Loading />}
      {page.data?.length === 0 && <EmptyNote>{t("topics.empty")}</EmptyNote>}
      {page.data?.length ? (
        <ul className="grid gap-2">
          {page.data.map((topic) => (
            <li key={topic.id}>
              <Card>
                <CardHeader>
                  <CardTitle className="text-base">
                    <Link to={topic.web_path} className="text-primary hover:underline">{topic.name}</Link>
                  </CardTitle>
                </CardHeader>
                <CardContent className="grid gap-2">
                  {topic.purpose && <p className="line-clamp-3 whitespace-pre-line text-sm">{topic.purpose}</p>}
                  <code className="break-all text-xs text-muted-foreground">{topic.id}</code>
                </CardContent>
              </Card>
            </li>
          ))}
        </ul>
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
  const [showDetails, setShowDetails] = useState<"references" | "materials" | null>(null);
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

  return (
    <div className="grid gap-4">
      <PageTitle title={topic.name} back={back}>
        <Button variant="outline" size="sm" onClick={() => void copy()}><Copy data-icon="inline-start" />{t("topics.copyLink")}</Button>
        {topic.allowed_actions.includes("edit") && (
          <Button variant="outline" size="sm" onClick={() => setEditing(true)}><Pencil data-icon="inline-start" />{t("topics.edit")}</Button>
        )}
        {topic.allowed_actions.includes("delete") && (
          <Button variant="outline" size="sm" onClick={() => void remove()}><Trash2 data-icon="inline-start" />{t("topics.delete")}</Button>
        )}
        {topic.allowed_actions.includes("submit") && (
          <Button size="sm" asChild><Link to={link("/runs/new")}><Plus data-icon="inline-start" />{t("topics.newRun")}</Link></Button>
        )}
      </PageTitle>
      <ErrorAlert error={overview.error} />
      {topic.deletion && <Pill tone="warn">{t("topics.recycled")}</Pill>}
      {editing ? (
        <TopicForm topic={topic} onCancel={() => setEditing(false)} onSave={async (body) => {
          await api.put("/topics/{topic_id}", { path, body });
          setEditing(false);
          await Promise.all([overview.reload(), scope.topicId === topic.id ? scope.reload() : undefined]);
        }} />
      ) : topic.purpose ? <Markdown>{topic.purpose}</Markdown> : null}
      <nav aria-label={t("topics.scope")} className="flex flex-wrap gap-2">
        <Button variant="outline" size="sm" asChild><Link to={link("/runs")}>{t("topics.allRuns")}</Link></Button>
        <Button variant="outline" size="sm" asChild><Link to={link("/collections")}>{t("topics.allCollections")}</Link></Button>
        <Button variant="outline" size="sm" asChild><Link to={link("/datasets")}>{t("topics.allDatasets")}</Link></Button>
        <Button variant="outline" size="sm" asChild><Link to={link("/attachments")}>{t("topics.allAttachments")}</Link></Button>
        <Button variant="ghost" size="sm" aria-label={t("runs.list.refresh")} disabled={overview.loading} onClick={() => void overview.reload()}>
          <RefreshCw />
        </Button>
      </nav>
      <Card>
        <CardHeader className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="text-base">{t("topics.latestHandoff")}</CardTitle>
          <Button size="sm" variant="outline" aria-expanded={showRecords} onClick={() => setShowRecords(!showRecords)}>
            {t("topics.handoffHistory")}
          </Button>
        </CardHeader>
        <CardContent className="grid gap-2">
          {data.latest_handoff ? (
            <>
              <p className="break-words text-sm font-medium">{data.latest_handoff.title}</p>
              <p className="text-xs text-muted-foreground">
                {data.latest_handoff.author_username} · {fmtTime(data.latest_handoff.created_at)}
              </p>
              {data.latest_handoff.text && <Markdown>{data.latest_handoff.text}</Markdown>}
              <RecordEvidence record={data.latest_handoff} />
            </>
          ) : <p className="text-sm text-muted-foreground">{t("topics.noHandoff")}</p>}
        </CardContent>
      </Card>
      {showRecords && <TopicRecords topic={topic} onChanged={overview.reload} />}
      <div className="grid gap-4 lg:grid-cols-2">
        <RunPreview title={t("topics.activeRuns", { count: data.active_run_count })} runs={data.active_runs} topicId={topicId} />
        <RunPreview title={t("topics.recentRuns")} runs={data.recent_runs} topicId={topicId} />
      </div>
      <Card>
        <CardHeader className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle className="text-base">{t("topics.selected")}</CardTitle>
          <Button variant="outline" size="sm" aria-expanded={showDetails === "references"} onClick={() => setShowDetails(showDetails === "references" ? null : "references")}>
            {t("topics.references")}
          </Button>
        </CardHeader>
        <CardContent className="grid gap-4 sm:grid-cols-3">
          <ObjectPreview heading={t("topics.kind.collection")} items={data.collections} topicId={topicId} />
          <ObjectPreview heading={t("topics.kind.dataset")} items={data.datasets} topicId={topicId} />
          <ObjectPreview heading={t("topics.kind.attachment_snapshot")} items={data.attachments} topicId={topicId} />
        </CardContent>
      </Card>
      {showDetails === "references" && <TopicObjectDirectory mode="references" topic={topic} onChanged={overview.reload} />}
      <Card>
        <CardContent>
          <details key={String(topic.settings.materials_expanded)} open={topic.settings.materials_expanded}>
            <summary className="cursor-pointer text-sm font-semibold">{t("topics.actual")}</summary>
            <div className="mt-3 grid gap-4 sm:grid-cols-2">
              <ObjectPreview heading={t("topics.kind.dataset_version")} items={data.used_dataset_versions} topicId={topicId} />
              <ObjectPreview heading={t("topics.kind.attachment_snapshot")} items={data.used_attachment_snapshots} topicId={topicId} />
            </div>
            <Button className="mt-3" variant="outline" size="sm" aria-expanded={showDetails === "materials"} onClick={() => setShowDetails(showDetails === "materials" ? null : "materials")}>
              {t("topics.materials")}
            </Button>
          </details>
        </CardContent>
      </Card>
      {showDetails === "materials" && <TopicObjectDirectory mode="materials" topic={topic} onChanged={overview.reload} />}
    </div>
  );
}

function RunPreview({ title, runs, topicId }: { title: string; runs: TopicRunSummary[]; topicId: string }) {
  return (
    <Card>
      <CardHeader><CardTitle className="text-base">{title}</CardTitle></CardHeader>
      <CardContent>
        {runs.length === 0 ? <p className="text-sm text-muted-foreground">{t("topics.noObjects")}</p> : (
          <ul className="divide-y">
            {runs.map((run) => (
              <li key={run.id} className="grid gap-1 py-2 first:pt-0 last:pb-0">
                <div className="flex flex-wrap items-center justify-between gap-1">
                  <Link to={scopedPath(`/runs/${run.id}`, topicId)} className="break-all text-sm font-medium text-primary hover:underline">{run.name}</Link>
                  <StatusPill value={label(RUN_STATE, run.state)} />
                </div>
                <ScoreCoverage scoring={run.scoring} />
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

function objectPath(item: TopicReference | TopicMaterial, topicId: string) {
  switch (item.kind) {
    case "run": return scopedPath(`/runs/${item.id}`, topicId);
    case "collection": return scopedPath(`/collections/${item.id}`, topicId);
    case "dataset": return scopedPath(`/datasets?dataset=${item.id}`, topicId);
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
        <div className="mt-1 flex flex-wrap items-baseline gap-x-2 gap-y-1 text-xs text-muted-foreground">
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
      <h3 className="mb-2 text-sm font-medium">{heading}</h3>
      {items.length === 0 ? <p className="text-sm text-muted-foreground">{t("topics.noObjects")}</p> : (
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
    <Card>
      <CardHeader><CardTitle className="text-base">{t(mode === "references" ? "topics.references" : "topics.materials")}</CardTitle></CardHeader>
      <CardContent className="grid gap-3">
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
      </CardContent>
    </Card>
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
