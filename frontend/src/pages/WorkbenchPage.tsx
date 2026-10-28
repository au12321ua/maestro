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

const RECENT = 8;
/** How far back the pending list looks; older problems stay visible on their own pages. */
const SCAN = 100;
const OPERATION_KIND: Record<string, string> = {
  dataset_import: "数据集导入",
  preparation_first: "首次准备",
  preparation_restore: "缓存恢复",
  preparation_eviction: "缓存回收",
};

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
        title: `运行「${run.name}」有 ${run.counts.interrupted} 个试次被中断`,
        code: "TRIAL_INTERRUPTED",
        at: run.finished_at,
        who: "submitter",
        action: "重跑未得分的任务",
      });
    } else if (run.counts.errors > 0) {
      add({
        key: `err-${run.id}`,
        to: `/runs/${run.id}`,
        title: `运行「${run.name}」有 ${run.counts.errors} 个试次执行出错（含附件安装失败）`,
        code: "EXECUTION_ERROR",
        at: run.finished_at,
        who: "submitter",
        action: "查看错因后重跑",
      });
    }
  }
  const admin = me.role === "admin";
  for (const run of admin ? team : mine) {
    if (run.cleanup_state === "failed") {
      add({
        key: `clean-${run.id}`,
        to: `/runs/${run.id}`,
        title: `运行「${run.name}」的执行环境清理失败`,
        code: "CLEANUP_FAILED",
        at: run.finished_at,
        who: "admin",
        action: "检查残留容器",
      });
    }
    if (run.artifact_state === "failed" || run.artifact_state === "partial") {
      add({
        key: `art-${run.id}`,
        to: `/runs/${run.id}?tab=artifacts`,
        title: `运行「${run.name}」有产物保存失败`,
        code: "ARTIFACT_SAVE_FAILED",
        at: run.finished_at,
        who: admin ? "admin" : "submitter",
        action: "查看产物",
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
            title: `${dataset.namespace}/${dataset.name} ${version.upstream_ref}：${missing} 个任务未准入`,
            code: "TASK_NOT_ADMITTED",
            at: version.created_at,
            who: "admin",
            action: "在数据集页重试首次准备",
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
        title: `${OPERATION_KIND[op.kind]}${op.status === "failed" ? "失败" : "被中断"}`,
        code: op.error?.code ?? op.status.toUpperCase(),
        at: op.finished_at ?? op.updated_at,
        who: "admin",
        action: "在数据集页处理",
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
    return data.error ? <ErrorAlert error={data.error} title="无法加载工作台" /> : <Loading />;
  }
  const { mine, team, health, pending } = data.data;
  const results = team.filter((r) => r.state === "finished" && r.scoring).slice(0, RECENT);
  return (
    <div className="grid gap-4">
      <PageTitle title="工作台">
        <Button size="sm" variant="outline" onClick={() => void data.reload()}>
          <RefreshCw />
          刷新
        </Button>
        <Button asChild size="sm">
          <Link to="/runs/new">
            <Plus />
            新建运行
          </Link>
        </Button>
      </PageTitle>
      {data.error !== undefined && <ErrorAlert error={data.error} title="刷新失败（显示的是上次数据）" />}
      <SystemCard health={health} />
      <div className="grid gap-4 lg:grid-cols-2">
        <Section title="我的近期运行" more="/runs">
          <RunRows runs={mine.slice(0, RECENT)} empty="还没有提交过运行。" />
        </Section>
        <Section title="团队近期结果" more="/runs">
          <RunRows runs={results} empty="团队还没有已结束的运行。" showOwner />
        </Section>
      </div>
      <Card data-testid="pending">
        <CardHeader>
          <CardTitle className="text-base">待处理（{pending.length}）</CardTitle>
          <div className="text-xs text-muted-foreground">
            由运行、数据集与操作记录实时得出（最近 {SCAN} 条）；源对象恢复正常后自动消失。
          </div>
        </CardHeader>
        <CardContent>
          {pending.length === 0 ? (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <CircleCheck className="size-4 text-emerald-600" />
              没有待处理事项。
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
                      {item.who === "admin" ? "管理员处理" : "提交者可处理"}
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
        <span className="text-muted-foreground">系统</span>
        {!ready ? (
          <Pill tone="bad">维护中</Pill>
        ) : health.accepting_runs ? (
          <Pill tone="good">可受理运行</Pill>
        ) : (
          <Pill tone="warn">暂不受理运行</Pill>
        )}
        <span className="text-xs text-muted-foreground">
          服务状态 {health.status}；健康、可受理与某个运行能否执行是三个不同的判断。
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
          全部
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
