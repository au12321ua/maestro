// One collection (06 section 8): its fixed specification, the comparison of its runs with
// the settings the team keeps, its members and history, and snapshots of it.
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Camera, CopyPlus, Play, Trash2 } from "lucide-react";
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
import { ComparisonView } from "@/components/ComparisonView";
import {
  ErrorAlert,
  Loading,
  PageTitle,
  StatusPill,
  TextAreaField,
  TextField,
} from "@/components/common";
import { SaveSnapshot, ScenarioPicker } from "@/pages/ComparePage";
import { SnapshotList } from "@/pages/CollectionsPage";
import { api, errorText } from "@/lib/api";
import { DIMENSION, SCENARIO, valueText } from "@/lib/comparison";
import { RUN_STATE, fmtTime, label } from "@/lib/format";
import { useLoad } from "@/lib/hooks";
import type {
  Collection,
  ComparisonSettingsInput,
  Dimension,
  Scenario,
  VariableDimension,
} from "@/lib/types";

const ACTION: Record<string, string> = {
  create: "建立集合",
  copy: "复制建立",
  add: "加入",
  remove: "移出",
  purged: "运行被彻底删除，移出",
  label: "改列名",
  edit: "修改",
};

export function CollectionPage() {
  const { collectionId = "" } = useParams();
  const navigate = useNavigate();
  const path = { collection_id: collectionId };
  const collection = useLoad(
    async () => (await api.get("/collections/{collection_id}", { path })).data,
    [collectionId],
  );
  const [view, setView] = useState<ComparisonSettingsInput>({});
  const table = useLoad(
    async () =>
      (
        await api.post("/collections/{collection_id}/comparison", {
          path,
          body: Object.keys(view).length ? view : null,
        })
      ).data,
    [collectionId, JSON.stringify(view)],
  );
  const snapshots = useLoad(
    async () =>
      (
        await api.get("/analysis-snapshots", {
          query: { collection_id: collectionId },
        })
      ).data,
    [collectionId],
  );
  const events = useLoad(
    async () =>
      (await api.get("/collections/{collection_id}/events", { path })).data,
    [collectionId],
  );
  const [panel, setPanel] = useState<"snapshot" | "copy" | "add" | null>(null);

  const c = collection.data;
  if (!c)
    return collection.error ? (
      <ErrorAlert error={collection.error} />
    ) : (
      <Loading />
    );
  const can = (action: string) =>
    c.allowed_actions.includes(action) && !c.deletion;
  const reloadAll = async () => {
    await Promise.all([collection.reload(), table.reload(), events.reload()]);
  };

  const changeSettings = async (change: ComparisonSettingsInput) => {
    if (!can("edit")) {
      setView({ ...view, ...change });
      return;
    }
    try {
      await api.put("/collections/{collection_id}", {
        path,
        body: { settings: change },
      });
      await reloadAll();
    } catch (err) {
      toast.error(`保存设置失败：${errorText(err)}`);
    }
  };
  const remove = async (runId: string) => {
    if (!window.confirm("把这个运行移出集合？运行本身不受影响。")) return;
    try {
      await api.del("/collections/{collection_id}/runs/{run_id}", {
        path: { ...path, run_id: runId },
      });
      await reloadAll();
    } catch (err) {
      toast.error(`移出失败：${errorText(err)}`);
    }
  };
  const rename = async (runId: string, current: string | null) => {
    const value = window.prompt(
      "这一列的名字（留空则按维度自动命名）",
      current ?? "",
    );
    if (value === null) return;
    try {
      await api.put("/collections/{collection_id}/runs/{run_id}", {
        path: { ...path, run_id: runId },
        body: { label: value.trim() || null },
      });
      await reloadAll();
    } catch (err) {
      toast.error(`修改失败：${errorText(err)}`);
    }
  };
  const deleteCollection = async () => {
    if (!window.confirm("删除这个集合？它会进回收站；运行和快照不受影响。"))
      return;
    try {
      await api.del("/collections/{collection_id}", { path });
      toast.success("已移入回收站");
      navigate("/collections");
    } catch (err) {
      toast.error(`删除失败：${errorText(err)}`);
    }
  };

  return (
    <div className="grid gap-4">
      <PageTitle title={c.name}>
        {can("submit") && (
          <Button asChild size="sm">
            <Link to={`/runs/new?collection=${c.id}`}>
              <Play />
              从集合新建运行
            </Link>
          </Button>
        )}
        {can("add_runs") && (
          <Button
            size="sm"
            variant="outline"
            onClick={() => setPanel(panel === "add" ? null : "add")}
          >
            加入已有运行
          </Button>
        )}
        {can("snapshot") && (
          <Button
            size="sm"
            variant="outline"
            onClick={() => setPanel(panel === "snapshot" ? null : "snapshot")}
          >
            <Camera />
            保存快照
          </Button>
        )}
        {c.allowed_actions.includes("copy") && (
          <Button
            size="sm"
            variant="outline"
            onClick={() => setPanel(panel === "copy" ? null : "copy")}
          >
            <CopyPlus />
            复制并修改规格
          </Button>
        )}
        {can("delete") && (
          <Button
            size="sm"
            variant="outline"
            onClick={() => void deleteCollection()}
          >
            <Trash2 />
            删除
          </Button>
        )}
      </PageTitle>
      {c.deletion && (
        <ErrorAlert
          error={new Error("这个集合已删除，在回收站中。")}
          title="已删除"
        />
      )}
      <Spec collection={c} />
      {panel === "add" && (
        <AddRuns collection={c} onDone={() => void reloadAll()} />
      )}
      {panel === "snapshot" && table.data && (
        <SaveSnapshot
          table={table.data}
          collectionId={c.id}
          onClose={() => setPanel(null)}
        />
      )}
      {panel === "copy" && (
        <CopyCollection collection={c} onClose={() => setPanel(null)} />
      )}
      <ErrorAlert error={table.error} />
      {table.loading && !table.data && <Loading />}
      {table.data && (
        <>
          {!can("edit") && Object.keys(view).length > 0 && (
            <p className="text-xs text-muted-foreground">
              你改的设置只用于这次查看，不会保存到集合。
            </p>
          )}
          <ComparisonView
            table={table.data}
            axes={c.variable_dimensions as Dimension[]}
            onChange={(change) => void changeSettings(change)}
            csvName={`${c.name}.csv`}
          />
        </>
      )}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">运行（{c.run_count}）</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="overflow-x-auto rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>运行</TableHead>
                  <TableHead>列名</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead>加入者</TableHead>
                  <TableHead>加入时间</TableHead>
                  <TableHead />
                </TableRow>
              </TableHeader>
              <TableBody>
                {(c.members ?? []).map((m) => (
                  <TableRow key={m.run_id}>
                    <TableCell className="max-w-[20rem]">
                      <Link
                        className="block truncate text-primary hover:underline"
                        to={`/runs/${m.run_id}`}
                        title={m.run_name}
                      >
                        {m.run_name}
                      </Link>
                    </TableCell>
                    <TableCell className="text-xs">{m.label ?? "—"}</TableCell>
                    <TableCell>
                      {m.deletion ? (
                        <span className="text-xs text-muted-foreground">
                          已删除，不参与计算
                        </span>
                      ) : (
                        <StatusPill value={label(RUN_STATE, m.run_state)} />
                      )}
                    </TableCell>
                    <TableCell className="text-xs">
                      {m.added_by_username}
                    </TableCell>
                    <TableCell className="text-xs">
                      {fmtTime(m.added_at)}
                    </TableCell>
                    <TableCell className="whitespace-nowrap text-right">
                      {can("edit") && (
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => void rename(m.run_id, m.label)}
                        >
                          列名
                        </Button>
                      )}
                      {can("remove_runs") && (
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => void remove(m.run_id)}
                        >
                          移出
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle className="text-base">快照</CardTitle>
        </CardHeader>
        <CardContent>
          {snapshots.data && snapshots.data.length === 0 && (
            <p className="text-sm text-muted-foreground">还没有快照。</p>
          )}
          {snapshots.data && snapshots.data.length > 0 && (
            <SnapshotList items={snapshots.data} />
          )}
        </CardContent>
      </Card>
      <details className="rounded-md border p-3 text-sm">
        <summary className="cursor-pointer">修改记录</summary>
        <ul className="mt-2 grid gap-1">
          {(events.data ?? []).map((e) => (
            <li key={e.id} className="text-xs">
              {fmtTime(e.at)} · {e.actor_username}
              {e.pat_id ? "（PAT）" : ""} · {ACTION[e.action] ?? e.action}
              {e.run_id && (
                <>
                  {" "}
                  <Link
                    className="text-primary hover:underline"
                    to={`/runs/${e.run_id}`}
                  >
                    {e.run_id}
                  </Link>
                </>
              )}
            </li>
          ))}
        </ul>
      </details>
    </div>
  );
}

function Spec({ collection: c }: { collection: Collection }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">规格</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-2 text-sm">
        {c.description && (
          <p className="whitespace-pre-wrap">{c.description}</p>
        )}
        <p>
          场景：<b>{SCENARIO[c.scenario].label}</b>；可变：
          {c.variable_dimensions
            .map((d) => DIMENSION[d as Dimension])
            .join("、") || "无"}
        </p>
        <div className="grid gap-1 md:grid-cols-2">
          {Object.entries(c.fixed_labels).map(([dimension, text]) => (
            <div
              key={dimension}
              className="min-w-0 truncate"
              title={valueText(text)}
            >
              <span className="text-muted-foreground">
                固定 · {DIMENSION[dimension as Dimension]}：
              </span>
              {valueText(text)}
            </div>
          ))}
        </div>
        <p className="text-xs text-muted-foreground">
          规格建好后不能改；要换比法请“复制并修改规格”。{c.owner_username} 建于{" "}
          {fmtTime(c.created_at)}
          {c.base_run_id && (
            <>
              ，固定值取自运行{" "}
              <Link className="underline" to={`/runs/${c.base_run_id}`}>
                {c.base_run_id}
              </Link>
            </>
          )}
          {c.copied_from && (
            <>
              ，复制自{" "}
              <Link className="underline" to={`/collections/${c.copied_from}`}>
                另一个集合
              </Link>
            </>
          )}
        </p>
      </CardContent>
    </Card>
  );
}

function AddRuns({
  collection,
  onDone,
}: {
  collection: Collection;
  onDone: () => void;
}) {
  const [text, setText] = useState("");
  const [error, setError] = useState<unknown>();
  const add = async () => {
    setError(undefined);
    const ids = text.split(/[\s,]+/).filter(Boolean);
    try {
      await api.post("/collections/{collection_id}/runs", {
        path: { collection_id: collection.id },
        body: { runs: ids.map((run_id) => ({ run_id })) },
      });
      setText("");
      toast.success("已加入");
      onDone();
    } catch (err) {
      setError(err);
    }
  };
  return (
    <Card>
      <CardContent className="grid gap-2 pt-6">
        <TextField
          id="add-run-ids"
          label="运行 ID（多个用空格或逗号分开）"
          value={text}
          onChange={(e) => setText(e.target.value)}
          hint="固定维度与集合不同的运行会被拒绝，并列出哪里不同。也可以在运行详情页点“加入集合”。"
        />
        <ErrorAlert error={error} title="没有加入" />
        <div>
          <Button size="sm" disabled={!text.trim()} onClick={() => void add()}>
            加入
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

function CopyCollection({
  collection,
  onClose,
}: {
  collection: Collection;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const [name, setName] = useState(`${collection.name}（副本）`);
  const [description, setDescription] = useState(collection.description);
  const [scenario, setScenario] = useState<Scenario>(collection.scenario);
  const [variable, setVariable] = useState<VariableDimension[]>(
    collection.variable_dimensions,
  );
  const [baseRun, setBaseRun] = useState("");
  const [error, setError] = useState<unknown>();
  const [result, setResult] = useState<Collection | null>(null);
  const copy = async () => {
    setError(undefined);
    try {
      const res = await api.post("/collections/{collection_id}/copy", {
        path: { collection_id: collection.id },
        body: {
          name: name.trim(),
          description,
          scenario,
          variable_dimensions: scenario === "custom" ? variable : null,
          base: baseRun ? { run_id: baseRun } : null,
        },
      });
      if (res.data.not_carried?.length) setResult(res.data);
      else navigate(`/collections/${res.data.id}`);
    } catch (err) {
      setError(err);
    }
  };
  if (result) {
    return (
      <Card>
        <CardContent className="grid gap-2 pt-6 text-sm">
          <p>
            新集合已建好，{result.run_count}{" "}
            个运行带了过去；下面这些不符合新规格，留在原集合：
          </p>
          <ul className="list-disc pl-5 text-xs">
            {(result.not_carried ?? []).map((x) => (
              <li key={x.run_id}>
                {x.name}：
                {[
                  ...new Set(
                    x.mismatches.map(
                      (m) => DIMENSION[m.dimension as Dimension],
                    ),
                  ),
                ].join("、")}{" "}
                不同
              </li>
            ))}
          </ul>
          <div>
            <Button
              size="sm"
              onClick={() => navigate(`/collections/${result.id}`)}
            >
              打开新集合
            </Button>
          </div>
        </CardContent>
      </Card>
    );
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">复制并修改规格</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-3">
        <TextField
          id="copy-name"
          label="新集合名称"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <TextAreaField
          id="copy-description"
          label="用途"
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
          固定值取自（原来可变、现在要固定的维度必须选）
          <select
            className="h-9 rounded-md border bg-background px-2"
            value={baseRun}
            onChange={(e) => setBaseRun(e.target.value)}
          >
            <option value="">沿用原集合的固定值</option>
            {(collection.members ?? [])
              .filter((m) => !m.deletion)
              .map((m) => (
                <option key={m.run_id} value={m.run_id}>
                  {m.label ?? m.run_name}
                </option>
              ))}
          </select>
        </label>
        <ErrorAlert error={error} />
        <div className="flex gap-2">
          <Button disabled={!name.trim()} onClick={() => void copy()}>
            复制
          </Button>
          <Button variant="ghost" onClick={onClose}>
            取消
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
