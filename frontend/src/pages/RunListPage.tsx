import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
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
import { AddToCollection } from "@/components/AddToCollection";
import {
  EmptyNote,
  ErrorAlert,
  Loading,
  PageTitle,
  ScoreCoverage,
  StatusPill,
} from "@/components/common";
import { t } from "@/i18n";
import { api, errorText } from "@/lib/api";
import {
  ARTIFACT_STATE,
  CLEANUP_STATE,
  RESULT_COMPLETENESS,
  RUN_STATE,
  dayToApiTime,
  fmtTime,
  label,
  shortId,
} from "@/lib/format";
import { useSession } from "@/lib/hooks";
import type { Run } from "@/lib/types";
import { cn } from "@/lib/utils";

export function RunListPage() {
  const { me } = useSession();
  const [scope, setScope] = useState<"mine" | "all">("all");
  const [runs, setRuns] = useState<Run[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState<unknown>();
  const [loading, setLoading] = useState(true);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [exporting, setExporting] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const navigate = useNavigate();

  const load = useCallback(
    async (after: string | null) => {
      setLoading(true);
      try {
        const res = await api.get("/runs", {
          query: {
            owner_id: scope === "mine" ? me.id : undefined,
            accepted_after: dayToApiTime(from),
            accepted_before: dayToApiTime(to, 1),
            cursor: after,
          },
        });
        setRuns((prev) => (after ? [...prev, ...res.data] : res.data));
        if (!after) setSelected(new Set());
        setCursor(res.meta.next_cursor ?? null);
        setError(undefined);
      } catch (err) {
        setError(err);
      } finally {
        setLoading(false);
      }
    },
    [scope, me.id, from, to],
  );

  useEffect(() => {
    void load(null);
  }, [load]);

  const toggle = (id: string) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setSelected(next);
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
    await load(null);
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
        <Button variant="outline" size="sm" onClick={() => void load(null)}>
          <RefreshCw />
          {t("runs.list.refresh")}
        </Button>
        <Button asChild size="sm">
          <Link to="/runs/new">
            <Plus />
            {t("runs.list.newRun")}
          </Link>
        </Button>
      </PageTitle>
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
                      to={`/runs/${run.id}`}
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
          <Button variant="outline" onClick={() => void load(cursor)}>
            {t("runs.list.loadMore")}
          </Button>
        </div>
      )}
    </div>
  );
}
