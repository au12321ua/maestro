// Words and helpers for comparisons, collections and snapshots (06 section 8).
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

export const DIMENSION: Record<Dimension, string> = {
  dataset: "数据集",
  scoring: "计分规则",
  model: "模型",
  harness: "Harness",
  attachments: "附件",
  resources: "资源与时限",
  repetitions: "重复次数",
  parameters: "作者参数",
  other: "其他运行设置",
};

export const VARIABLE_DIMENSIONS: VariableDimension[] = [
  "model",
  "harness",
  "attachments",
  "resources",
  "repetitions",
  "parameters",
];

export const SCENARIO: Record<
  Scenario,
  { label: string; variable: VariableDimension[] | null }
> = {
  models: { label: "比较模型", variable: ["model"] },
  harnesses: { label: "比较 Harness", variable: ["harness"] },
  model_harness: { label: "模型 × Harness", variable: ["model", "harness"] },
  attachments: { label: "附件 A/B", variable: ["attachments"] },
  repeats: { label: "重复稳定性", variable: [] },
  custom: { label: "自定义", variable: null },
};

export const FAILURE_GROUP: Record<FailureGroup, string> = {
  provider_unavailable: "模型接口暂时故障（限流、断连、过载）",
  provider_access: "认证失败、模型不存在、额度耗尽",
  environment_setup: "镜像构建、环境启动、Harness 安装超时",
  preparation: "准备缓存恢复失败、附件原件缺失",
  executor_unsupported: "执行框架不支持",
  platform_stop: "未开始、取消、中断、服务重启、运行总时限",
  out_of_memory: "运行中内存超限",
  verifier: "验证器没有产出分数",
  attachment: "附件安装失败或超时",
  provider_request: "模型接口的其他永久错误",
  unclassified: "未归类的错误",
};

export const EVIDENCE: Record<string, string> = {
  available: "原始证据可查看",
  none: "原始证据已不可用",
  deleted: "运行已删除",
  purged: "运行已彻底删除",
};

/** A dimension value as the service names it, with its placeholder words in Chinese. */
export function valueText(text: string | undefined): string {
  if (text === "none") return "无";
  if (text === "author") return "按作者设定";
  if (text === "execution_mode=agent") return "模型实验";
  return text ?? "—";
}

export function noticeText(notice: Comparison["warnings"][number]): string {
  const dim = notice.dimension ? DIMENSION[notice.dimension] : "";
  switch (notice.code) {
    case "DATASET_DIFFERS":
      return "数据集内容不同：只并排列出各运行自己的汇总，不对齐任务、不给差值。";
    case "SCORING_DIFFERS":
      return "计分规则不同：只并排列出各运行自己的汇总，不对齐任务、不给差值。";
    case "UNEXPECTED_DIFFERENCE":
      return `${dim}也不同：照常比较，但结果可能受它影响。`;
    case "ACTUAL_MODEL_DIFFERS":
      return "配置相同的模型实际报告的版本不同。";
    case "RUN_IN_PROGRESS":
      return `${notice.run_ids?.length ?? 0} 个运行还没结束，数字会变。`;
    case "TASK_SETS_DIFFER":
      return `任务范围不同：共同任务 ${notice.common} 个，全部 ${notice.all} 个。`;
    case "FAILURES_NOT_IN_MEAN":
      return "该指标越低越好：计为失败的试次只算未通过，不计入均分。";
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
    .join("；");
}

export function cellText(cell: ComparisonCell | null | undefined): string {
  if (!cell) return "未包含";
  if (!cell.valid_trials) return cell.pending_trials ? "进行中" : "无有效试次";
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
