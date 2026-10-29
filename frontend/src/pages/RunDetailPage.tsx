import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import {
  ChevronDown,
  ChevronRight,
  CircleCheck,
  CircleDashed,
  CircleSlash,
  CircleX,
  Download,
  Loader2,
  Package,
  PencilLine,
  RefreshCw,
  RotateCcw,
  Trash2,
} from "lucide-react";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
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
  JsonBlock,
  Loading,
  LongName,
  PageTitle,
  Pill,
  StatusPill,
} from "@/components/common";
import { t, Trans } from "@/i18n";
import { API, api, errorText } from "@/lib/api";
import {
  ARTIFACT_STATE,
  ATTEMPT_STATE,
  CLEANUP_STATE,
  QUEUE_REASON,
  RESULT_COMPLETENESS,
  RUN_STATE,
  RUN_TERMINAL,
  STEP_LABEL,
  TRIAL_STATE,
  fmtBytes,
  fmtDuration,
  fmtNum,
  fmtPct,
  commonPrefix,
  fmtTime,
  label,
  shortId,
  type Tone,
  snapshotName,
} from "@/lib/format";
import { useInterval, useSession } from "@/lib/hooks";
import type {
  Artifact,
  Attempt,
  DeletionState,
  RunDetail,
  RunSummary,
  Trial,
} from "@/lib/types";
import { cn } from "@/lib/utils";
import { SetupNetworkRevision } from "@/pages/admin/SetupNetworkTab";
import { ArtifactTree } from "@/components/ArtifactTree";
import { AddToCollection } from "@/components/AddToCollection";

const TABS = new Set(["execution", "results", "config", "artifacts"]);
const TRIAL_TERMINAL = new Set(["scored", "execution_error", "not_started", "cancelled", "interrupted"]);

export function RunDetailPage() {
  const { runId = "" } = useParams();
  return <RunDetailView key={runId} runId={runId} />;
}

function RunDetailView({ runId }: { runId: string }) {
  const { me } = useSession();
  const [run, setRun] = useState<RunDetail>();
  const [trials, setTrials] = useState<Trial[]>();
  const [artifacts, setArtifacts] = useState<Artifact[]>();
  const [summary, setSummary] = useState<RunSummary>();
  const [summaryError, setSummaryError] = useState<unknown>();
  const [error, setError] = useState<unknown>();
  const [cancelling, setCancelling] = useState(false);
  const [busy, setBusy] = useState(false);
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const tab = TABS.has(params.get("tab") ?? "") ? params.get("tab")! : "execution";
  const setTab = (value: string) => {
    const next = new URLSearchParams(params);
    if (value === "execution") next.delete("tab");
    else next.set("tab", value);
    setParams(next, { replace: true });
  };
  const canDeleteArtifacts = me.role === "admin" && me.auth_channel === "web_session";

  const load = useCallback(async () => {
    try {
      const [r, tr, a] = await Promise.all([
        api.get("/runs/{run_id}", { path: { run_id: runId } }),
        api.get("/runs/{run_id}/trials", { path: { run_id: runId } }),
        api.get("/runs/{run_id}/artifacts", { path: { run_id: runId } }),
        api.get("/runs/{run_id}/summary", { path: { run_id: runId } }).then(
          (res) => {
            setSummary(res.data);
            setSummaryError(undefined);
          },
          (err: unknown) => setSummaryError(err),
        ),
      ]);
      setRun(r.data);
      setTrials(tr.data);
      setArtifacts(a.data);
      setError(undefined);
    } catch (err) {
      setError(err);
    }
  }, [runId]);

  useEffect(() => {
    void load();
  }, [load]);

  const active = !!run && !RUN_TERMINAL.has(run.state);
  useInterval(() => void load(), 2000, active);

  const cancel = async () => {
    if (!window.confirm(t("runs.detail.cancelConfirm"))) return;
    setCancelling(true);
    try {
      const res = await api.post("/runs/{run_id}/cancel", { path: { run_id: runId } });
      toast.success(
        res.data.stop_requested ? t("runs.detail.cancelRequested") : t("runs.detail.cancelSubmitted"),
      );
      await load();
    } catch (err) {
      toast.error(t("runs.cancelFailed", { error: errorText(err) }));
    } finally {
      setCancelling(false);
    }
  };

  const rerun = async (mode: "all" | "unfinished") => {
    const text =
      mode === "all"
        ? t("runs.detail.rerunAllConfirm")
        : t("runs.detail.rerunUnfinishedConfirm");
    if (!window.confirm(text)) return;
    setBusy(true);
    try {
      const res = await api.post("/runs/{run_id}/rerun", {
        path: { run_id: runId },
        body: { task_selection: { mode } },
        headers: { "Idempotency-Key": crypto.randomUUID() },
      });
      toast.success(t("runs.detail.rerunAccepted", { count: res.data.planned_trials }));
      navigate(`/runs/${res.data.run_id}`);
    } catch (err) {
      toast.error(t("runs.detail.rerunFailed", { error: errorText(err) }));
    } finally {
      setBusy(false);
    }
  };

  const exportRun = async () => {
    setBusy(true);
    try {
      await api.post("/exports", { body: { run_ids: [runId] } });
      toast.success(t("runs.detail.exportCreated"));
      navigate("/exports");
    } catch (err) {
      toast.error(t("runs.list.exportFailed", { error: errorText(err) }));
      setBusy(false);
    }
  };

  const remove = async () => {
    if (
      !window.confirm(
        t("runs.detail.deleteConfirm"),
      )
    )
      return;
    setBusy(true);
    try {
      await api.del("/runs/{run_id}", { path: { run_id: runId } });
      toast.success(t("runs.detail.deleted"));
      await load();
    } catch (err) {
      toast.error(t("runs.detail.deleteFailed", { error: errorText(err) }));
    } finally {
      setBusy(false);
    }
  };

  const removeArtifact = async (artifact: Artifact) => {
    if (
      !window.confirm(
        t("runs.detail.deleteArtifactConfirm", { path: artifact.relative_path }),
      )
    )
      return;
    try {
      await api.del("/artifacts/{artifact_id}", { path: { artifact_id: artifact.id } });
      toast.success(t("runs.detail.artifactDeleted"));
      await load();
    } catch (err) {
      toast.error(t("runs.detail.deleteFailed", { error: errorText(err) }));
    }
  };

  if (!run) {
    return error ? <ErrorAlert error={error} title={t("runs.detail.loadFailed")} /> : <Loading />;
  }

  return (
    <div className="grid gap-4">
      <PageTitle title={run.name}>
        {active && (
          <span className="flex items-center gap-1 text-xs text-muted-foreground">
            <Loader2 className="size-3 animate-spin" />
            {t("runs.detail.autoRefresh")}
          </span>
        )}
        <Button size="sm" variant="outline" onClick={() => void load()}>
          <RefreshCw />
          {t("runs.list.refresh")}
        </Button>
        <Button size="sm" variant="outline" disabled={busy} onClick={() => void exportRun()}>
          <Package />
          {t("runs.detail.export")}
        </Button>
        {run.allowed_actions.includes("rerun") && (
          <>
            <Button size="sm" variant="outline" disabled={busy} onClick={() => void rerun("all")}>
              <RotateCcw />
              {t("runs.detail.rerun")}
            </Button>
            {!active && run.counts.scored < run.counts.planned && (
              <Button
                size="sm"
                variant="outline"
                disabled={busy}
                onClick={() => void rerun("unfinished")}
              >
                {t("runs.detail.rerunUnfinished")}
              </Button>
            )}
          </>
        )}
        {run.allowed_actions.includes("modify") && (
          <Button asChild size="sm" variant="outline">
            <Link to={`/runs/new?from=${run.id}`}>
              <PencilLine />
              {t("runs.detail.modify")}
            </Link>
          </Button>
        )}
        {run.allowed_actions.includes("modify") && <AddToCollection runIds={[run.id]} />}
        {run.allowed_actions.includes("delete") && (
          <Button size="sm" variant="outline" disabled={busy} onClick={() => void remove()}>
            <Trash2 />
            {t("runs.artifacts.delete")}
          </Button>
        )}
        {run.allowed_actions.includes("cancel") && (
          <Button
            size="sm"
            variant="destructive"
            disabled={cancelling || run.stop_requested}
            onClick={cancel}
          >
            {run.stop_requested ? t("runs.detail.cancelling") : t("runs.detail.cancel")}
          </Button>
        )}
      </PageTitle>
      {error !== undefined && <ErrorAlert error={error} title={t("runs.refreshFailed")} />}
      {run.deletion && (
        <DeletionBanner
          deletion={run.deletion}
          title={
            run.deletion.state === "purged"
              ? t("runs.deletion.runPurged")
              : t("runs.deletion.runDeleted")
          }
          text={t("runs.deletion.runText")}
        />
      )}
      {run.dataset_version_deletion && (
        <DeletionBanner
          deletion={run.dataset_version_deletion}
          title={
            run.dataset_version_deletion.state === "purged"
              ? t("runs.deletion.versionPurged")
              : t("runs.deletion.versionDeleted")
          }
          text={t("runs.deletion.versionText")}
        />
      )}

      <Card>
        <CardContent className="grid gap-x-6 gap-y-2 pt-6 text-sm sm:grid-cols-2 lg:grid-cols-3">
          <Info label={t("runs.detail.runId")} value={<code>{run.id}</code>} />
          <Info
            label={t("runs.column.owner")}
            value={
              <>
                {run.owner_id === me.id
                  ? t("runs.detail.ownerMe", { username: me.username })
                  : t("runs.detail.ownerOther", {
                      name: run.owner.display_name,
                      username: run.owner.username,
                    })}
                {run.owner.deleted && <span className="text-muted-foreground">{t("runs.ownerDeleted")}</span>}
              </>
            }
          />
          <Info
            label={t("runs.detail.channel")}
            value={run.pat_id ? <>PAT <code>{run.pat_id}</code></> : t("runs.detail.webSession")}
          />
          <Info
            label={t("runs.detail.source")}
            value={
              run.relation === "initial" ? (
                t("runs.detail.initialRun")
              ) : (
                <>
                  {run.relation === "rerun" ? t("runs.detail.rerun") : t("runs.detail.modify")}
                  {run.source_run_id && (
                    <Link className="ml-1 text-primary underline" to={`/runs/${run.source_run_id}`}>
                      {shortId(run.source_run_id)}
                    </Link>
                  )}
                </>
              )
            }
          />
          {run.plan_id && (
            <Info
              label={t("runs.detail.plan")}
              value={
                <>
                  <code>{shortId(run.plan_id)}</code>{" "}
                  {t("runs.detail.planRevision", { revision: run.plan_revision })}
                </>
              }
            />
          )}
          {run.batch_id && (
            <Info
              label={t("runs.batch.title")}
              value={
                <Link className="text-primary underline" to={`/batches/${run.batch_id}`}>
                  {shortId(run.batch_id)}
                </Link>
              }
            />
          )}
          <SourceDifferences snapshot={run.snapshot} />
          <Info
            label={t("runs.detail.datasetVersion")}
            value={
              <Link
                className="text-primary underline"
                to={`/datasets?version=${run.dataset_version_id}`}
              >
                <code>{run.dataset_version_id}</code>
              </Link>
            }
          />
          <Info label="Operation" value={<code>{run.operation_id}</code>} />
          <Info label={t("runs.detail.clientRequestId")} value={run.client_request_id ?? "—"} />
          <Info label={t("runs.column.acceptedAt")} value={fmtTime(run.accepted_at)} />
          <Info label={t("runs.detail.startedAt")} value={fmtTime(run.started_at)} />
          <Info label={t("runs.detail.finishedAt")} value={fmtTime(run.finished_at)} />
          <Info label={t("runs.detail.deadline")} value={fmtTime(run.deadline_at)} />
          <Info label={t("runs.detail.executionMode")} value={<ExecutionMode mode={run.snapshot.effective.execution_mode} />} />
          {run.snapshot.effective.model != null && (
            <Info label={t("runs.detail.modelHarness")} value={<AgentSummary snapshot={run.snapshot} />} />
          )}
          {run.state === "queued" && run.queue_reason && (
            <Info
              label={t("runs.detail.queueReason")}
              value={<span className="text-amber-800">{QUEUE_REASON[run.queue_reason] ?? run.queue_reason}</span>}
            />
          )}
          {active && run.heartbeat_at && (
            <Info label={t("runs.detail.heartbeat")} value={fmtTime(run.heartbeat_at)} />
          )}
          {(run.stop_requested || run.stop_reason) && (
            <Info
              label={t("runs.detail.stop")}
              value={
                <span className="text-amber-800">
                  {run.stop_requested ? t("runs.stopRequested") : ""} {run.stop_reason ?? ""}
                </span>
              }
            />
          )}
        </CardContent>
      </Card>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Dimension title={t("runs.column.lifecycle")} value={label(RUN_STATE, run.state)} raw={run.state} />
        <Dimension
          title={t("runs.column.resultCompleteness")}
          value={label(RESULT_COMPLETENESS, run.result_completeness)}
          raw={run.result_completeness}
        />
        <Dimension title={t("runs.detail.artifactState")} value={label(ARTIFACT_STATE, run.artifact_state)} raw={run.artifact_state} />
        <Dimension title={t("runs.column.cleanup")} value={label(CLEANUP_STATE, run.cleanup_state)} raw={run.cleanup_state} />
      </div>

      <Card>
        <CardContent className="flex flex-wrap gap-x-6 gap-y-2 pt-6 text-sm">
          <Count label={t("runs.count.planned")} value={run.counts.planned} />
          <Count label={t("runs.count.queued")} value={run.counts.queued} />
          <Count label={t("runs.inProgress")} value={run.counts.active} />
          <Count label={t("runs.count.terminal")} value={run.counts.terminal} />
          <Count label={t("runs.count.scored")} value={run.counts.scored} tone="good" />
          <Count label={t("runs.count.errors")} value={run.counts.errors} tone={run.counts.errors ? "bad" : undefined} />
          <Count label={t("runs.count.notStarted")} value={run.counts.not_started} />
          <Count label={t("runs.count.cancelled")} value={run.counts.cancelled} />
          <Count label={t("runs.count.interrupted")} value={run.counts.interrupted} tone={run.counts.interrupted ? "bad" : undefined} />
        </CardContent>
      </Card>

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="execution">{t("runs.detail.tab.execution")}</TabsTrigger>
          <TabsTrigger value="results">{t("runs.summary.title")}</TabsTrigger>
          <TabsTrigger value="config">{t("runs.detail.tab.config")}</TabsTrigger>
          <TabsTrigger value="artifacts">
            {t("runs.column.artifacts")}
            {artifacts ? t("runs.detail.tab.count", { n: artifacts.length }) : ""}
          </TabsTrigger>
        </TabsList>
        <TabsContent value="execution" className="mt-3">
          {!trials && <Loading />}
          {trials && trials.length === 0 && <EmptyNote>{t("runs.detail.noTrials")}</EmptyNote>}
          {trials && trials.length > 0 && (
            <ExecutionView
              trials={trials}
              artifacts={artifacts ?? []}
              live={active}
              onShowArtifacts={(trialId) => {
                const next = new URLSearchParams(params);
                next.set("tab", "artifacts");
                next.set("artifacts_of", trialId);
                setParams(next);
              }}
            />
          )}
        </TabsContent>
        <TabsContent value="results" className="mt-3">
          <ResultSummary summary={summary} error={summaryError} />
        </TabsContent>
        <TabsContent value="config" className="mt-3">
          <Card>
            <CardHeader>
              <CardTitle className="text-base">{t("runs.detail.tab.config")}</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-2 text-sm">
              <div className="grid gap-1 sm:grid-cols-3">
                <Info label="config_digest" value={<Digest value={run.snapshot.config_digest} />} />
                <Info label="snapshot_digest" value={<Digest value={run.snapshot.snapshot_digest} />} />
                <Info
                  label="comparison_digest"
                  value={<Digest value={run.snapshot.comparison_digest} />}
                />
              </div>
              <JsonBlock summary={t("runs.detail.effective")} value={run.snapshot.effective} />
              <JsonBlock summary={t("runs.detail.provenance")} value={run.snapshot.provenance} />
              <JsonBlock summary={t("runs.detail.policy")} value={run.snapshot.policy} />
            </CardContent>
          </Card>
        </TabsContent>
        <TabsContent value="artifacts" className="mt-3">
          {!artifacts && <Loading />}
          {artifacts && artifacts.length === 0 && <EmptyNote>{t("runs.detail.noArtifacts")}</EmptyNote>}
          {artifacts && artifacts.length > 0 && (
            <ArtifactTree
              runId={run.id}
              ended={!active}
              artifacts={artifacts}
              trials={trials ?? []}
              trialId={params.get("artifacts_of") ?? ""}
              onTrialId={(trialId) => {
                const next = new URLSearchParams(params);
                if (trialId) next.set("artifacts_of", trialId);
                else next.delete("artifacts_of");
                setParams(next, { replace: true });
              }}
              onDelete={canDeleteArtifacts ? removeArtifact : undefined}
            />
          )}
        </TabsContent>
      </Tabs>
    </div>
  );
}

function ExecutionMode({ mode }: { mode: unknown }) {
  if (mode === "oracle") {
    return (
      <span title={t("runs.detail.oracleTitle")}>
        <Pill tone="warn">{t("runs.detail.oracle")}</Pill>
      </span>
    );
  }
  if (mode === "agent") return <span>{t("runs.detail.agentMode")}</span>;
  return <span>{mode ? String(mode) : "—"}</span>;
}

/** The model and harness this run froze at acceptance; later edits do not change it. */
/** What a modified run changed, or which tasks a rerun took and why (04 section 7). */
function SourceDifferences({ snapshot }: { snapshot: RunDetail["snapshot"] }) {
  const source = snapshot.provenance.source as
    | {
        differences?: { path: string; before: unknown; after: unknown }[];
        task_causes?: Record<string, string[]>;
      }
    | null
    | undefined;
  if (!source) return null;
  if (source.differences) {
    return (
      <Info
        label={t("runs.detail.differences")}
        value={
          source.differences.length === 0 ? (
            t("runs.none")
          ) : (
            <ul className="text-xs">
              {source.differences.slice(0, 12).map((d) => (
                <li key={d.path}>
                  <code>{d.path}</code>
                  {t("runs.detail.difference", {
                    before: JSON.stringify(d.before),
                    after: JSON.stringify(d.after),
                  })}
                </li>
              ))}
              {source.differences.length > 12 && (
                <li>{t("runs.detail.moreDifferences", { count: source.differences.length })}</li>
              )}
            </ul>
          )
        }
      />
    );
  }
  const causes = Object.values(source.task_causes ?? {});
  return (
    <Info
      label={t("runs.detail.rerunScope")}
      value={t("runs.detail.rerunScopeValue", {
        count: causes.length,
        causes: [...new Set(causes.flat())].join(t("runs.enumSeparator")) || "—",
      })}
    />
  );
}

const INSTALL_STATUS: Record<string, string> = t("runs.installStatus", { returnObjects: true });

function isAttachmentFailure(
  detail: unknown,
): detail is { attachment: string; label?: string; status?: string; exit_code?: number | null } {
  return typeof detail === "object" && detail !== null && "attachment" in detail;
}

function AgentSummary({ snapshot }: { snapshot: RunDetail["snapshot"] }) {
  const { model, harness, attachments, bootstrap_timeout_sec: installSec } = snapshot.effective;
  const labels = snapshot.provenance.labels;
  if (!model) return <span>—</span>;
  return (
    <span>
      <Trans
        i18nKey="runs.detail.agentSummary"
        values={{
          label: labels.model ?? model.model_id,
          modelId: model.model_id,
          provider: model.provider,
          revision: model.revision,
          harness: harness ? `${harness.name} ${harness.version}` : "—",
        }}
        components={{ code: <code /> }}
      />
      {attachments.length > 0 && (
        <span className="block text-xs">
          {t("runs.detail.attachments")}
          {attachments.map((a, i) => (
            <span key={a.version_id} title={a.content_digest}>
              {i > 0 && " → "}
              {a.name} <code>{snapshotName(a)}</code>
            </span>
          ))}
          {installSec != null && t("runs.detail.installTimeout", { count: installSec })}
        </span>
      )}
    </span>
  );
}

function Info({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="break-all">{value}</div>
    </div>
  );
}

function Digest({ value }: { value: string }) {
  return (
    <code className="text-xs" title={value}>
      {shortId(value, 24)}
    </code>
  );
}

function Dimension({ title, value, raw }: { title: string; value: [string, Tone]; raw: string }) {
  return (
    <Card>
      <CardContent className="grid gap-1 pt-4 pb-4">
        <div className="text-xs text-muted-foreground">{title}</div>
        <div className="flex items-center gap-2">
          <StatusPill value={value} />
          <code className="text-xs text-muted-foreground">{raw}</code>
        </div>
      </CardContent>
    </Card>
  );
}

function Count({ label, value, tone }: { label: string; value: number; tone?: "good" | "bad" }) {
  return (
    <div>
      <span className="text-muted-foreground">{label} </span>
      <span
        className={cn(
          "font-semibold tabular-nums",
          tone === "good" && "text-emerald-700",
          tone === "bad" && "text-red-700",
        )}
      >
        {value}
      </span>
    </div>
  );
}

/**
 * Score cell. Three outcomes stay distinct: a valid score (0 is a real zero), a missing
 * score (the verifier gave no usable reward) and an execution failure. Never render null as 0.
 */
function ScoreCell({ trial }: { trial: Trial }) {
  const result = trial.result;
  const score = result?.score;
  if (score !== null && score !== undefined) {
    return (
      <span className="tabular-nums">
        <span className="font-semibold">{String(score)}</span>
        {score === 0 && <span className="ml-1 text-xs text-muted-foreground">{t("runs.score.validZero")}</span>}
      </span>
    );
  }
  if (!TRIAL_TERMINAL.has(trial.state)) {
    return <span className="text-muted-foreground">{t("runs.inProgress")}</span>;
  }
  const reason = trial.reason_code ?? result?.failure_cause ?? result?.exception_type;
  const invalidRaw =
    result && !result.reward_valid && result.raw_score !== null
      ? t("runs.score.invalidRaw", { raw: String(result.raw_score) })
      : null;
  let verdict: [string, Tone];
  if (trial.state !== "execution_error" && trial.state !== "scored") {
    verdict = [t("runs.score.unfinished"), "muted"];
  } else if (
    result?.failure_cause === "verifier_failed" ||
    (result != null && result.verifier_status !== "not_run" && result.failure_cause == null)
  ) {
    verdict = [t("runs.score.missing"), "warn"];
  } else {
    verdict = [t("runs.score.failed"), "bad"];
  }
  return (
    <span className="flex flex-wrap items-center gap-1">
      <StatusPill value={verdict} />
      {reason && <span className="text-xs text-muted-foreground">{reason}</span>}
      {invalidRaw && <span className="text-xs text-muted-foreground">{invalidRaw}</span>}
    </span>
  );
}

const TRIAL_ACTIVE = new Set(["preparing", "executing", "verifying"]);

function trialIcon(state: string) {
  switch (state) {
    case "scored":
      return <CircleCheck className="size-4 shrink-0 text-emerald-600" />;
    case "execution_error":
    case "interrupted":
      return <CircleX className="size-4 shrink-0 text-red-600" />;
    case "cancelled":
    case "not_started":
      return <CircleSlash className="size-4 shrink-0 text-amber-600" />;
    case "queued":
      return <CircleDashed className="size-4 shrink-0 text-muted-foreground" />;
    default:
      return <Loader2 className="size-4 shrink-0 animate-spin text-sky-600" />;
  }
}

/** From the first attempt's start to the last attempt's end (or now, while running). */
function trialDuration(trial: Trial): string {
  const first = trial.attempts[0];
  if (!first) return "";
  const last = trial.attempts[trial.attempts.length - 1];
  if (!last.ended_at) return t("runs.inProgress");
  return fmtDuration(new Date(last.ended_at).getTime() - new Date(first.started_at).getTime());
}

/**
 * The GitHub Actions-like execution page (U-07): trials on the left like jobs, the selected
 * trial's attempts and steps on the right, each step expandable to its live log. The
 * selected trial is in the URL (?trial=) so a refresh or a shared link keeps it.
 */
function ExecutionView({
  trials,
  artifacts,
  live,
  onShowArtifacts,
}: {
  trials: Trial[];
  artifacts: Artifact[];
  live: boolean;
  onShowArtifacts: (trialId: string) => void;
}) {
  const [params, setParams] = useSearchParams();
  const chosen = trials.find((t) => t.id === params.get("trial"));
  const selected = chosen ?? trials.find((t) => TRIAL_ACTIVE.has(t.state)) ?? trials[0];
  const select = (id: string) => {
    const next = new URLSearchParams(params);
    next.set("trial", id);
    setParams(next, { replace: true });
  };
  const own = artifacts.filter((a) => a.trial_id === selected.id);
  const prefix = commonPrefix(trials.map((t) => t.task_key));
  return (
    <div className="grid items-start gap-4 md:grid-cols-[minmax(14rem,20rem)_minmax(0,1fr)]">
      <nav aria-label={t("runs.artifacts.trialFilter")} className="grid gap-0.5 rounded-md border p-1">
        {trials.map((trial) => (
          <button
            key={trial.id}
            type="button"
            data-testid="trial-row"
            aria-current={trial.id === selected.id ? "true" : undefined}
            onClick={() => select(trial.id)}
            className={cn(
              "grid gap-0.5 rounded px-2 py-1.5 text-left text-sm hover:bg-muted",
              trial.id === selected.id && "bg-muted",
            )}
          >
            <span className="flex items-center gap-2">
              {trialIcon(trial.state)}
              <LongName
                value={trial.task_key}
                prefix={prefix}
                copy={false}
                className="font-mono text-xs"
              />
              <span className="text-xs text-muted-foreground">#{trial.repetition_index}</span>
              <span className="ml-auto text-xs tabular-nums text-muted-foreground">
                {trialDuration(trial)}
              </span>
            </span>
            <span className="pl-6 text-xs" data-testid="trial-score">
              <ScoreCell trial={trial} />
            </span>
          </button>
        ))}
      </nav>
      <Card data-testid="trial-panel" className="min-w-0">
        <CardHeader className="gap-1">
          <CardTitle className="flex flex-wrap items-center gap-2 text-base">
            {trialIcon(selected.state)}
            <LongName value={selected.task_key} wrap className="font-mono" />
            <span className="text-sm font-normal text-muted-foreground">
              {t("runs.execution.repetition", { index: selected.repetition_index })}
            </span>
            <StatusPill value={label(TRIAL_STATE, selected.state)} />
            {selected.phase && !TRIAL_TERMINAL.has(selected.state) && (
              <span className="text-xs font-normal text-muted-foreground">
                {STEP_LABEL[selected.phase] ?? selected.phase}
              </span>
            )}
          </CardTitle>
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
            <span>
              {t("runs.execution.score")}
              <ScoreCell trial={selected} />
            </span>
            <span>
              {t("runs.execution.artifacts")}
              <ArtifactCounts counts={selected.artifact_counts} state={selected.artifact_state} />
            </span>
            <span>
              {t("runs.execution.cleanup", {
                state: label(CLEANUP_STATE, selected.cleanup_state)[0],
              })}
            </span>
            {selected.result && (
              <span>
                {t("runs.execution.result", {
                  verifier: selected.result.verifier_status ?? "—",
                  raw: selected.result.raw_score ?? t("runs.none"),
                  valid: String(selected.result.reward_valid),
                  policy: selected.result.policy_version,
                })}
              </span>
            )}
          </div>
        </CardHeader>
        <CardContent className="grid gap-2">
          <AttemptSteps key={selected.id} trial={selected} artifacts={own} live={live} />
          {own.length > 0 && (
            <div>
              <Button size="sm" variant="ghost" onClick={() => onShowArtifacts(selected.id)}>
                {t("runs.execution.artifactCount", { count: own.length })}
                <ChevronRight />
              </Button>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function stepIcon(state: string) {
  switch (state) {
    case "completed":
      return <CircleCheck className="size-4 text-emerald-600" />;
    case "failed":
    case "interrupted":
      return <CircleX className="size-4 text-red-600" />;
    case "cancelled":
      return <CircleSlash className="size-4 text-amber-600" />;
    case "starting":
    case "preparing":
    case "executing":
    case "verifying":
      return <Loader2 className="size-4 animate-spin text-sky-600" />;
    default:
      return <CircleDashed className="size-4 text-muted-foreground" />;
  }
}

function duration(attempt: Attempt): string {
  if (!attempt.ended_at) return t("runs.inProgress");
  return fmtDuration(new Date(attempt.ended_at).getTime() - new Date(attempt.started_at).getTime());
}

/** Which live logs belong to a step: Harbor writes agent and verifier output separately. */
const STEP_LOG_PREFIX: Record<string, string> = {
  installing: "agent/attachments/",
  agent: "agent/",
  verifying: "verifier/",
};
const INSTALL_LOGS = STEP_LOG_PREFIX.installing;
const ENDED_BADLY = new Set(["failed", "interrupted", "cancelled"]);

function AttemptSteps({
  trial,
  artifacts,
  live,
}: {
  trial: Trial;
  artifacts: Artifact[];
  live: boolean;
}) {
  if (trial.attempts.length === 0) {
    return <div className="text-xs text-muted-foreground">{t("runs.execution.noAttempts")}</div>;
  }
  return (
    <ol className="grid gap-2">
      {trial.attempts.map((attempt) => {
        const err = attempt.error;
        return (
          <li key={attempt.id} className="rounded-md border bg-background px-3 py-2">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              {stepIcon(attempt.state)}
              <span className="font-medium">
                {t("runs.execution.attempt", { index: attempt.attempt_index })}
              </span>
              <StatusPill value={label(ATTEMPT_STATE, attempt.state)} />
              <span className="ml-auto text-xs text-muted-foreground">
                {fmtTime(attempt.started_at)} · {duration(attempt)}
              </span>
            </div>
            <StepList
              attempt={attempt}
              artifacts={artifacts.filter((a) => a.attempt_id === attempt.id)}
              live={live}
            />
            {attempt.setup_network_revision != null && (
              <SetupNetworkRevision revision={attempt.setup_network_revision} />
            )}
            {err && (
              <div className="mt-1 rounded bg-red-50 px-2 py-1 font-mono text-xs text-red-800">
                {String(err.code ?? "ERROR")}
                {err.exception_type ? ` · ${String(err.exception_type)}` : ""}
                {err.message ? t("runs.execution.errorMessage", { message: String(err.message) }) : ""}
                {isAttachmentFailure(err.detail) &&
                  t("runs.execution.attachmentError", {
                    attachment: err.detail.attachment,
                    label: err.detail.label ?? "",
                    status: INSTALL_STATUS[err.detail.status ?? ""] ?? err.detail.status,
                    exit:
                      err.detail.exit_code != null
                        ? t("runs.execution.exitCode", { code: err.detail.exit_code })
                        : "",
                  })}
              </div>
            )}
          </li>
        );
      })}
    </ol>
  );
}

/** GitHub Actions-like steps: each with its duration; steps with logs expand in place. */
function StepList({
  attempt,
  artifacts,
  live,
}: {
  attempt: Attempt;
  artifacts: Artifact[];
  live: boolean;
}) {
  // undefined: follow the running step (its log opens by itself); null: all closed by the user.
  const [open, setOpen] = useState<string | null | undefined>(undefined);
  const steps = attempt.steps;
  if (steps.length === 0) return null;
  return (
    <ol className="mt-2 grid gap-0.5 border-l pl-3" data-testid="attempt-steps">
      {steps.map((step, index) => {
        const next = steps[index + 1]?.at ?? attempt.ended_at;
        const last = index === steps.length - 1;
        const running = !next;
        const failed = last && ENDED_BADLY.has(attempt.state);
        const prefix = STEP_LOG_PREFIX[step.step];
        // Only what was forwarded live: for a harness that is its trajectory rendered as
        // text; the raw event stream and trajectory.json stay in the artifacts tab.
        const logs = prefix
          ? artifacts.filter(
              (a) =>
                a.kind === "log" &&
                a.relative_path.startsWith(prefix) &&
                // Attachment install logs belong to their own step, not the agent's.
                (step.step === "installing" || !a.relative_path.startsWith(INSTALL_LOGS)),
            )
          : [];
        const key = `${step.step}-${index}`;
        const expanded = open === undefined ? running && logs.length > 0 && live : open === key;
        return (
          <li key={key}>
            <button
              type="button"
              className={cn(
                "flex w-full items-center gap-2 rounded px-1 py-0.5 text-left text-xs",
                logs.length > 0 && "hover:bg-muted",
              )}
              disabled={logs.length === 0}
              aria-expanded={logs.length > 0 ? expanded : undefined}
              onClick={() => setOpen(expanded ? null : key)}
            >
              {running ? (
                <Loader2 className="size-3.5 animate-spin text-sky-600" />
              ) : failed ? (
                <CircleX className="size-3.5 text-red-600" />
              ) : (
                <CircleCheck className="size-3.5 text-emerald-600" />
              )}
              <span className="font-medium">{STEP_LABEL[step.step] ?? step.step}</span>
              {logs.length > 0 &&
                (expanded ? <ChevronDown className="size-3" /> : <ChevronRight className="size-3" />)}
              <span className="ml-auto tabular-nums text-muted-foreground">
                {running
                  ? t("runs.inProgress")
                  : fmtDuration(new Date(next).getTime() - new Date(step.at).getTime())}
              </span>
            </button>
            {expanded &&
              logs.map((log) => <LogView key={log.id} artifact={log} live={live && running} />)}
          </li>
        );
      })}
    </ol>
  );
}

const LOG_TAIL_LINES = 400;
/** Logs larger than this are read as a bounded tail slice (the API caps a slice at 1 MiB). */
const LOG_TAIL_BYTES = 256 * 1024;

/** Live log tail: refetched whenever the stored size grows (the page polls while active). */
function LogView({ artifact, live }: { artifact: Artifact; live: boolean }) {
  const [text, setText] = useState<string>();
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let stale = false;
    const size = artifact.size_bytes ?? 0;
    const sliced = size > LOG_TAIL_BYTES;
    const query = sliced ? `?offset=${size - LOG_TAIL_BYTES}&length=${LOG_TAIL_BYTES}` : "";
    fetch(`${API}/artifacts/${artifact.id}/download${query}`, { credentials: "same-origin" })
      .then((res) => (res.ok ? res.text() : Promise.reject(new Error(String(res.status)))))
      .then((body) => {
        if (!stale) {
          // A slice may start mid-line; drop the partial first line.
          setText(sliced ? body.slice(body.indexOf("\n") + 1) : body);
          setFailed(false);
        }
      })
      .catch(() => !stale && setFailed(true));
    return () => {
      stale = true;
    };
  }, [artifact.id, artifact.size_bytes]);
  const lines = text?.split("\n") ?? [];
  const shown = lines.length > LOG_TAIL_LINES ? lines.slice(-LOG_TAIL_LINES) : lines;
  const pre = useRef<HTMLPreElement>(null);
  useEffect(() => {
    if (live && pre.current) pre.current.scrollTop = pre.current.scrollHeight;
  }, [live, text]);
  return (
    <div className="my-1 ml-5 min-w-0">
      <div className="flex min-w-0 flex-wrap items-center gap-x-2 text-xs text-muted-foreground">
        <code className="break-all">{artifact.relative_path}</code>
        <span>{fmtBytes(artifact.size_bytes)}</span>
        <a
          className="inline-flex items-center gap-1 text-primary hover:underline"
          href={`${API}/artifacts/${artifact.id}/download`}
        >
          <Download className="size-3" />
          {t("runs.execution.download")}
        </a>
        {live && <Loader2 className="size-3 animate-spin" />}
        {lines.length > LOG_TAIL_LINES && (
          <span>{t("runs.execution.logTail", { count: LOG_TAIL_LINES })}</span>
        )}
      </div>
      {failed && <div className="text-xs text-red-700">{t("runs.execution.logFailed")}</div>}
      {text !== undefined && (
        <pre
          ref={pre}
          data-testid="step-log"
          className="max-h-96 overflow-auto rounded bg-zinc-950 p-2 font-mono text-xs leading-relaxed text-zinc-100"
        >
          {shown.join("\n") || t("runs.execution.logEmpty")}
        </pre>
      )}
    </div>
  );
}

function DeletionBanner({
  deletion,
  title,
  text,
}: {
  deletion: DeletionState;
  title: string;
  text: string;
}) {
  return (
    <Alert className="border-amber-400 bg-amber-50" data-testid="deletion-banner">
      <Trash2 className="size-4 text-amber-700" />
      <AlertTitle className="text-amber-900">{title}</AlertTitle>
      <AlertDescription className="text-amber-900">
        {t("runs.deletion.deletedAt", { time: fmtTime(deletion.deleted_at) })}
        {deletion.purged_at && t("runs.deletion.purgedAt", { time: fmtTime(deletion.purged_at) })}
        {t("runs.deletion.end")}
        {text}
      </AlertDescription>
    </Alert>
  );
}

function ArtifactCounts({
  counts,
  state,
}: {
  counts: Trial["artifact_counts"];
  state: string;
}) {
  const parts: ReactNode[] = [];
  if (counts.available) parts.push(<span key="a">{t("runs.execution.available", { n: counts.available })}</span>);
  if (counts.expired) parts.push(<span key="e" className="text-amber-700">
          {t("runs.execution.expired", { n: counts.expired })}
        </span>);
  if (counts.failed) parts.push(<span key="f" className="text-red-700">
          {t("runs.execution.failed", { n: counts.failed })}
        </span>);
  if (parts.length === 0) return <>{label(ARTIFACT_STATE, state)[0]}</>;
  return (
    <span className="inline-flex gap-1" title={label(ARTIFACT_STATE, state)[0]}>
      {parts}
    </span>
  );
}

const DIRECTION: Record<string, string> = t("runs.summary.direction", { returnObjects: true });

function passRule(summary: RunSummary): string {
  const rule = summary.contract.passed;
  if (!rule) return t("runs.summary.noPassRule");
  if (rule.rule === "threshold" && typeof rule.threshold === "number") {
    const op = summary.contract.direction === "lower_is_better" ? "≤" : "≥";
    return t("runs.summary.passRule", { op, threshold: rule.threshold });
  }
  return JSON.stringify(rule);
}

/** 06 §5 result summary: counts, statistics with their coverage, and per-task rows. */
function ResultSummary({ summary, error }: { summary?: RunSummary; error: unknown }) {
  if (!summary) {
    return error ? <ErrorAlert error={error} title={t("runs.summary.loadFailed")} /> : null;
  }
  const c = summary.contract;
  const causes = Object.entries(summary.excluded_by_cause);
  const summaryPrefix = commonPrefix(summary.tasks.map((t) => t.task_key));
  return (
    <Card data-testid="result-summary">
      <CardHeader>
        <CardTitle className="text-base">{t("runs.summary.title")}</CardTitle>
        <div className="text-xs text-muted-foreground">
          <Trans
            i18nKey="runs.summary.contract"
            values={{
              name: c.name,
              metricId: c.metric_id,
              unit: c.unit ? t("runs.summary.unit", { unit: c.unit }) : "",
              direction: DIRECTION[c.direction] ?? c.direction,
              source:
                c.source === "dataset_version"
                  ? t("runs.summary.sourceDataset")
                  : t("runs.summary.sourceDefault"),
              rule: passRule(summary),
            }}
            components={{ code: <code /> }}
          />
        </div>
      </CardHeader>
      <CardContent className="grid gap-4 text-sm">
        <div className="flex flex-wrap gap-x-6 gap-y-2">
          <Count label={t("runs.count.planned")} value={summary.planned_trials} />
          <Count label={t("runs.count.scored")} value={summary.scored_trials} tone="good" />
          <Count
            label={t("runs.summary.excludedErrors")}
            value={summary.excluded_error_trials}
            tone={summary.excluded_error_trials ? "bad" : undefined}
          />
          {summary.count_as_failure_trials > 0 && (
            <Count label={t("runs.summary.countAsFailure")} value={summary.count_as_failure_trials} tone="bad" />
          )}
          <Count label={t("runs.score.unfinished")} value={summary.unfinished_trials} />
          {summary.active_trials > 0 && <Count label={t("runs.inProgress")} value={summary.active_trials} />}
          {summary.queued_trials > 0 && <Count label={t("runs.count.queued")} value={summary.queued_trials} />}
          {summary.passed_trials !== null && <Count label={t("runs.summary.passed")} value={summary.passed_trials} />}
        </div>
        <div className="grid gap-x-6 gap-y-2 sm:grid-cols-3 lg:grid-cols-5">
          <Info
            label={t("runs.summary.mean")}
            value={
              <span className="tabular-nums">
                <b>{fmtNum(summary.mean)}</b>
                <span className="ml-1 text-xs text-muted-foreground">
                  {t("runs.summary.scoredOf", {
                    scored: summary.scored_trials,
                    planned: summary.planned_trials,
                  })}
                </span>
              </span>
            }
          />
          <Info label={t("runs.summary.stddev")} value={<span className="tabular-nums">{fmtNum(summary.stddev)}</span>} />
          <Info
            label={t("runs.summary.passRate")}
            value={
              <span className="tabular-nums">
                {fmtPct(summary.pass_rate)}
                {summary.passed_trials !== null && (
                  <span className="ml-1 text-xs text-muted-foreground">
                    {summary.passed_trials}/{summary.scored_trials}
                  </span>
                )}
              </span>
            }
          />
          <Info
            label={t("runs.summary.coverage")}
            value={
              <span className="tabular-nums" title={t("runs.summary.coverageTitle")}>
                {fmtPct(summary.scored_coverage)} / {fmtPct(summary.evaluation_coverage)}
              </span>
            }
          />
          <Info
            label={t("runs.summary.benchScore")}
            value={
              <span className="tabular-nums">
                <b>{fmtNum(summary.bench_score)}</b>
                <span className="ml-1 text-xs text-muted-foreground">
                  {t("runs.summary.scoredTasks", {
                    scored: summary.bench_scored_tasks,
                    total: summary.task_count,
                  })}
                  {summary.bench_task_coverage != null &&
                    t("runs.summary.benchCoverage", { pct: fmtPct(summary.bench_task_coverage) })}
                </span>
              </span>
            }
          />
          <Info
            label={t("runs.summary.attemptsRetries")}
            value={
              <span className="tabular-nums">
                {t("runs.summary.attemptsValue", {
                  attempts: summary.attempts,
                  retries: summary.retry_count,
                  trials: summary.retried_trials,
                })}
              </span>
            }
          />
          <Info label={t("runs.summary.completeness")} value={<StatusPill value={label(RESULT_COMPLETENESS, summary.result_completeness)} />} />
          <Info
            label={t("runs.summary.failureCauses")}
            value={
              causes.length === 0 ? (
                "—"
              ) : (
                <span className="flex flex-wrap gap-1">
                  {causes.map(([cause, n]) => (
                    <Pill key={cause} tone="bad">
                      {cause} × {n}
                    </Pill>
                  ))}
                </span>
              )
            }
          />
          <Info
            label={t("runs.summary.usage")}
            value={
              <span className="text-xs">
                {t("runs.summary.usageValue", {
                  known: summary.usage.known_trials,
                  unknown: summary.usage.unknown_trials,
                })}
                {Object.keys(summary.usage.known_totals).length > 0 &&
                  t("runs.summary.usageTotals", {
                    totals: Object.entries(summary.usage.known_totals)
                      .map(([k, v]) => `${k} ${fmtNum(v)}`)
                      .join(t("runs.commaSeparator")),
                  })}
              </span>
            }
          />
        </div>
        {summary.tasks.length > 0 && (
          <div className="rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t("runs.summary.task")}</TableHead>
                  <TableHead className="text-right">{t("runs.summary.scoredPlanned")}</TableHead>
                  <TableHead className="text-right">{t("runs.summary.meanShort")}</TableHead>
                  <TableHead className="text-right">{t("runs.summary.stddev")}</TableHead>
                  <TableHead className="text-right">{t("runs.summary.passed")}</TableHead>
                  <TableHead>{t("runs.score.failed")}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {summary.tasks.map((task) => (
                  <TableRow key={task.task_key} data-testid="task-summary-row">
                    <TableCell className="max-w-[24rem] font-mono text-xs">
                      <LongName value={task.task_key} prefix={summaryPrefix} />
                      {task.partial && (
                        <span className="ml-2" title={t("runs.summary.partialTitle")}>
                          <Pill tone="warn">{t("runs.summary.partial")}</Pill>
                        </span>
                      )}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {task.scored_trials} / {task.planned_trials}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">{fmtNum(task.mean)}</TableCell>
                    <TableCell className="text-right tabular-nums">{fmtNum(task.stddev)}</TableCell>
                    <TableCell className="text-right tabular-nums">{task.passed_trials ?? "—"}</TableCell>
                    <TableCell className="text-xs">
                      {task.excluded_trials > 0
                        ? t("runs.summary.excludedValue", {
                            count: task.excluded_trials,
                            causes: task.failure_causes.join(t("runs.enumSeparator")) || "—",
                          })
                        : "—"}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
