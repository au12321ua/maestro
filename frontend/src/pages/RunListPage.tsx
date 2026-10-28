import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { GitCompare, Package, Plus, RefreshCw } from "lucide-react";
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
import {
  EmptyNote,
  ErrorAlert,
  Loading,
  PageTitle,
  ScoreCoverage,
  StatusPill,
} from "@/components/common";
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
      toast.success(`已创建导出（${selected.size} 个运行），正在打包`);
      navigate("/exports");
    } catch (err) {
      toast.error(`导出失败：${errorText(err)}`);
    } finally {
      setExporting(false);
    }
  };

  return (
    <div>
      <PageTitle title="运行">
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
              {value === "all" ? "全部" : "我的"}
            </button>
          ))}
        </div>
        <label className="flex items-center gap-1 text-xs text-muted-foreground">
          受理于
          <input
            type="date"
            aria-label="开始日期"
            className="h-8 rounded-md border bg-background px-2 text-sm text-foreground"
            value={from}
            onChange={(e) => setFrom(e.target.value)}
          />
          至
          <input
            type="date"
            aria-label="结束日期"
            className="h-8 rounded-md border bg-background px-2 text-sm text-foreground"
            value={to}
            onChange={(e) => setTo(e.target.value)}
          />
        </label>
        <Button
          variant="outline"
          size="sm"
          disabled={selected.size < 2}
          title="把所选运行放在一起比较"
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
          比较所选
        </Button>
        <Button
          variant="outline"
          size="sm"
          disabled={selected.size === 0 || exporting}
          onClick={() => void exportSelected()}
        >
          <Package />
          导出所选{selected.size > 0 && `（${selected.size}）`}
        </Button>
        <Button variant="outline" size="sm" onClick={() => void load(null)}>
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
      <ErrorAlert error={error} />
      {runs.length === 0 && !loading && !error && <EmptyNote>还没有运行记录。</EmptyNote>}
      {runs.length > 0 && (
        <div className="rounded-md border bg-card">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-8">
                  <input
                    type="checkbox"
                    aria-label="全选"
                    checked={allSelected}
                    onChange={() =>
                      setSelected(allSelected ? new Set() : new Set(runs.map((r) => r.id)))
                    }
                  />
                </TableHead>
                <TableHead>名称</TableHead>
                <TableHead>提交者</TableHead>
                <TableHead>生命周期</TableHead>
                <TableHead>得分（均值 · 覆盖）</TableHead>
                <TableHead>评分覆盖</TableHead>
                <TableHead>产物</TableHead>
                <TableHead>环境清理</TableHead>
                <TableHead className="text-right">错误</TableHead>
                <TableHead>受理时间</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {runs.map((run) => (
                <TableRow key={run.id}>
                  <TableCell>
                    <input
                      type="checkbox"
                      aria-label={`选择 ${run.name}`}
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
                    {run.owner_id === me.id ? "我" : run.owner.display_name}
                    {run.owner.deleted && <span className="text-muted-foreground">（已删除）</span>}
                    {run.pat_id && <div className="text-muted-foreground">PAT</div>}
                  </TableCell>
                  <TableCell>
                    <StatusPill value={label(RUN_STATE, run.state)} />
                    {run.stop_requested && run.state !== "cancelled" && (
                      <div className="mt-1 text-xs text-amber-700">已请求停止</div>
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
            加载更多
          </Button>
        </div>
      )}
    </div>
  );
}
