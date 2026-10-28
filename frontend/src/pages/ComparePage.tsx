// An ad-hoc comparison (06 section 8): any runs side by side, settings kept in the address
// so a link shows the same table. From here it can become a collection or a snapshot.
import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Camera, Library } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ComparisonView } from "@/components/ComparisonView";
import {
  EmptyNote,
  ErrorAlert,
  Loading,
  PageTitle,
  SelectField,
  TextAreaField,
  TextField,
} from "@/components/common";
import { api, errorText } from "@/lib/api";
import {
  DIMENSION,
  SCENARIO,
  VARIABLE_DIMENSIONS,
  settingsFromParams,
  settingsToParams,
} from "@/lib/comparison";
import { useLoad } from "@/lib/hooks";
import type {
  Comparison,
  Dimension,
  Scenario,
  VariableDimension,
} from "@/lib/types";

/** The preset that lets exactly the dimensions that vary here vary, else custom. */
export function scenarioFor(varying: Dimension[]): {
  scenario: Scenario;
  variable: VariableDimension[];
} {
  const wanted = VARIABLE_DIMENSIONS.filter((d) => varying.includes(d));
  for (const [key, value] of Object.entries(SCENARIO)) {
    if (value.variable && value.variable.join() === wanted.join())
      return { scenario: key as Scenario, variable: wanted };
  }
  return { scenario: "custom", variable: wanted };
}

export function ComparePage() {
  const [params, setParams] = useSearchParams();
  const runIds = (params.get("runs") ?? "").split(",").filter(Boolean);
  const settings = settingsFromParams(params);
  const table = useLoad(
    async () =>
      runIds.length
        ? (
            await api.post("/comparisons", {
              body: { run_ids: runIds, ...settings },
            })
          ).data
        : undefined,
    [params.toString()],
  );
  const [panel, setPanel] = useState<"collection" | "snapshot" | null>(null);

  return (
    <div className="grid gap-4">
      <PageTitle title="比较">
        {table.data && (
          <>
            <Button
              size="sm"
              variant="outline"
              disabled={!table.data.comparable}
              title={
                table.data.comparable
                  ? undefined
                  : "数据集或计分规则不同，不能建成集合"
              }
              onClick={() =>
                setPanel(panel === "collection" ? null : "collection")
              }
            >
              <Library />
              建成集合
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => setPanel(panel === "snapshot" ? null : "snapshot")}
            >
              <Camera />
              保存快照
            </Button>
          </>
        )}
      </PageTitle>
      {runIds.length === 0 && (
        <EmptyNote>
          在运行列表勾选几个运行，或在批次页点“比较这一批”。
        </EmptyNote>
      )}
      <ErrorAlert error={table.error} />
      {table.loading && !table.data && <Loading />}
      {table.data && panel === "collection" && (
        <MakeCollection table={table.data} onClose={() => setPanel(null)} />
      )}
      {table.data && panel === "snapshot" && (
        <SaveSnapshot
          table={table.data}
          runIds={runIds}
          onClose={() => setPanel(null)}
        />
      )}
      {table.data && (
        <ComparisonView
          table={table.data}
          onChange={(change) =>
            setParams(settingsToParams(params, change), { replace: true })
          }
        />
      )}
    </div>
  );
}

function MakeCollection({
  table,
  onClose,
}: {
  table: Comparison;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const found = scenarioFor(table.varying);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const create = async () => {
    setBusy(true);
    setError(undefined);
    try {
      const res = await api.post("/collections", {
        body: {
          name: name.trim(),
          description,
          scenario: found.scenario,
          variable_dimensions:
            found.scenario === "custom" ? found.variable : null,
          base: { run_id: table.runs[0].run_id },
          run_ids: table.runs.map((r) => r.run_id),
          settings: {
            exclude: table.settings.exclude,
            scope: table.settings.scope,
          },
        },
      });
      toast.success("集合已建好");
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
        <CardTitle className="text-base">建成集合</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-3 text-sm">
        <p>
          场景：<b>{SCENARIO[found.scenario].label}</b>。可变的维度：
          {found.variable.length
            ? found.variable.map((d) => DIMENSION[d]).join("、")
            : "无"}
          ；其余维度固定为第一个运行的值。之后只有符合的运行能加入，规格建好后不能改。
        </p>
        <TextField
          id="collection-name"
          label="名称"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <TextAreaField
          id="collection-description"
          label="用途（可选）"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
        <ErrorAlert error={error} />
        <div className="flex gap-2">
          <Button disabled={busy || !name.trim()} onClick={() => void create()}>
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

export function SaveSnapshot({
  table,
  runIds,
  collectionId,
  onClose,
}: {
  table: Comparison;
  runIds?: string[];
  collectionId?: string;
  onClose: () => void;
}) {
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const active = table.runs.filter((r) => !r.terminal);
  const save = async () => {
    setBusy(true);
    try {
      const { exclude, scope, baseline_run_id, layout } = table.settings;
      const res = await api.post("/analysis-snapshots", {
        body: {
          name: name.trim(),
          collection_id: collectionId ?? null,
          run_ids: collectionId ? null : runIds,
          settings: { exclude, scope, baseline_run_id, layout },
          note: note.trim() || null,
        },
      });
      toast.success("快照已保存");
      navigate(`/snapshots/${res.data.id}`);
    } catch (err) {
      toast.error(`保存失败：${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">保存快照</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-3 text-sm">
        <p className="text-muted-foreground">
          冻结当前的运行清单、剔除设置、摆法和整张表；之后运行增减、日志过期，快照里的数字都不变。
        </p>
        {active.length > 0 && (
          <p className="text-amber-800">
            还有 {active.length} 个运行没结束，结束后才能保存快照。
          </p>
        )}
        <TextField
          id="snapshot-name"
          label="名称"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <TextAreaField
          id="snapshot-note"
          label="结论（可选，之后也能补）"
          value={note}
          onChange={(e) => setNote(e.target.value)}
        />
        <div className="flex gap-2">
          <Button
            disabled={busy || !name.trim() || active.length > 0}
            onClick={() => void save()}
          >
            保存
          </Button>
          <Button variant="ghost" onClick={onClose}>
            取消
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}

export function ScenarioPicker({
  scenario,
  variable,
  onChange,
}: {
  scenario: Scenario;
  variable: VariableDimension[];
  onChange: (scenario: Scenario, variable: VariableDimension[]) => void;
}) {
  return (
    <div className="grid gap-2">
      <SelectField
        id="collection-scenario"
        label="场景"
        value={scenario}
        onChange={(e) => {
          const next = e.target.value as Scenario;
          onChange(next, SCENARIO[next].variable ?? variable);
        }}
      >
        {Object.entries(SCENARIO).map(([key, value]) => (
          <option key={key} value={key}>
            {value.label}
            {value.variable
              ? `（可变：${value.variable.map((d) => DIMENSION[d]).join("、") || "无"}）`
              : ""}
          </option>
        ))}
      </SelectField>
      {scenario === "custom" && (
        <div className="flex flex-wrap gap-3 text-sm">
          可变的维度：
          {VARIABLE_DIMENSIONS.map((d) => (
            <label key={d} className="flex items-center gap-1">
              <input
                type="checkbox"
                checked={variable.includes(d)}
                onChange={() =>
                  onChange(
                    scenario,
                    variable.includes(d)
                      ? variable.filter((x) => x !== d)
                      : VARIABLE_DIMENSIONS.filter(
                          (x) => x === d || variable.includes(x),
                        ),
                  )
                }
              />
              {DIMENSION[d]}
            </label>
          ))}
        </div>
      )}
      <p className="text-xs text-muted-foreground">
        数据集与计分规则永远固定；其余没选为可变的维度都固定为基准的值。
      </p>
    </div>
  );
}
