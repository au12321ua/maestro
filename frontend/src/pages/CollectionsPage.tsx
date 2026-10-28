// Collections (06 section 8): run lists the team keeps up, each with a specification fixed
// at creation; and the snapshots that froze a comparison.
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Plus } from "lucide-react";
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
import {
  EmptyNote,
  ErrorAlert,
  Loading,
  PageTitle,
  TextAreaField,
  TextField,
} from "@/components/common";
import { ScenarioPicker } from "@/pages/ComparePage";
import { api } from "@/lib/api";
import { DIMENSION, SCENARIO } from "@/lib/comparison";
import { fmtTime } from "@/lib/format";
import { useLoad } from "@/lib/hooks";
import type { Dimension, Scenario, VariableDimension } from "@/lib/types";

export function CollectionsPage() {
  const collections = useLoad(async () => (await api.get("/collections")).data);
  const snapshots = useLoad(
    async () => (await api.get("/analysis-snapshots")).data,
  );
  const [creating, setCreating] = useState(false);
  return (
    <div className="grid gap-4">
      <PageTitle title="集合">
        <Button size="sm" onClick={() => setCreating(!creating)}>
          <Plus />
          新建集合
        </Button>
      </PageTitle>
      <p className="text-sm text-muted-foreground">
        集合是团队一起维护的一组运行，例如“TB2
        各家模型”。建集合时选场景，定下哪些维度可以变、其余维度固定为什么值；之后只有符合的运行能加入。也可以在运行列表勾选几个运行先比较，再“建成集合”。
      </p>
      {creating && <NewCollection onClose={() => setCreating(false)} />}
      <ErrorAlert error={collections.error} />
      {collections.loading && !collections.data && <Loading />}
      {collections.data && collections.data.length === 0 && (
        <EmptyNote>还没有集合。</EmptyNote>
      )}
      {collections.data && collections.data.length > 0 && (
        <div className="overflow-x-auto rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>名称</TableHead>
                <TableHead>场景</TableHead>
                <TableHead>数据集</TableHead>
                <TableHead className="text-right">运行</TableHead>
                <TableHead>创建者</TableHead>
                <TableHead>更新时间</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {collections.data.map((c) => (
                <TableRow key={c.id}>
                  <TableCell className="max-w-[24rem]">
                    <Link
                      className="block truncate font-medium text-primary hover:underline"
                      to={`/collections/${c.id}`}
                      title={c.name}
                    >
                      {c.name}
                    </Link>
                    {c.description && (
                      <div
                        className="truncate text-xs text-muted-foreground"
                        title={c.description}
                      >
                        {c.description}
                      </div>
                    )}
                  </TableCell>
                  <TableCell className="text-xs">
                    {SCENARIO[c.scenario].label}
                    <div className="text-muted-foreground">
                      可变：
                      {c.variable_dimensions
                        .map((d) => DIMENSION[d as Dimension])
                        .join("、") || "无"}
                    </div>
                  </TableCell>
                  <TableCell className="text-xs">
                    {c.fixed_labels.dataset}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {c.run_count}
                  </TableCell>
                  <TableCell className="text-xs">{c.owner_username}</TableCell>
                  <TableCell className="text-xs">
                    {fmtTime(c.updated_at)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">快照</CardTitle>
        </CardHeader>
        <CardContent>
          <ErrorAlert error={snapshots.error} />
          {snapshots.data && snapshots.data.length === 0 && (
            <EmptyNote>还没有快照。在比较页或集合页点“保存快照”。</EmptyNote>
          )}
          {snapshots.data && snapshots.data.length > 0 && (
            <SnapshotList items={snapshots.data} />
          )}
        </CardContent>
      </Card>
    </div>
  );
}

export function SnapshotList({
  items,
}: {
  items: {
    id: string;
    name: string;
    owner_username?: string | null;
    run_count: number;
    created_at: string;
    pat_id?: string | null;
  }[];
}) {
  return (
    <ul className="grid gap-1 text-sm">
      {items.map((s) => (
        <li key={s.id} className="flex min-w-0 flex-wrap items-center gap-2">
          <Link
            className="truncate text-primary hover:underline"
            to={`/snapshots/${s.id}`}
          >
            {s.name}
          </Link>
          <span className="text-xs text-muted-foreground">
            {s.run_count} 个运行 · {s.owner_username}
            {s.pat_id ? "（PAT）" : ""} · {fmtTime(s.created_at)}
          </span>
        </li>
      ))}
    </ul>
  );
}

function NewCollection({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate();
  const runs = useLoad(
    async () => (await api.get("/runs", { query: { limit: 100 } })).data,
  );
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [scenario, setScenario] = useState<Scenario>("models");
  const [variable, setVariable] = useState<VariableDimension[]>(["model"]);
  const [baseRun, setBaseRun] = useState("");
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const create = async () => {
    setBusy(true);
    setError(undefined);
    try {
      const res = await api.post("/collections", {
        body: {
          name: name.trim(),
          description,
          scenario,
          variable_dimensions: scenario === "custom" ? variable : null,
          base: { run_id: baseRun },
          run_ids: [baseRun],
        },
      });
      navigate(`/collections/${res.data.id}`);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">新建集合</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-3">
        <TextField
          id="new-collection-name"
          label="名称"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <TextAreaField
          id="new-collection-description"
          label="用途（可选）"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
        <ScenarioPicker
          scenario={scenario}
          variable={variable}
          onChange={(s, v) => {
            setScenario(s);
            setVariable(v);
          }}
        />
        <label className="grid gap-1 text-sm">
          基准运行：固定维度取它的值，它也是集合的第一个运行
          <select
            className="h-9 rounded-md border bg-background px-2"
            value={baseRun}
            onChange={(e) => setBaseRun(e.target.value)}
          >
            <option value="">选择一个运行</option>
            {(runs.data ?? []).map((r) => (
              <option key={r.id} value={r.id}>
                {r.name}（{r.owner.username}，{fmtTime(r.accepted_at)}）
              </option>
            ))}
          </select>
          <span className="text-xs text-muted-foreground">
            也可以在“新建运行”页把填好的表单建成集合，不必先有运行。
          </span>
        </label>
        <ErrorAlert error={error} />
        <div className="flex gap-2">
          <Button
            disabled={busy || !name.trim() || !baseRun}
            onClick={() => void create()}
          >
            建好
          </Button>
          <Button variant="ghost" onClick={onClose}>
            取消
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
