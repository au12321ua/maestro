import { useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { GitCompare, Package, Plus, RefreshCw, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { DIRECTORY_PAGE_SIZE, TopicDirectory, TopicMembershipActions, useDirectorySelection } from "@/components/TopicDirectory";
import { scopedPath, useTopicScope } from "@/lib/topics";
import { AddToCollection } from "@/components/AddToCollection";
import {
  EmptyNote,
  ErrorAlert,
  Loading,
  PageTitle,
  ScoreCoverage,
  StatusPill,
  SelectField,
} from "@/components/common";
import { t } from "@/i18n";
import { api, errorText } from "@/lib/api";
import {
  ARTIFACT_STATE,
  CLEANUP_STATE,
  RESULT_COMPLETENESS,
  RUN_STATE,
  dayToApiTime,
  fmtBytes,
  fmtTime,
  label,
  shortId,
} from "@/lib/format";
import { useCursorList, useSession } from "@/lib/hooks";
import { cn } from "@/lib/utils";

export function RunListPage() {
  const { me } = useSession();
  const [scope, setScope] = useState<"mine" | "all">("all");
  const [retainedOnly, setRetainedOnly] = useState(false);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [q, setQuery] = useState("");
  const [state, setState] = useState("");
  const { topicId } = useTopicScope();
  const [params] = useSearchParams();
  const datasetVersion = params.get("dataset_version_id");
  const attachmentVersion = params.get("attachment_version_id");
  const [exporting, setExporting] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const navigate = useNavigate();

  const query = {
    owner_id: scope === "mine" ? me.id : undefined,
    accepted_after: dayToApiTime(from),
    accepted_before: dayToApiTime(to, 1),
    q: q.trim() || undefined,
    state: state || undefined,
    topic_id: topicId ?? undefined,
    artifacts_retained: retainedOnly || undefined,
    dataset_version_id: datasetVersion ?? undefined,
    attachment_version_id: attachmentVersion ?? undefined,
    limit: DIRECTORY_PAGE_SIZE,
  };
  const queryKey = JSON.stringify(query);
  const page = useCursorList((cursor) => api.get("/runs", { query: { ...query, cursor } }), queryKey);
  const { cursor, error, loading } = page;
  const runs = page.data ?? [];
  const { selected, setSelected, toggle } = useDirectorySelection(queryKey);
  const reload = async () => {
    setSelected(new Set());
    await page.reload();
  };
  const allSelected = runs.length > 0 && runs.every((r) => selected.has(r.id));

  const exportSelected = async () => {
    setExporting(true);
    try {
      await api.post("/exports", { body: { run_ids: [...selected] } });
      toast.success(t("runs.list.exportCreated", { count: selected.size }));
      navigate("/exports");
    } catch (err) {
      toast.error(t("runs.list.exportFailed", { error: errorText(err) }));
    } finally {
      setExporting(false);
    }
  };

  // What the server would allow: ended runs the caller owns (any, for an admin).
  const deletable = runs.filter((r) => selected.has(r.id) && r.allowed_actions.includes("delete"));
  const deleteSelected = async () => {
    const skipped = selected.size - deletable.length;
    const text =
      t("runs.list.deleteConfirm", { count: deletable.length }) +
      (skipped > 0 ? `\n\n${t("runs.list.deleteSkipped", { count: skipped })}` : "");
    if (!window.confirm(text)) return;
    setDeleting(true);
    let done = 0;
    let failed = 0;
    let firstError: unknown;
    for (const run of deletable) {
      try {
        await api.del("/runs/{run_id}", { path: { run_id: run.id } });
        done += 1;
      } catch (err) {
        failed += 1;
        firstError ??= err;
      }
    }
    if (failed === 0) toast.success(t("runs.list.deleted", { count: done }));
    else toast.error(t("runs.list.deletePartial", { done, failed, error: errorText(firstError) }));
    setDeleting(false);
    await reload();
  };

  return (
    <div>
      <PageTitle title={t("runs.list.title")}>
        <div className="flex rounded-md border bg-background p-0.5 text-sm">
          {(["all", "mine"] as const).map((value) => (
            <button
              key={value}
              type="button"
              className={cn(
                "rounded px-3 py-1",
                scope === value ? "bg-primary text-primary-foreground" : "text-muted-foreground",
              )}
              onClick={() => setScope(value)}
            >
              {value === "all" ? t("runs.list.scopeAll") : t("runs.list.scopeMine")}
            </button>
          ))}
        </div>
        <label className="flex items-center gap-1 text-xs text-muted-foreground">
          {t("runs.list.acceptedFrom")}
          <input
            type="date"
            aria-label={t("runs.list.startDate")}
            className="h-8 rounded-md border bg-background px-2 text-sm text-foreground"
            value={from}
            onChange={(e) => setFrom(e.target.value)}
          />
          {t("runs.list.dateTo")}
          <input
            type="date"
            aria-label={t("runs.list.endDate")}
            className="h-8 rounded-md border bg-background px-2 text-sm text-foreground"
            value={to}
            onChange={(e) => setTo(e.target.value)}
          />
        </label>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={retainedOnly} onChange={(e) => setRetainedOnly(e.target.checked)} />
          {t("runs.retention.filter")}
        </label>
        <Button
          variant="outline"
          size="sm"
          disabled={selected.size < 2}
          title={t("runs.list.compareTitle")}
          onClick={() =>
            navigate(
              `/compare?runs=${runs
                .filter((r) => selected.has(r.id))
                .map((r) => r.id)
                .join(",")}`,
            )
          }
        >
          <GitCompare />
          {t("runs.list.compare")}
        </Button>
        <AddToCollection runIds={runs.filter((r) => selected.has(r.id)).map((r) => r.id)} />
        <Button
          variant="outline"
          size="sm"
          disabled={selected.size === 0 || exporting}
          onClick={() => void exportSelected()}
        >
          <Package />
          {t("runs.list.export")}
          {selected.size > 0 && t("runs.list.selectedCount", { n: selected.size })}
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={deletable.length === 0 || deleting}
          title={t("runs.list.deleteTitle")}
          onClick={() => void deleteSelected()}
        >
          <Trash2 />
          {t("runs.list.delete")}
          {deletable.length > 0 && t("runs.list.selectedCount", { n: deletable.length })}
        </Button>
        <Button variant="outline" size="sm" onClick={() => void reload()}>
          <RefreshCw />
          {t("runs.list.refresh")}
        </Button>
        <Button asChild size="sm">
          <Link to={scopedPath("/runs/new", topicId)}>
            <Plus />
            {t("runs.list.newRun")}
          </Link>
        </Button>
      </PageTitle>
      <TopicDirectory search={q} onSearch={setQuery}>
        <SelectField id="run-state" label={t("topics.state")} value={state} onChange={(e) => setState(e.target.value)}>
          <option value="">{t("topics.anyState")}</option>
          {Object.entries(RUN_STATE).map(([value, [name]]) => <option key={value} value={value}>{name}</option>)}
        </SelectField>
        {(datasetVersion || attachmentVersion) && (
          <Button size="sm" variant="outline" asChild>
            <Link to={scopedPath("/runs", topicId)}>{t("topics.clearMaterial", { id: datasetVersion ?? attachmentVersion })}</Link>
          </Button>
        )}
      </TopicDirectory>
      <TopicMembershipActions kind="run" ids={selected} onChanged={reload} />
      <ErrorAlert error={error} />
      {runs.length === 0 && !loading && !error && <EmptyNote>{t("runs.list.empty")}</EmptyNote>}
      {runs.length > 0 && (
        <div className="rounded-md border bg-card">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-8">
                  <input
                    type="checkbox"
                    aria-label={t("runs.list.selectAll")}
                    checked={allSelected}
                    onChange={() =>
                      setSelected(allSelected ? new Set() : new Set(runs.map((r) => r.id)))
                    }
                  />
                </TableHead>
                <TableHead>{t("runs.column.name")}</TableHead>
                <TableHead>{t("runs.column.owner")}</TableHead>
                <TableHead>{t("runs.column.lifecycle")}</TableHead>
                <TableHead>{t("runs.column.score")}</TableHead>
                <TableHead>{t("runs.column.resultCompleteness")}</TableHead>
                <TableHead>{t("runs.column.artifacts")}</TableHead>
                <TableHead>{t("runs.column.cleanup")}</TableHead>
                <TableHead className="text-right">{t("runs.column.errors")}</TableHead>
                <TableHead>{t("runs.column.acceptedAt")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {runs.map((run) => (
                <TableRow key={run.id}>
                  <TableCell>
                    <input
                      type="checkbox"
                      aria-label={t("runs.list.select", { name: run.name })}
                      checked={selected.has(run.id)}
                      onChange={() => toggle(run.id)}
                    />
                  </TableCell>
                  <TableCell className="max-w-[24rem]">
                    <Link
                      className="block truncate font-medium text-primary hover:underline"
                      to={scopedPath(`/runs/${run.id}`, topicId)}
                      title={run.name}
                    >
                      {run.name}
                    </Link>
                    <div className="text-xs text-muted-foreground">{shortId(run.id, 16)}</div>
                  </TableCell>
                  <TableCell className="text-xs">
                    {run.owner_id === me.id ? t("runs.me") : run.owner.display_name}
                    {run.owner.deleted && <span className="text-muted-foreground">{t("runs.ownerDeleted")}</span>}
                    {run.pat_id && <div className="text-muted-foreground">PAT</div>}
                  </TableCell>
                  <TableCell>
                    <StatusPill value={label(RUN_STATE, run.state)} />
                    {run.stop_requested && run.state !== "cancelled" && (
                      <div className="mt-1 text-xs text-amber-700">{t("runs.stopRequested")}</div>
                    )}
                  </TableCell>
                  <TableCell>
                    <ScoreCoverage scoring={run.scoring} />
                  </TableCell>
                  <TableCell>
                    <StatusPill value={label(RESULT_COMPLETENESS, run.result_completeness)} />
                  </TableCell>
                  <TableCell>
                    <StatusPill value={label(ARTIFACT_STATE, run.artifact_state)} />
                    {run.retained_artifact_bytes != null && (
                      <div className="mt-1 text-xs text-muted-foreground">
                        {t("runs.retention.retainedSize", { size: fmtBytes(run.retained_artifact_bytes) })}
                      </div>
                    )}
                  </TableCell>
                  <TableCell>
                    <StatusPill value={label(CLEANUP_STATE, run.cleanup_state)} />
                  </TableCell>
                  <TableCell className="text-right tabular-nums">{run.counts.errors}</TableCell>
                  <TableCell className="text-xs">{fmtTime(run.accepted_at)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
      {loading && <Loading />}
      {cursor && !loading && (
        <div className="mt-4 flex justify-center">
          <Button variant="outline" onClick={() => void page.more()}>
            {t("runs.list.loadMore")}
          </Button>
        </div>
      )}
    </div>
  );
}
