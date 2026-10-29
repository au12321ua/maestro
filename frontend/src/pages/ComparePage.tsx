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
import { Trans, t } from "@/i18n";
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
      <PageTitle title={t("comparison.compare.title")}>
        {table.data && (
          <>
            <Button
              size="sm"
              variant="outline"
              disabled={!table.data.comparable}
              title={
                table.data.comparable
                  ? undefined
                  : t("comparison.compare.cannotCollect")
              }
              onClick={() =>
                setPanel(panel === "collection" ? null : "collection")
              }
            >
              <Library />
              {t("comparison.compare.makeCollection")}
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => setPanel(panel === "snapshot" ? null : "snapshot")}
            >
              <Camera />
              {t("comparison.shared.saveSnapshot")}
            </Button>
          </>
        )}
      </PageTitle>
      {runIds.length === 0 && (
        <EmptyNote>{t("comparison.compare.empty")}</EmptyNote>
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
      toast.success(t("comparison.compare.created"));
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
        <CardTitle className="text-base">
          {t("comparison.compare.makeCollection")}
        </CardTitle>
      </CardHeader>
      <CardContent className="grid gap-3 text-sm">
        <p>
          <Trans
            i18nKey="comparison.compare.collectionSpec"
            values={{
              scenario: SCENARIO[found.scenario].label,
              dimensions: found.variable.length
                ? found.variable
                    .map((d) => DIMENSION[d])
                    .join(t("comparison.shared.listJoin"))
                : t("comparison.value.none"),
            }}
            components={{ b: <b /> }}
          />
        </p>
        <TextField
          id="collection-name"
          label={t("comparison.shared.name")}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <TextAreaField
          id="collection-description"
          label={t("comparison.shared.purpose")}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
        <ErrorAlert error={error} />
        <div className="flex gap-2">
          <Button disabled={busy || !name.trim()} onClick={() => void create()}>
            {t("comparison.shared.create")}
          </Button>
          <Button variant="ghost" onClick={onClose}>
            {t("comparison.shared.cancel")}
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
      toast.success(t("comparison.compare.snapshotSaved"));
      navigate(`/snapshots/${res.data.id}`);
    } catch (err) {
      toast.error(t("comparison.shared.saveFailed", { error: errorText(err) }));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          {t("comparison.shared.saveSnapshot")}
        </CardTitle>
      </CardHeader>
      <CardContent className="grid gap-3 text-sm">
        <p className="text-muted-foreground">
          {t("comparison.compare.freezeHint")}
        </p>
        {active.length > 0 && (
          <p className="text-amber-800">
            {t("comparison.compare.activeRuns", { count: active.length })}
          </p>
        )}
        <TextField
          id="snapshot-name"
          label={t("comparison.shared.name")}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <TextAreaField
          id="snapshot-note"
          label={t("comparison.compare.noteLabel")}
          value={note}
          onChange={(e) => setNote(e.target.value)}
        />
        <div className="flex gap-2">
          <Button
            disabled={busy || !name.trim() || active.length > 0}
            onClick={() => void save()}
          >
            {t("comparison.shared.save")}
          </Button>
          <Button variant="ghost" onClick={onClose}>
            {t("comparison.shared.cancel")}
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
        label={t("comparison.shared.scenario")}
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
              ? t("comparison.compare.scenarioVariable", {
                  dimensions:
                    value.variable
                      .map((d) => DIMENSION[d])
                      .join(t("comparison.shared.listJoin")) ||
                    t("comparison.value.none"),
                })
              : ""}
          </option>
        ))}
      </SelectField>
      {scenario === "custom" && (
        <div className="flex flex-wrap gap-3 text-sm">
          {t("comparison.compare.customDimensions")}
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
        {t("comparison.compare.fixedHint")}
      </p>
    </div>
  );
}
