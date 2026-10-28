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
import { EmptyNote, ErrorAlert, Loading, PageTitle, StatusPill } from "@/components/common";
import { api, errorText } from "@/lib/api";
import { OPERATION_STATUS, RUN_STATE, fmtTime, label } from "@/lib/format";
import { useInterval, useLoad } from "@/lib/hooks";
import type { Batch } from "@/lib/types";

const ENDED = new Set(["succeeded", "failed", "cancelled", "interrupted"]);

export function BatchListPage() {
  const list = useLoad(async () => (await api.get("/run-batches")).data);
  return (
    <div className="grid gap-4">
      <PageTitle title="批次" />
      {list.loading && !list.data && <Loading />}
      <ErrorAlert error={list.error} />
      {list.data && list.data.length === 0 && (
        <EmptyNote>还没有批次。在“新建运行”页把几个配置加入批次草稿后一起提交。</EmptyNote>
      )}
      {list.data && list.data.length > 0 && (
        <div className="overflow-auto rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>名称</TableHead>
                <TableHead>提交者</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>运行</TableHead>
                <TableHead>受理时间</TableHead>
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
                    {b.progress.terminal_runs}/{b.progress.runs} 已结束
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
    if (!window.confirm("取消整个批次？还没结束的运行会停止，已得到的评分保留。")) return;
    try {
      setBatch(
        (await api.post("/run-batches/{batch_id}/cancel", { path: { batch_id: batchId } })).data,
      );
      toast.success("已请求取消批次");
    } catch (err) {
      toast.error(`取消失败：${errorText(err)}`);
    }
  };

  if (!batch) return error ? <ErrorAlert error={error} /> : <Loading />;
  const p = batch.progress;
  return (
    <div className="grid gap-4">
      <PageTitle title={`批次：${batch.name}`}>
        {batch.runs.length > 1 && (
          <Button asChild size="sm" variant="outline">
            <Link to={`/compare?runs=${batch.runs.map((r) => r.id).join(",")}`}>比较这一批</Link>
          </Button>
        )}
        {batch.allowed_actions.includes("cancel") && (
          <Button size="sm" variant="destructive" onClick={() => void cancel()}>
            取消批次
          </Button>
        )}
      </PageTitle>
      <ErrorAlert error={error} title="刷新失败（显示的是上次数据）" />
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-base">
            <StatusPill value={label(OPERATION_STATUS, batch.status)} />
            {batch.stop_requested && <span className="text-sm text-muted-foreground">已请求取消</span>}
          </CardTitle>
        </CardHeader>
        <CardContent className="grid gap-1 text-sm">
          <p>
            {p.runs} 个运行：已结束 {p.terminal_runs}（完成 {p.finished}、取消 {p.cancelled}、中断{" "}
            {p.interrupted}）
          </p>
          <p>
            计划 {p.planned_trials} 个试次：得分 {p.scored_trials}、出错 {p.error_trials}
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
              <TableHead>运行</TableHead>
              <TableHead>状态</TableHead>
              <TableHead>得分 / 计划</TableHead>
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
