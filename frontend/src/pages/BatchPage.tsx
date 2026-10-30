// Batches (04 section 4): runs accepted together; each runs and ends on its own.
import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
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
import { BackLink, EmptyNote, ErrorAlert, Loading, PageTitle, StatusPill } from "@/components/common";
import { t } from "@/i18n";
import { api, errorText } from "@/lib/api";
import { OPERATION_STATUS, RUN_STATE, fmtTime, label } from "@/lib/format";
import { useInterval, useLoad } from "@/lib/hooks";
import type { Batch } from "@/lib/types";

const ENDED = new Set(["succeeded", "failed", "cancelled", "interrupted"]);

export function BatchListPage() {
  const list = useLoad(async () => (await api.get("/run-batches")).data);
  return (
    <div className="grid gap-4">
      <PageTitle title={t("runs.batch.title")} />
      {list.loading && !list.data && <Loading />}
      <ErrorAlert error={list.error} />
      {list.data && list.data.length === 0 && (
        <EmptyNote>{t("runs.batch.empty")}</EmptyNote>
      )}
      {list.data && list.data.length > 0 && (
        <div className="overflow-auto rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t("runs.column.name")}</TableHead>
                <TableHead>{t("runs.column.owner")}</TableHead>
                <TableHead>{t("runs.column.status")}</TableHead>
                <TableHead>{t("runs.column.run")}</TableHead>
                <TableHead>{t("runs.column.acceptedAt")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {list.data.map((b) => (
                <TableRow key={b.id}>
                  <TableCell className="max-w-[24rem]">
                    <Link
                      className="block truncate font-medium text-primary underline"
                      to={`/batches/${b.id}`}
                      title={b.name}
                    >
                      {b.name}
                    </Link>
                  </TableCell>
                  <TableCell className="text-xs">{b.owner_username}</TableCell>
                  <TableCell>
                    <StatusPill value={label(OPERATION_STATUS, b.status)} />
                  </TableCell>
                  <TableCell className="text-xs">
                    {t("runs.batch.ended", {
                      ended: b.progress.terminal_runs,
                      runs: b.progress.runs,
                    })}
                  </TableCell>
                  <TableCell className="text-xs">{fmtTime(b.created_at)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}

export function BatchPage() {
  const { batchId = "" } = useParams();
  const [batch, setBatch] = useState<Batch | null>(null);
  const [error, setError] = useState<unknown>();
  const load = useCallback(async () => {
    try {
      setBatch((await api.get("/run-batches/{batch_id}", { path: { batch_id: batchId } })).data);
      setError(undefined);
    } catch (err) {
      setError(err);
    }
  }, [batchId]);
  useEffect(() => {
    void load();
  }, [load]);
  useInterval(() => void load(), 2000, !!batch && !ENDED.has(batch.status));

  const cancel = async () => {
    if (!window.confirm(t("runs.batch.cancelConfirm"))) return;
    try {
      setBatch(
        (await api.post("/run-batches/{batch_id}/cancel", { path: { batch_id: batchId } })).data,
      );
      toast.success(t("runs.batch.cancelRequested"));
    } catch (err) {
      toast.error(t("runs.cancelFailed", { error: errorText(err) }));
    }
  };

  if (!batch)
    return error ? (
      <div>
        <BackLink to="/batches">{t("common.nav.back.batches")}</BackLink>
        <ErrorAlert error={error} />
      </div>
    ) : (
      <Loading />
    );
  const p = batch.progress;
  return (
    <div className="grid gap-4">
      <PageTitle
        title={t("runs.batch.detailTitle", { name: batch.name })}
        back={{ to: "/batches", label: t("common.nav.back.batches") }}
      >
        {batch.runs.length > 1 && (
          <Button asChild size="sm" variant="outline">
            <Link to={`/compare?runs=${batch.runs.map((r) => r.id).join(",")}`}>
              {t("runs.batch.compare")}
            </Link>
          </Button>
        )}
        {batch.allowed_actions.includes("cancel") && (
          <Button size="sm" variant="destructive" onClick={() => void cancel()}>
            {t("runs.batch.cancel")}
          </Button>
        )}
      </PageTitle>
      <ErrorAlert error={error} title={t("runs.refreshFailed")} />
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <StatusPill value={label(OPERATION_STATUS, batch.status)} />
            {batch.stop_requested && <span className="text-sm text-muted-foreground">{t("runs.batch.stopRequested")}</span>}
          </CardTitle>
        </CardHeader>
        <CardContent className="grid gap-1 text-sm">
          <p>
            {t("runs.batch.runProgress", {
              count: p.runs,
              ended: p.terminal_runs,
              finished: p.finished,
              cancelled: p.cancelled,
              interrupted: p.interrupted,
            })}
          </p>
          <p>
            {t("runs.batch.trialProgress", {
              count: p.planned_trials,
              scored: p.scored_trials,
              errors: p.error_trials,
            })}
          </p>
          <p className="text-xs text-muted-foreground">
            {batch.owner_username} · {fmtTime(batch.created_at)} · <code>{batch.id}</code>
          </p>
        </CardContent>
      </Card>
      <div className="overflow-auto rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>{t("runs.column.run")}</TableHead>
              <TableHead>{t("runs.column.status")}</TableHead>
              <TableHead>{t("runs.batch.scoredPlanned")}</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {batch.runs.map((r) => (
              <TableRow key={r.id}>
                <TableCell>
                  <Link className="text-primary underline" to={`/runs/${r.id}`}>
                    {r.name}
                  </Link>
                </TableCell>
                <TableCell>
                  <StatusPill value={label(RUN_STATE, r.state)} />
                </TableCell>
                <TableCell className="text-xs">
                  {r.counts.scored} / {r.counts.planned}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </div>
  );
}
