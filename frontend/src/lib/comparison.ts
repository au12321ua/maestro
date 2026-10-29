// Words and helpers for comparisons, collections and snapshots (06 section 8).
import { t } from "@/i18n";
import type {
  ComparedRunRow,
  Comparison,
  ComparisonCell,
  ComparisonLayout,
  ComparisonSettingsInput,
  Dimension,
  FailureGroup,
  Scenario,
  VariableDimension,
} from "./types";

export const DIMENSION: Record<Dimension, string> = t("comparison.dimension", {
  returnObjects: true,
});

export const VARIABLE_DIMENSIONS: VariableDimension[] = [
  "model",
  "harness",
  "attachments",
  "resources",
  "repetitions",
  "parameters",
];

const SCENARIO_LABEL: Record<Scenario, string> = t("comparison.scenario", {
  returnObjects: true,
});

export const SCENARIO: Record<
  Scenario,
  { label: string; variable: VariableDimension[] | null }
> = {
  models: { label: SCENARIO_LABEL.models, variable: ["model"] },
  harnesses: { label: SCENARIO_LABEL.harnesses, variable: ["harness"] },
  model_harness: {
    label: SCENARIO_LABEL.model_harness,
    variable: ["model", "harness"],
  },
  attachments: { label: SCENARIO_LABEL.attachments, variable: ["attachments"] },
  repeats: { label: SCENARIO_LABEL.repeats, variable: [] },
  custom: { label: SCENARIO_LABEL.custom, variable: null },
};

export const FAILURE_GROUP: Record<FailureGroup, string> = t(
  "comparison.failureGroup",
  { returnObjects: true },
);

export const EVIDENCE: Record<string, string> = t("comparison.evidence", {
  returnObjects: true,
});

/** A dimension value as the service names it, with its placeholder words translated. */
export function valueText(text: string | undefined): string {
  if (text === "none") return t("comparison.value.none");
  if (text === "author") return t("comparison.value.author");
  if (text === "execution_mode=agent") return t("comparison.value.agentMode");
  return text ?? "—";
}

export function noticeText(notice: Comparison["warnings"][number]): string {
  const dim = notice.dimension ? DIMENSION[notice.dimension] : "";
  switch (notice.code) {
    case "DATASET_DIFFERS":
      return t("comparison.notice.datasetDiffers");
    case "SCORING_DIFFERS":
      return t("comparison.notice.scoringDiffers");
    case "UNEXPECTED_DIFFERENCE":
      return t("comparison.notice.unexpectedDifference", { dimension: dim });
    case "ACTUAL_MODEL_DIFFERS":
      return t("comparison.notice.actualModelDiffers");
    case "RUN_IN_PROGRESS":
      return t("comparison.notice.runInProgress", {
        count: notice.run_ids?.length ?? 0,
      });
    case "TASK_SETS_DIFFER":
      return t("comparison.notice.taskSetsDiffer", {
        common: notice.common,
        all: notice.all,
      });
    case "FAILURES_NOT_IN_MEAN":
      return t("comparison.notice.failuresNotInMean");
    default:
      return notice.code;
  }
}

/** A row's name: the collection's label, else the varying values, else the run name. */
export function rowTitle(row: ComparedRunRow): string {
  return row.label || row.title || row.name;
}

/** The value of one dimension for a run, as the service names it. */
export function dimensionValue(
  table: Comparison,
  dimension: Dimension,
  runId: string,
): string {
  return valueText(
    table.dimensions.find((d) => d.key === dimension)?.labels[runId],
  );
}

export function sumGroups(groups: Record<string, number>): string {
  return Object.entries(groups)
    .map(([key, n]) => `${FAILURE_GROUP[key as FailureGroup] ?? key} ${n}`)
    .join(t("common.format.listSeparator"));
}

export function cellText(cell: ComparisonCell | null | undefined): string {
  if (!cell) return t("comparison.cell.notIncluded");
  if (!cell.valid_trials)
    return cell.pending_trials
      ? t("comparison.cell.pending")
      : t("comparison.cell.noValidTrials");
  return cell.mean == null ? "—" : String(Number(cell.mean.toFixed(3)));
}

/** Settings carried in a page address, so a link shows the same table. */
export function settingsFromParams(
  params: URLSearchParams,
): ComparisonSettingsInput {
  const found: ComparisonSettingsInput = {};
  if (params.has("exclude")) {
    const value = params.get("exclude") ?? "";
    found.exclude = (value ? value.split(",") : []) as FailureGroup[];
  }
  const scope = params.get("scope");
  if (scope === "common" || scope === "all") found.scope = scope;
  if (params.get("baseline")) found.baseline_run_id = params.get("baseline");
  const mode = params.get("layout");
  if (mode === "matrix" || mode === "leaderboard") {
    const key = params.get("sort");
    found.layout = {
      mode,
      rows: (params.get("rows") as Dimension) || null,
      columns: (params.get("cols") as Dimension) || null,
      sort:
        key && ["mean", "pass_rate", "valid_trials", "title"].includes(key)
          ? { key, descending: params.get("descending") === "true" }
          : null,
    };
  }
  return found;
}

export function settingsToParams(
  params: URLSearchParams,
  change: ComparisonSettingsInput,
): URLSearchParams {
  const next = new URLSearchParams(params);
  if (change.exclude) next.set("exclude", change.exclude.join(","));
  if (change.scope) next.set("scope", change.scope);
  if (change.baseline_run_id !== undefined) {
    if (change.baseline_run_id) next.set("baseline", change.baseline_run_id);
    else next.delete("baseline");
  }
  if (change.layout) {
    next.set("layout", change.layout.mode ?? "leaderboard");
    if (change.layout.rows) next.set("rows", change.layout.rows);
    else next.delete("rows");
    if (change.layout.columns) next.set("cols", change.layout.columns);
    else next.delete("cols");
    if (change.layout.sort) {
      next.set("sort", change.layout.sort.key);
      next.set("descending", String(change.layout.sort.descending ?? false));
    } else {
      next.delete("sort");
      next.delete("descending");
    }
  }
  return next;
}

/** The two dimensions a matrix can use, or nothing when there are not two varying. */
export function matrixAxes(
  table: Comparison,
  layout: ComparisonLayout,
  allowed?: Dimension[],
): [Dimension, Dimension] | null {
  const varying = table.varying.filter(
    (d) =>
      d !== "dataset" && d !== "scoring" && (!allowed || allowed.includes(d)),
  );
  const rows =
    layout.rows && varying.includes(layout.rows) ? layout.rows : varying[0];
  const columns =
    layout.columns &&
    varying.includes(layout.columns) &&
    layout.columns !== rows
      ? layout.columns
      : varying.find((d) => d !== rows);
  return rows && columns ? [rows, columns] : null;
}

function csvField(value: unknown): string {
  const text = value == null ? "" : String(value);
  return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

/** The whole table as CSV: one line per run, then one line per task and run. */
export function comparisonCsv(table: Comparison): string {
  const lines: unknown[][] = [];
  const varying = table.varying.filter(
    (d) => d !== "dataset" && d !== "scoring",
  );
  lines.push([
    "run_id",
    "name",
    ...varying,
    "task_count",
    "valid_trials",
    "mean",
    "stddev",
    "pass_rate",
    "difference_mean",
    "failed_trials",
    "excluded_trials",
  ]);
  for (const row of table.runs) {
    lines.push([
      row.run_id,
      rowTitle(row),
      ...varying.map((d) => dimensionValue(table, d, row.run_id)),
      row.task_count,
      row.valid_trials,
      row.mean,
      row.stddev,
      row.pass_rate,
      row.difference?.mean,
      row.failed_trials,
      row.excluded_trials,
    ]);
  }
  lines.push([]);
  lines.push([
    "task_key",
    "status",
    "run_id",
    "mean",
    "valid_trials",
    "planned_trials",
    "failed",
    "excluded",
  ]);
  for (const task of table.tasks) {
    for (const row of table.runs) {
      const cell = task.cells[row.run_id];
      lines.push([
        task.task_key,
        task.status,
        row.run_id,
        cell?.mean,
        cell?.valid_trials,
        cell?.planned_trials,
        cell?.failed_trials,
        cell?.excluded_trials,
      ]);
    }
  }
  return lines.map((line) => line.map(csvField).join(",")).join("\n") + "\n";
}

export function downloadText(name: string, text: string, type = "text/csv") {
  const url = URL.createObjectURL(
    new Blob([text], { type: `${type};charset=utf-8` }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  URL.revokeObjectURL(url);
}
