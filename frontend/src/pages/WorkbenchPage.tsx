import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { AlertTriangle, CircleCheck, Plus, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  EmptyNote,
  ErrorAlert,
  Loading,
  PageTitle,
  Pill,
  ScoreCoverage,
  StatusPill,
} from "@/components/common";
import { api } from "@/lib/api";
import { RUN_STATE, RUN_TERMINAL, fmtTime, label } from "@/lib/format";
import { useInterval, useLoad, useSession } from "@/lib/hooks";
import type { Dataset, Me, Operation, Run, SystemStatus } from "@/lib/types";
import { t } from "@/i18n";

const RECENT = 8;
/** How far back the pending list looks; older problems stay visible on their own pages. */
const SCAN = 100;
const OPERATION_KIND: Record<string, string> = t("workbench.operationKind", { returnObjects: true });

interface Pending {
  key: string;
  to: string;
  title: string;
  code: string;
  at: string | null;
  /** Who can act: the submitter (a rerun) or an administrator (settings). */
  who: "submitter" | "admin";
  action: string;
}

/**
 * 11 section 6: pending items are a query over business records, not a to-do list. An item
 * disappears when its source object changes; there is no "mark as done".
 */
function pendingItems(me: Me, mine: Run[], team: Run[], datasets: Dataset[], ops: Operation[]) {
  const items: Pending[] = [];
  const seen = new Set<string>();
  const add = (item: Pending) => {
    if (!seen.has(item.key)) {
      seen.add(item.key);
      items.push(item);
    }
  };
  for (const run of mine) {
    if (!RUN_TERMINAL.has(run.state)) continue;
    if (run.state === "interrupted" || run.counts.interrupted > 0) {
      add({
        key: `int-${run.id}`,
        to: `/runs/${run.id}`,
        title: t("workbench.pending.interrupted", { name: run.name, count: run.counts.interrupted }),
        code: "TRIAL_INTERRUPTED",
        at: run.finished_at,
        who: "submitter",
        action: t("workbench.pending.rerunUnscored"),
      });
    } else if (run.counts.errors > 0) {
      add({
        key: `err-${run.id}`,
        to: `/runs/${run.id}`,
        title: t("workbench.pending.errors", { name: run.name, count: run.counts.errors }),
        code: "EXECUTION_ERROR",
        at: run.finished_at,
        who: "submitter",
        action: t("workbench.pending.checkErrorsRerun"),
      });
    }
  }
  const admin = me.role === "admin";
  for (const run of admin ? team : mine) {
    if (run.cleanup_state === "failed") {
      add({
        key: `clean-${run.id}`,
        to: `/runs/${run.id}`,
        title: t("workbench.pending.cleanupFailed", { name: run.name }),
        code: "CLEANUP_FAILED",
        at: run.finished_at,
        who: "admin",
        action: t("workbench.pending.checkContainers"),
      });
    }
    if (run.artifact_state === "failed" || run.artifact_state === "partial") {
      add({
        key: `art-${run.id}`,
        to: `/runs/${run.id}?tab=artifacts`,
        title: t("workbench.pending.artifactFailed", { name: run.name }),
        code: "ARTIFACT_SAVE_FAILED",
        at: run.finished_at,
        who: admin ? "admin" : "submitter",
        action: t("workbench.pending.viewArtifacts"),
      });
    }
  }
  if (admin) {
    for (const dataset of datasets) {
      for (const version of dataset.versions) {
        const missing = version.task_count - version.admitted_task_count;
        if (missing > 0) {
          add({
            key: `adm-${version.id}`,
            to: "/datasets",
            title: t("workbench.pending.notAdmitted", {
              dataset: `${dataset.namespace}/${dataset.name}`,
              version: version.upstream_ref,
              count: missing,
            }),
            code: "TASK_NOT_ADMITTED",
            at: version.created_at,
            who: "admin",
            action: t("workbench.pending.retryPreparation"),
          });
        }
      }
    }
    for (const op of ops) {
      if (op.status !== "failed" && op.status !== "interrupted") continue;
      if (!(op.kind in OPERATION_KIND) || op.kind === "preparation_restore") continue;
      add({
        key: `op-${op.id}`,
        to: "/datasets",
        title: t(
          op.status === "failed" ? "workbench.pending.operationFailed" : "workbench.pending.operationInterrupted",
          { kind: OPERATION_KIND[op.kind] },
        ),
        code: op.error?.code ?? op.status.toUpperCase(),
        at: op.finished_at ?? op.updated_at,
        who: "admin",
        action: t("workbench.pending.handleOnDatasets"),
      });
    }
  }
  return items.sort((a, b) => (b.at ?? "").localeCompare(a.at ?? ""));
}

export function WorkbenchPage() {
  const { me } = useSession();
  const admin = me.role === "admin" && me.auth_channel === "web_session";
  const data = useLoad(async () => {
    const [mine, team, health, datasets, ops] = await Promise.all([
      api.get("/runs", { query: { owner_id: me.id, limit: SCAN } }),
      api.get("/runs", { query: { limit: SCAN } }),
      api.get("/system-status"),
      admin ? api.get("/datasets", { query: { limit: 200 } }) : null,
      admin ? api.get("/operations", { query: { limit: SCAN } }) : null,
    ]);
    return {
      mine: mine.data,
      team: team.data,
      health: health.data,
      pending: pendingItems(me, mine.data, team.data, datasets?.data ?? [], ops?.data ?? []),
    };
  }, [me.id, admin]);
  const active = !!data.data?.mine.some((r) => !RUN_TERMINAL.has(r.state));
  useInterval(() => void data.reload(), 5000, active);

  if (!data.data) {
    return data.error ? <ErrorAlert error={data.error} title={t("workbench.loadFailed")} /> : <Loading />;
  }
  const { mine, team, health, pending } = data.data;
  const results = team.filter((r) => r.state === "finished" && r.scoring).slice(0, RECENT);
  return (
    <div className="grid gap-4">
      <PageTitle title={t("common.nav.workbench")}>
        <Button size="sm" variant="outline" onClick={() => void data.reload()}>
          <RefreshCw />
          {t("workbench.refresh")}
        </Button>
        <Button asChild size="sm">
          <Link to="/runs/new">
            <Plus />
            {t("workbench.newRun")}
          </Link>
        </Button>
      </PageTitle>
      {data.error !== undefined && <ErrorAlert error={data.error} title={t("workbench.refreshFailed")} />}
      <SystemCard health={health} />
      <div className="grid gap-4 lg:grid-cols-2">
        <Section title={t("workbench.myRecent")} more="/runs">
          <RunRows runs={mine.slice(0, RECENT)} empty={t("workbench.myRecentEmpty")} />
        </Section>
        <Section title={t("workbench.teamResults")} more="/runs">
          <RunRows runs={results} empty={t("workbench.teamResultsEmpty")} showOwner />
        </Section>
      </div>
      <Card data-testid="pending">
        <CardHeader>
          <CardTitle className="text-base">{t("workbench.pending.title", { count: pending.length })}</CardTitle>
          <div className="text-xs text-muted-foreground">
            {t("workbench.pending.hint", { count: SCAN })}
          </div>
        </CardHeader>
        <CardContent>
          {pending.length === 0 ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <CircleCheck className="size-4 text-emerald-600" />
              {t("workbench.pending.none")}
            </div>
          ) : (
            <ul className="grid gap-1">
              {pending.map((item) => (
                <li key={item.key} data-testid="pending-item">
                  <Link
                    to={item.to}
                    className="flex flex-wrap items-center gap-2 rounded px-2 py-1.5 text-sm hover:bg-muted"
                  >
                    <AlertTriangle className="size-4 shrink-0 text-amber-600" />
                    <span>{item.title}</span>
                    <code className="text-xs text-muted-foreground">{item.code}</code>
                    <Pill tone={item.who === "admin" ? "warn" : "info"}>
                      {item.who === "admin" ? t("workbench.pending.byAdmin") : t("workbench.pending.bySubmitter")}
                    </Pill>
                    <span className="ml-auto text-xs text-muted-foreground">
                      {item.action} · {fmtTime(item.at)}
                    </span>
                  </Link>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function SystemCard({ health }: { health: SystemStatus }) {
  const ready = health.status === "ready";
  return (
    <Card>
      <CardContent className="flex flex-wrap items-center gap-3 pt-6 text-sm">
        <span className="text-muted-foreground">{t("workbench.system.label")}</span>
        {!ready ? (
          <Pill tone="bad">{t("common.app.state.maintenance")}</Pill>
        ) : health.accepting_runs ? (
          <Pill tone="good">{t("common.app.state.accepting")}</Pill>
        ) : (
          <Pill tone="warn">{t("common.app.state.notAccepting")}</Pill>
        )}
        <span className="text-xs text-muted-foreground">
          {t("workbench.system.hint", { status: health.status })}
        </span>
      </CardContent>
    </Card>
  );
}

function Section({ title, more, children }: { title: string; more: string; children: ReactNode }) {
  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between">
        <CardTitle className="text-base">{title}</CardTitle>
        <Link to={more} className="text-xs text-primary hover:underline">
          {t("workbench.all")}
        </Link>
      </CardHeader>
      <CardContent>{children}</CardContent>
    </Card>
  );
}

function RunRows({ runs, empty, showOwner }: { runs: Run[]; empty: string; showOwner?: boolean }) {
  if (runs.length === 0) return <EmptyNote>{empty}</EmptyNote>;
  return (
    <ul className="grid gap-0.5">
      {runs.map((run) => (
        <li key={run.id}>
          <Link
            to={`/runs/${run.id}`}
            className="flex flex-wrap items-center gap-2 rounded px-2 py-1.5 text-sm hover:bg-muted"
          >
            <StatusPill value={label(RUN_STATE, run.state)} />
            <span className="min-w-0 truncate font-medium" title={run.name}>
              {run.name}
            </span>
            {showOwner && (
              <span className="text-xs text-muted-foreground">{run.owner.display_name}</span>
            )}
            <span className="ml-auto flex items-center gap-3 text-xs">
              <ScoreCoverage scoring={run.scoring} />
              <span className="text-muted-foreground">{fmtTime(run.accepted_at)}</span>
            </span>
          </Link>
        </li>
      ))}
    </ul>
  );
}
