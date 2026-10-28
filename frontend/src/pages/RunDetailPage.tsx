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
import { API, api, errorText } from "@/lib/api";
import {
  ARTIFACT_REASON,
  ARTIFACT_STATE,
  ARTIFACT_STATUS,
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
      const [r, t, a] = await Promise.all([
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
      setTrials(t.data);
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
    if (!window.confirm("确定取消该运行？已获得的评分结果会保留，未完成的试次将停止。")) return;
    setCancelling(true);
    try {
      const res = await api.post("/runs/{run_id}/cancel", { path: { run_id: runId } });
      toast.success(res.data.stop_requested ? "已请求取消" : "已提交取消");
      await load();
    } catch (err) {
      toast.error(`取消失败：${errorText(err)}`);
    } finally {
      setCancelling(false);
    }
  };

  const rerun = async (mode: "all" | "unfinished") => {
    const text =
      mode === "all"
        ? "按原配置重跑全部任务？会创建一个新运行，原运行不受影响。"
        : "只重跑有试次未得分的任务（这些任务的全部重复次数都会重跑）？会创建一个新运行。";
    if (!window.confirm(text)) return;
    setBusy(true);
    try {
      const res = await api.post("/runs/{run_id}/rerun", {
        path: { run_id: runId },
        body: { task_selection: { mode } },
        headers: { "Idempotency-Key": crypto.randomUUID() },
      });
      toast.success(`已受理重跑：计划 ${res.data.planned_trials} 个试次`);
      navigate(`/runs/${res.data.run_id}`);
    } catch (err) {
      toast.error(`重跑失败：${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  };

  const exportRun = async () => {
    setBusy(true);
    try {
      await api.post("/exports", { body: { run_ids: [runId] } });
      toast.success("已创建导出，正在打包");
      navigate("/exports");
    } catch (err) {
      toast.error(`导出失败：${errorText(err)}`);
      setBusy(false);
    }
  };

  const remove = async () => {
    if (
      !window.confirm(
        "删除该运行？它会移入回收站，从运行列表中隐藏；只有管理员可以从回收站恢复或彻底删除。",
      )
    )
      return;
    setBusy(true);
    try {
      await api.del("/runs/{run_id}", { path: { run_id: runId } });
      toast.success("已移入回收站");
      await load();
    } catch (err) {
      toast.error(`删除失败：${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  };

  const removeArtifact = async (artifact: Artifact) => {
    if (
      !window.confirm(
        `删除产物「${artifact.relative_path}」？文件内容将移入回收站，之后无法下载；索引记录保留。`,
      )
    )
      return;
    try {
      await api.del("/artifacts/{artifact_id}", { path: { artifact_id: artifact.id } });
      toast.success("产物已删除");
      await load();
    } catch (err) {
      toast.error(`删除失败：${errorText(err)}`);
    }
  };

  if (!run) {
    return error ? <ErrorAlert error={error} title="无法加载运行" /> : <Loading />;
  }

  return (
    <div className="grid gap-4">
      <PageTitle title={run.name}>
        {active && (
          <span className="flex items-center gap-1 text-xs text-muted-foreground">
            <Loader2 className="size-3 animate-spin" />
            每 2 秒自动刷新
          </span>
        )}
        <Button size="sm" variant="outline" onClick={() => void load()}>
          <RefreshCw />
          刷新
        </Button>
        <Button size="sm" variant="outline" disabled={busy} onClick={() => void exportRun()}>
          <Package />
          导出
        </Button>
        {run.allowed_actions.includes("rerun") && (
          <>
            <Button size="sm" variant="outline" disabled={busy} onClick={() => void rerun("all")}>
              <RotateCcw />
              重跑
            </Button>
            {!active && run.counts.scored < run.counts.planned && (
              <Button
                size="sm"
                variant="outline"
                disabled={busy}
                onClick={() => void rerun("unfinished")}
              >
                重跑未得分的任务
              </Button>
            )}
          </>
        )}
        {run.allowed_actions.includes("modify") && (
          <Button asChild size="sm" variant="outline">
            <Link to={`/runs/new?from=${run.id}`}>
              <PencilLine />
              修改后运行
            </Link>
          </Button>
        )}
        {run.allowed_actions.includes("modify") && <AddToCollection runId={run.id} />}
        {run.allowed_actions.includes("delete") && (
          <Button size="sm" variant="outline" disabled={busy} onClick={() => void remove()}>
            <Trash2 />
            删除
          </Button>
        )}
        {run.allowed_actions.includes("cancel") && (
          <Button
            size="sm"
            variant="destructive"
            disabled={cancelling || run.stop_requested}
            onClick={cancel}
          >
            {run.stop_requested ? "取消中…" : "取消运行"}
          </Button>
        )}
      </PageTitle>
      {error !== undefined && <ErrorAlert error={error} title="刷新失败（显示的是上次数据）" />}
      {run.deletion && (
        <DeletionBanner
          deletion={run.deletion}
          title={run.deletion.state === "purged" ? "该运行已彻底删除" : "该运行已删除，在回收站中"}
          text="它不再出现在运行列表中；只有管理员可以在回收站中恢复。"
        />
      )}
      {run.dataset_version_deletion && (
        <DeletionBanner
          deletion={run.dataset_version_deletion}
          title={
            run.dataset_version_deletion.state === "purged"
              ? "所用数据集版本已彻底删除"
              : "所用数据集版本已删除，在回收站中"
          }
          text="本运行的记录与结果保留，但不能再用该版本重跑。"
        />
      )}

      <Card>
        <CardContent className="grid gap-x-6 gap-y-2 pt-6 text-sm sm:grid-cols-2 lg:grid-cols-3">
          <Info label="运行 ID" value={<code>{run.id}</code>} />
          <Info
            label="提交者"
            value={
              <>
                {run.owner_id === me.id
                  ? `我（${me.username}）`
                  : `${run.owner.display_name}（${run.owner.username}）`}
                {run.owner.deleted && <span className="text-muted-foreground">（已删除）</span>}
              </>
            }
          />
          <Info label="提交方式" value={run.pat_id ? <>PAT <code>{run.pat_id}</code></> : "网页会话"} />
          <Info
            label="来源"
            value={
              run.relation === "initial" ? (
                "首次运行"
              ) : (
                <>
                  {run.relation === "rerun" ? "重跑" : "修改后运行"}
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
              label="实验方案"
              value={
                <>
                  <code>{shortId(run.plan_id)}</code> · 修订 {run.plan_revision}
                </>
              }
            />
          )}
          {run.batch_id && (
            <Info
              label="批次"
              value={
                <Link className="text-primary underline" to={`/batches/${run.batch_id}`}>
                  {shortId(run.batch_id)}
                </Link>
              }
            />
          )}
          <SourceDifferences snapshot={run.snapshot} />
          <Info label="数据集版本" value={<code>{run.dataset_version_id}</code>} />
          <Info label="Operation" value={<code>{run.operation_id}</code>} />
          <Info label="客户端请求标识" value={run.client_request_id ?? "—"} />
          <Info label="受理时间" value={fmtTime(run.accepted_at)} />
          <Info label="开始时间" value={fmtTime(run.started_at)} />
          <Info label="结束时间" value={fmtTime(run.finished_at)} />
          <Info label="总时限" value={fmtTime(run.deadline_at)} />
          <Info label="执行方式" value={<ExecutionMode mode={run.snapshot.effective.execution_mode} />} />
          {run.snapshot.effective.model != null && (
            <Info label="模型与 Harness" value={<AgentSummary snapshot={run.snapshot} />} />
          )}
          {run.state === "queued" && run.queue_reason && (
            <Info
              label="等待原因"
              value={<span className="text-amber-800">{QUEUE_REASON[run.queue_reason] ?? run.queue_reason}</span>}
            />
          )}
          {active && run.heartbeat_at && (
            <Info label="执行进程心跳" value={fmtTime(run.heartbeat_at)} />
          )}
          {(run.stop_requested || run.stop_reason) && (
            <Info
              label="停止"
              value={
                <span className="text-amber-800">
                  {run.stop_requested ? "已请求停止" : ""} {run.stop_reason ?? ""}
                </span>
              }
            />
          )}
        </CardContent>
      </Card>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Dimension title="生命周期" value={label(RUN_STATE, run.state)} raw={run.state} />
        <Dimension
          title="评分覆盖"
          value={label(RESULT_COMPLETENESS, run.result_completeness)}
          raw={run.result_completeness}
        />
        <Dimension title="产物保存" value={label(ARTIFACT_STATE, run.artifact_state)} raw={run.artifact_state} />
        <Dimension title="环境清理" value={label(CLEANUP_STATE, run.cleanup_state)} raw={run.cleanup_state} />
      </div>

      <Card>
        <CardContent className="flex flex-wrap gap-x-6 gap-y-2 pt-6 text-sm">
          <Count label="计划" value={run.counts.planned} />
          <Count label="排队" value={run.counts.queued} />
          <Count label="进行中" value={run.counts.active} />
          <Count label="已终结" value={run.counts.terminal} />
          <Count label="已评分" value={run.counts.scored} tone="good" />
          <Count label="执行错误" value={run.counts.errors} tone={run.counts.errors ? "bad" : undefined} />
          <Count label="未开始" value={run.counts.not_started} />
          <Count label="已取消" value={run.counts.cancelled} />
          <Count label="已中断" value={run.counts.interrupted} tone={run.counts.interrupted ? "bad" : undefined} />
        </CardContent>
      </Card>

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList>
          <TabsTrigger value="execution">执行</TabsTrigger>
          <TabsTrigger value="results">结果汇总</TabsTrigger>
          <TabsTrigger value="config">配置快照</TabsTrigger>
          <TabsTrigger value="artifacts">
            产物{artifacts ? `（${artifacts.length}）` : ""}
          </TabsTrigger>
        </TabsList>
        <TabsContent value="execution" className="mt-3">
          {!trials && <Loading />}
          {trials && trials.length === 0 && <EmptyNote>没有试次。</EmptyNote>}
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
              <CardTitle className="text-base">配置快照</CardTitle>
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
              <JsonBlock summary="生效配置（effective）" value={run.snapshot.effective} />
              <JsonBlock summary="来源（provenance）" value={run.snapshot.provenance} />
              <JsonBlock summary="策略（policy）" value={run.snapshot.policy} />
            </CardContent>
          </Card>
        </TabsContent>
        <TabsContent value="artifacts" className="mt-3">
          {!artifacts && <Loading />}
          {artifacts && artifacts.length === 0 && <EmptyNote>暂无产物。</EmptyNote>}
          {artifacts && artifacts.length > 0 && (
            <ArtifactTable
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
      <span title="执行任务自带的参考解法，不调用模型；结果不进入模型能力比较">
        <Pill tone="warn">测试模式（Oracle）</Pill>
      </span>
    );
  }
  if (mode === "agent") return <span>模型实验</span>;
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
        label="与原运行的差异"
        value={
          source.differences.length === 0 ? (
            "无"
          ) : (
            <ul className="text-xs">
              {source.differences.slice(0, 12).map((d) => (
                <li key={d.path}>
                  <code>{d.path}</code>：{JSON.stringify(d.before)} → {JSON.stringify(d.after)}
                </li>
              ))}
              {source.differences.length > 12 && <li>…共 {source.differences.length} 处</li>}
            </ul>
          )
        }
      />
    );
  }
  const causes = Object.values(source.task_causes ?? {});
  return (
    <Info
      label="重跑范围"
      value={`${causes.length} 个任务；原结果：${[...new Set(causes.flat())].join("、") || "—"}`}
    />
  );
}

const INSTALL_STATUS: Record<string, string> = {
  failed: "安装脚本失败",
  timeout: "安装超时",
  transfer_mismatch: "复制后内容不符",
  transfer_failed: "复制失败",
};

function isAttachmentFailure(
  detail: unknown,
): detail is { attachment: string; version?: string; status?: string; exit_code?: number | null } {
  return typeof detail === "object" && detail !== null && "attachment" in detail;
}

function AgentSummary({ snapshot }: { snapshot: RunDetail["snapshot"] }) {
  const { model, harness, attachments, bootstrap_timeout_sec: installSec } = snapshot.effective;
  const labels = snapshot.provenance.labels;
  if (!model) return <span>—</span>;
  return (
    <span>
      {labels.model ?? model.model_id}（<code>{model.model_id}</code>，{model.provider}，修订{" "}
      {model.revision}）· {harness ? `${harness.name} ${harness.version}` : "—"}
      {attachments.length > 0 && (
        <span className="block text-xs">
          附件：
          {attachments.map((a, i) => (
            <span key={a.version_id} title={a.content_digest}>
              {i > 0 && " → "}
              {a.name} <code>{a.version}</code>
            </span>
          ))}
          {installSec != null && `（每试次安装时限 ${installSec} 秒）`}
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
        {score === 0 && <span className="ml-1 text-xs text-muted-foreground">有效零分</span>}
      </span>
    );
  }
  if (!TRIAL_TERMINAL.has(trial.state)) {
    return <span className="text-muted-foreground">进行中</span>;
  }
  const reason = trial.reason_code ?? result?.failure_cause ?? result?.exception_type;
  const invalidRaw =
    result && !result.reward_valid && result.raw_score !== null
      ? `原始值 ${String(result.raw_score)} 无效`
      : null;
  let verdict: [string, Tone];
  if (trial.state !== "execution_error" && trial.state !== "scored") {
    verdict = ["未完成", "muted"];
  } else if (
    result?.failure_cause === "verifier_failed" ||
    (result != null && result.verifier_status !== "not_run" && result.failure_cause == null)
  ) {
    verdict = ["缺评分", "warn"];
  } else {
    verdict = ["执行失败", "bad"];
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
  if (!last.ended_at) return "进行中";
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
      <nav aria-label="试次" className="grid gap-0.5 rounded-md border p-1">
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
              第 {selected.repetition_index} 次重复
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
              得分：<ScoreCell trial={selected} />
            </span>
            <span>
              产物：
              <ArtifactCounts counts={selected.artifact_counts} state={selected.artifact_state} />
            </span>
            <span>清理：{label(CLEANUP_STATE, selected.cleanup_state)[0]}</span>
            {selected.result && (
              <span>
                验证 {selected.result.verifier_status ?? "—"} · 原始分{" "}
                {selected.result.raw_score ?? "无"} · reward_valid{" "}
                {String(selected.result.reward_valid)} · 策略 {selected.result.policy_version}
              </span>
            )}
          </div>
        </CardHeader>
        <CardContent className="grid gap-2">
          <AttemptSteps key={selected.id} trial={selected} artifacts={own} live={live} />
          {own.length > 0 && (
            <div>
              <Button size="sm" variant="ghost" onClick={() => onShowArtifacts(selected.id)}>
                产物 {own.length} 个
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
  if (!attempt.ended_at) return "进行中";
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
    return <div className="text-xs text-muted-foreground">尚无执行尝试。</div>;
  }
  return (
    <ol className="grid gap-2">
      {trial.attempts.map((attempt) => {
        const err = attempt.error;
        return (
          <li key={attempt.id} className="rounded-md border bg-background px-3 py-2">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              {stepIcon(attempt.state)}
              <span className="font-medium">第 {attempt.attempt_index} 次尝试</span>
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
                {err.message ? `：${String(err.message)}` : ""}
                {isAttachmentFailure(err.detail) &&
                  `（附件 ${err.detail.attachment} ${err.detail.version ?? ""}：${
                    INSTALL_STATUS[err.detail.status ?? ""] ?? err.detail.status
                  }${err.detail.exit_code != null ? `，退出码 ${err.detail.exit_code}` : ""}）`}
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
                  ? "进行中"
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
          下载
        </a>
        {live && <Loader2 className="size-3 animate-spin" />}
        {lines.length > LOG_TAIL_LINES && <span>仅显示最后 {LOG_TAIL_LINES} 行</span>}
      </div>
      {failed && <div className="text-xs text-red-700">日志读取失败</div>}
      {text !== undefined && (
        <pre
          ref={pre}
          data-testid="step-log"
          className="max-h-96 overflow-auto rounded bg-zinc-950 p-2 font-mono text-xs leading-relaxed text-zinc-100"
        >
          {shown.join("\n") || "（空）"}
        </pre>
      )}
    </div>
  );
}

function artifactReason(artifact: Artifact): string | null {
  if (!artifact.status_reason) return null;
  return ARTIFACT_REASON[artifact.status_reason] ?? artifact.status_reason;
}

function ArtifactLink({ artifact }: { artifact: Artifact }) {
  const name = artifact.relative_path.split("/").pop() || artifact.relative_path;
  if (artifact.status !== "available") {
    const reason = artifactReason(artifact);
    return (
      <span className="text-muted-foreground" title={`${artifact.relative_path}${reason ? `：${reason}` : ""}`}>
        {name}（{label(ARTIFACT_STATUS, artifact.status)[0]}）
      </span>
    );
  }
  return (
    <a
      className="inline-flex items-center gap-1 text-primary hover:underline"
      href={`${API}/artifacts/${artifact.id}/download`}
      title={artifact.relative_path}
    >
      <Download className="size-3" />
      {name}
    </a>
  );
}

/** All of a run's artifacts, or one trial's (``trialId``, kept in the URL). */
function ArtifactTable({
  artifacts,
  trials,
  trialId,
  onTrialId,
  onDelete,
}: {
  artifacts: Artifact[];
  trials: Trial[];
  trialId: string;
  onTrialId: (trialId: string) => void;
  onDelete?: (artifact: Artifact) => Promise<void>;
}) {
  const trialName = new Map(trials.map((t) => [t.id, `${t.task_key} #${t.repetition_index}`]));
  const prefix = commonPrefix(trials.map((t) => t.task_key));
  const shown = trialId ? artifacts.filter((a) => a.trial_id === trialId) : artifacts;
  return (
    <div className="grid gap-2">
      <div className="flex min-w-0 items-center gap-2 text-sm">
        <label htmlFor="artifacts-of" className="shrink-0 text-muted-foreground">
          试次
        </label>
        <select
          id="artifacts-of"
          className="h-8 min-w-0 max-w-full rounded-md border border-input bg-background px-2 text-sm"
          value={trialId}
          onChange={(e) => onTrialId(e.target.value)}
        >
          <option value="">全部（{artifacts.length}）</option>
          {trials.map((t) => (
            <option key={t.id} value={t.id}>
              {trialName.get(t.id)}（{artifacts.filter((a) => a.trial_id === t.id).length}）
            </option>
          ))}
        </select>
      </div>
      <div className="rounded-md border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>文件</TableHead>
              <TableHead>类型</TableHead>
              <TableHead>试次</TableHead>
              <TableHead>大小</TableHead>
              <TableHead>状态</TableHead>
              <TableHead>到期</TableHead>
              {onDelete && <TableHead />}
            </TableRow>
          </TableHeader>
          <TableBody>
            {shown.map((a) => (
              <TableRow key={a.id}>
                <TableCell className="max-w-[28rem]">
                  <ArtifactLink artifact={a} />
                  <div className="break-all font-mono text-xs text-muted-foreground">
                    {a.relative_path}
                  </div>
                </TableCell>
                <TableCell className="text-xs">{a.kind}</TableCell>
                <TableCell className="max-w-[16rem] text-xs">
                  {a.trial_id ? (
                    trialName.has(a.trial_id) ? (
                      <LongName value={trialName.get(a.trial_id)!} prefix={prefix} copy={false} />
                    ) : (
                      shortId(a.trial_id)
                    )
                  ) : (
                    "运行级"
                  )}
                </TableCell>
                <TableCell className="text-xs tabular-nums">{fmtBytes(a.size_bytes)}</TableCell>
                <TableCell>
                  <StatusPill value={label(ARTIFACT_STATUS, a.status)} />
                  {artifactReason(a) && (
                    <div className="mt-1 text-xs text-muted-foreground">{artifactReason(a)}</div>
                  )}
                </TableCell>
                <TableCell className="text-xs">
                  {a.expires_at ? (
                    <span className={cn(a.status === "expired" && "text-amber-800")}>
                      {fmtTime(a.expires_at)}
                      {a.status === "expired" && "（已过期）"}
                    </span>
                  ) : (
                    <span className="text-muted-foreground">试次结束后起算</span>
                  )}
                </TableCell>
                {onDelete && (
                  <TableCell className="text-right">
                    {a.status !== "deleted" && a.status !== "deleting" && (
                      <Button
                        size="sm"
                        variant="outline"
                        aria-label={`删除产物 ${a.relative_path}`}
                        onClick={() => void onDelete(a)}
                      >
                        删除
                      </Button>
                    )}
                  </TableCell>
                )}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
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
        删除于 {fmtTime(deletion.deleted_at)}
        {deletion.purged_at && `，彻底删除于 ${fmtTime(deletion.purged_at)}`}。{text}
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
  if (counts.available) parts.push(<span key="a">可用 {counts.available}</span>);
  if (counts.expired) parts.push(<span key="e" className="text-amber-700">过期 {counts.expired}</span>);
  if (counts.failed) parts.push(<span key="f" className="text-red-700">失败 {counts.failed}</span>);
  if (parts.length === 0) return <>{label(ARTIFACT_STATE, state)[0]}</>;
  return (
    <span className="inline-flex gap-1" title={label(ARTIFACT_STATE, state)[0]}>
      {parts}
    </span>
  );
}

const DIRECTION: Record<string, string> = {
  higher_is_better: "越高越好",
  lower_is_better: "越低越好",
};

function passRule(summary: RunSummary): string {
  const rule = summary.contract.passed;
  if (!rule) return "未定义（不计算通过率）";
  if (rule.rule === "threshold" && typeof rule.threshold === "number") {
    const op = summary.contract.direction === "lower_is_better" ? "≤" : "≥";
    return `得分 ${op} ${rule.threshold} 记为通过`;
  }
  return JSON.stringify(rule);
}

/** 06 §5 result summary: counts, statistics with their coverage, and per-task rows. */
function ResultSummary({ summary, error }: { summary?: RunSummary; error: unknown }) {
  if (!summary) {
    return error ? <ErrorAlert error={error} title="无法加载结果汇总" /> : null;
  }
  const c = summary.contract;
  const causes = Object.entries(summary.excluded_by_cause);
  const summaryPrefix = commonPrefix(summary.tasks.map((t) => t.task_key));
  return (
    <Card data-testid="result-summary">
      <CardHeader>
        <CardTitle className="text-base">结果汇总</CardTitle>
        <div className="text-xs text-muted-foreground">
          指标 {c.name}（<code>{c.metric_id}</code>
          {c.unit ? `，单位 ${c.unit}` : ""}，{DIRECTION[c.direction] ?? c.direction}）· 评分规则来源：
          {c.source === "dataset_version" ? "数据集版本声明" : "平台默认"} · {passRule(summary)}
        </div>
      </CardHeader>
      <CardContent className="grid gap-4 text-sm">
        <div className="flex flex-wrap gap-x-6 gap-y-2">
          <Count label="计划" value={summary.planned_trials} />
          <Count label="已评分" value={summary.scored_trials} tone="good" />
          <Count
            label="执行失败（不计入统计）"
            value={summary.excluded_error_trials}
            tone={summary.excluded_error_trials ? "bad" : undefined}
          />
          {summary.count_as_failure_trials > 0 && (
            <Count label="按失败计分" value={summary.count_as_failure_trials} tone="bad" />
          )}
          <Count label="未完成" value={summary.unfinished_trials} />
          {summary.active_trials > 0 && <Count label="进行中" value={summary.active_trials} />}
          {summary.queued_trials > 0 && <Count label="排队" value={summary.queued_trials} />}
          {summary.passed_trials !== null && <Count label="通过" value={summary.passed_trials} />}
        </div>
        <div className="grid gap-x-6 gap-y-2 sm:grid-cols-3 lg:grid-cols-5">
          <Info
            label="均值（已评分试次）"
            value={
              <span className="tabular-nums">
                <b>{fmtNum(summary.mean)}</b>
                <span className="ml-1 text-xs text-muted-foreground">
                  已评分 {summary.scored_trials}/{summary.planned_trials}
                </span>
              </span>
            }
          />
          <Info label="标准差" value={<span className="tabular-nums">{fmtNum(summary.stddev)}</span>} />
          <Info
            label="通过率"
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
            label="评分覆盖 / 评估覆盖"
            value={
              <span className="tabular-nums" title="评分覆盖 = 已评分 / 计划；评估覆盖计入按失败计分的试次">
                {fmtPct(summary.scored_coverage)} / {fmtPct(summary.evaluation_coverage)}
              </span>
            }
          />
          <Info
            label="基准分（任务均值的宏平均）"
            value={
              <span className="tabular-nums">
                <b>{fmtNum(summary.bench_score)}</b>
                <span className="ml-1 text-xs text-muted-foreground">
                  有分任务 {summary.bench_scored_tasks}/{summary.task_count}
                  {summary.bench_task_coverage != null &&
                    ` · 覆盖基准任务 ${fmtPct(summary.bench_task_coverage)}`}
                </span>
              </span>
            }
          />
          <Info
            label="尝试 / 重试"
            value={
              <span className="tabular-nums">
                {summary.attempts} 次尝试 · 重试 {summary.retry_count} 次（{summary.retried_trials}{" "}
                个试次）
              </span>
            }
          />
          <Info label="评分完整性" value={<StatusPill value={label(RESULT_COMPLETENESS, summary.result_completeness)} />} />
          <Info
            label="执行失败原因"
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
            label="用量"
            value={
              <span className="text-xs">
                已知 {summary.usage.known_trials} 个试次，未知 {summary.usage.unknown_trials} 个
                {Object.keys(summary.usage.known_totals).length > 0 &&
                  ` · ${Object.entries(summary.usage.known_totals)
                    .map(([k, v]) => `${k} ${fmtNum(v)}`)
                    .join("，")}`}
              </span>
            }
          />
        </div>
        {summary.tasks.length > 0 && (
          <div className="rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>任务</TableHead>
                  <TableHead className="text-right">已评分 / 计划</TableHead>
                  <TableHead className="text-right">均值</TableHead>
                  <TableHead className="text-right">标准差</TableHead>
                  <TableHead className="text-right">通过</TableHead>
                  <TableHead>执行失败</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {summary.tasks.map((task) => (
                  <TableRow key={task.task_key} data-testid="task-summary-row">
                    <TableCell className="max-w-[24rem] font-mono text-xs">
                      <LongName value={task.task_key} prefix={summaryPrefix} />
                      {task.partial && (
                        <span className="ml-2" title="部分计划的重复没有得分，均值基于更少的样本">
                          <Pill tone="warn">部分</Pill>
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
                        ? `${task.excluded_trials} 个：${task.failure_causes.join("、") || "—"}`
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
