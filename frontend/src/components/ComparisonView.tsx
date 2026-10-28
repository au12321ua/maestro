// The comparison table shared by ad-hoc comparisons, collections and snapshots (06 section
// 8): what is the same and what differs, which failures are left out, the runs as a
// leaderboard or a matrix, and every task. Without ``onChange`` it is read-only (a snapshot).
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { ArrowDownUp, Download } from "lucide-react";
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
import { EmptyNote, LongName, Pill, StatusPill } from "@/components/common";
import {
  DIMENSION,
  EVIDENCE,
  FAILURE_GROUP,
  cellText,
  comparisonCsv,
  dimensionValue,
  downloadText,
  matrixAxes,
  noticeText,
  rowTitle,
  sumGroups,
  valueText,
} from "@/lib/comparison";
import { RUN_STATE, commonPrefix, fmtNum, fmtPct, label } from "@/lib/format";
import type {
  ComparedRunRow,
  Comparison,
  ComparisonCell,
  ComparisonLayout,
  ComparisonSettingsInput,
  Dimension,
  FailureGroup,
} from "@/lib/types";
import { cn } from "@/lib/utils";

const HIDDEN: Dimension[] = ["dataset", "scoring"];

export function ComparisonView({
  table,
  onChange,
  axes,
  evidence,
  csvName = "comparison.csv",
}: {
  table: Comparison;
  onChange?: (change: ComparisonSettingsInput) => void;
  /** The dimensions a matrix may use (a collection's varying ones); all when unset. */
  axes?: Dimension[];
  /** Whether each run's files still open now (a snapshot's numbers are older). */
  evidence?: Record<string, string>;
  csvName?: string;
}) {
  // A frozen table may still be laid out differently; that is not saved anywhere.
  const [localLayout, setLocalLayout] = useState<ComparisonLayout | null>(null);
  if (!onChange && localLayout) {
    table = { ...table, settings: { ...table.settings, layout: localLayout } };
  }
  const change =
    onChange ??
    ((c: ComparisonSettingsInput) => {
      if (c.layout) setLocalLayout(c.layout as ComparisonLayout);
    });
  const settings = table.settings;
  const varying = table.varying.filter((d) => !HIDDEN.includes(d));
  const matrix = matrixAxes(table, settings.layout, axes);
  const mode =
    settings.layout.mode === "matrix" && matrix ? "matrix" : "leaderboard";
  const evidenceOf = (row: ComparedRunRow) =>
    evidence?.[row.run_id] ?? row.evidence;

  return (
    <div className="grid gap-4">
      <Conditions table={table} />
      <Card>
        <CardHeader>
          <CardTitle className="flex flex-wrap items-center gap-2 text-base">
            结果
            <span className="text-sm font-normal text-muted-foreground">
              {settings.scope === "common"
                ? `共同任务 ${table.common_tasks} 个${
                    table.dropped_tasks
                      ? `，因剔除少比了 ${table.dropped_tasks} 个`
                      : ""
                  }`
                : `全部任务 ${table.all_tasks} 个，不给差值`}
            </span>
            <span className="ml-auto flex flex-wrap gap-2">
              <Button
                size="sm"
                variant="outline"
                onClick={() => downloadText(csvName, comparisonCsv(table))}
              >
                <Download />
                导出 CSV
              </Button>
            </span>
          </CardTitle>
        </CardHeader>
        <CardContent className="grid gap-4">
          <Controls
            table={table}
            onChange={change}
            frozen={!onChange}
            matrixAvailable={!!matrix}
            axes={axes}
          />
          {table.runs.length === 0 ? (
            <EmptyNote>没有可比较的运行。</EmptyNote>
          ) : mode === "matrix" && matrix ? (
            <MatrixTable table={table} axes={matrix} />
          ) : (
            <Leaderboard
              table={table}
              varying={varying}
              onChange={change}
              evidenceOf={evidenceOf}
            />
          )}
        </CardContent>
      </Card>
      {table.tasks.length > 0 && <TaskTable table={table} />}
    </div>
  );
}

function Conditions({ table }: { table: Comparison }) {
  const first = table.runs[0]?.run_id;
  const same = table.dimensions.filter((d) => !d.varies);
  const varying = table.dimensions.filter((d) => d.varies);
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">条件核对</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-2 text-sm">
        {first && (
          <p className="min-w-0 break-words">
            <span className="mr-1 font-medium text-emerald-700">✓ 相同：</span>
            {same.map((d, i) => (
              <span key={d.key}>
                {i > 0 && "；"}
                {DIMENSION[d.key]}
                {["dataset", "model", "harness", "repetitions"].includes(
                  d.key,
                ) && (
                  <span className="text-muted-foreground">
                    （{valueText(d.labels[first])}）
                  </span>
                )}
              </span>
            ))}
          </p>
        )}
        {varying.length > 0 && (
          <p>
            <span className="mr-1 font-medium text-sky-700">↔ 不同：</span>
            {varying.map((d) => DIMENSION[d.key]).join("、")}
          </p>
        )}
        {table.blockers.map((b) => (
          <p
            key={b.code}
            className="rounded-md border border-red-300 bg-red-50 px-2 py-1 text-red-800"
          >
            {noticeText(b)}
          </p>
        ))}
        {table.warnings.map((w, i) => (
          <p
            key={`${w.code}-${i}`}
            className="rounded-md border border-amber-300 bg-amber-50 px-2 py-1 text-amber-900"
          >
            ⚠ {noticeText(w)}
          </p>
        ))}
        {table.set_aside.length > 0 && (
          <p className="text-muted-foreground">
            已删除、不参与计算：{table.set_aside.map((x) => x.name).join("、")}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function Controls({
  table,
  onChange,
  frozen,
  matrixAvailable,
  axes,
}: {
  table: Comparison;
  onChange: (change: ComparisonSettingsInput) => void;
  /** Only the layout can change: the numbers are a snapshot's. */
  frozen: boolean;
  matrixAvailable: boolean;
  axes?: Dimension[];
}) {
  const settings = table.settings;
  const excluded = new Set(settings.exclude);
  const removed = table.groups.filter((g) => g.excluded && g.trials > 0);
  const summary = removed.length
    ? `已剔除 ${removed.reduce((n, g) => n + g.trials, 0)} 个试次（${removed
        .map((g) => `${FAILURE_GROUP[g.key].split("（")[0]} ${g.trials}`)
        .join("、")}）`
    : "没有剔除任何试次";
  const external = table.groups
    .filter((g) => g.side === "external")
    .map((g) => g.key);
  const toggle = (key: FailureGroup) => {
    const next = new Set(excluded);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    onChange({
      exclude: table.groups.map((g) => g.key).filter((k) => next.has(k)),
    });
  };
  const layout = settings.layout;
  const varying = table.varying.filter(
    (d) => !HIDDEN.includes(d) && (!axes || axes.includes(d)),
  );
  return (
    <div className="grid gap-3 text-sm">
      <details className="rounded-md border p-2" open={false}>
        <summary className="cursor-pointer">
          失败剔除：{summary}
          <span className="ml-1 text-xs text-muted-foreground">
            （没剔除的执行失败算 0 分、未通过；单个运行页的汇总不受影响）
          </span>
        </summary>
        <div className="mt-2 grid gap-3 md:grid-cols-2">
          {(["external", "subject"] as const).map((side) => (
            <div key={side} className="grid gap-1">
              <div className="text-xs font-medium text-muted-foreground">
                {side === "external"
                  ? "外部或平台原因（预设剔除）"
                  : "可能与被测对象有关（预设算 0 分）"}
              </div>
              {table.groups
                .filter((g) => g.side === side)
                .map((g) => (
                  <label key={g.key} className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked={excluded.has(g.key)}
                      disabled={frozen}
                      onChange={() => toggle(g.key)}
                    />
                    <span
                      className={cn(g.trials === 0 && "text-muted-foreground")}
                    >
                      {FAILURE_GROUP[g.key]}
                    </span>
                    <span className="tabular-nums text-xs text-muted-foreground">
                      {g.trials}
                    </span>
                  </label>
                ))}
            </div>
          ))}
        </div>
        {!frozen && (
          <div className="mt-2 flex flex-wrap gap-2">
            <Button
              size="sm"
              variant="outline"
              onClick={() => onChange({ exclude: external })}
            >
              剔除外部原因
            </Button>
            <Button
              size="sm"
              variant="outline"
              onClick={() => onChange({ exclude: [] })}
            >
              不剔除
            </Button>
          </div>
        )}
      </details>
      {(!frozen || matrixAvailable) && (
        <div className="flex flex-wrap items-center gap-3">
          {!frozen && (
            <Toggle
              value={settings.scope}
              options={[
                ["common", "共同任务"],
                ["all", "全部任务"],
              ]}
              onChange={(scope) =>
                onChange({ scope: scope as "common" | "all" })
              }
            />
          )}
          <Toggle
            value={
              layout.mode === "matrix" && matrixAvailable
                ? "matrix"
                : "leaderboard"
            }
            options={[
              ["leaderboard", "排行榜"],
              ...(matrixAvailable
                ? ([["matrix", "矩阵"]] as [string, string][])
                : []),
            ]}
            onChange={(mode) =>
              onChange({
                layout: {
                  ...layout,
                  mode: mode as "leaderboard" | "matrix",
                  rows:
                    mode === "matrix"
                      ? (layout.rows ?? varying[0] ?? null)
                      : null,
                  columns:
                    mode === "matrix"
                      ? (layout.columns ?? varying[1] ?? null)
                      : null,
                },
              })
            }
          />
          {layout.mode === "matrix" &&
            matrixAvailable &&
            varying.length >= 2 && (
              <Button
                size="sm"
                variant="outline"
                onClick={() => {
                  const axes = matrixAxes(table, layout, varying);
                  if (axes)
                    onChange({
                      layout: { ...layout, rows: axes[1], columns: axes[0] },
                    });
                }}
              >
                <ArrowDownUp />
                交换行列
              </Button>
            )}
          {!frozen && settings.scope === "common" && table.comparable && (
            <label className="flex items-center gap-1">
              基准
              <select
                className="h-8 max-w-[16rem] rounded-md border bg-background px-2"
                value={settings.baseline_run_id ?? table.runs[0]?.run_id ?? ""}
                onChange={(e) => onChange({ baseline_run_id: e.target.value })}
              >
                {table.runs.map((r) => (
                  <option key={r.run_id} value={r.run_id}>
                    {rowTitle(r)}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>
      )}
    </div>
  );
}

function Toggle({
  value,
  options,
  onChange,
}: {
  value: string;
  options: [string, string][];
  onChange: (value: string) => void;
}) {
  return (
    <div className="flex rounded-md border bg-background p-0.5">
      {options.map(([key, text]) => (
        <button
          key={key}
          type="button"
          className={cn(
            "rounded px-3 py-1",
            value === key
              ? "bg-primary text-primary-foreground"
              : "text-muted-foreground",
          )}
          onClick={() => onChange(key)}
        >
          {text}
        </button>
      ))}
    </div>
  );
}

type SortKey = "mean" | "pass_rate" | "valid_trials" | "title";

function Leaderboard({
  table,
  varying,
  onChange,
  evidenceOf,
}: {
  table: Comparison;
  varying: Dimension[];
  onChange: (change: ComparisonSettingsInput) => void;
  evidenceOf: (row: ComparedRunRow) => string;
}) {
  const layout = table.settings.layout;
  const sort = layout.sort;
  const rows = useMemo(() => {
    const key = sort?.key as SortKey | undefined;
    if (!key) return table.runs;
    const value = (r: ComparedRunRow) =>
      key === "title" ? rowTitle(r) : r[key];
    const sorted = [...table.runs].sort((a, b) => {
      const x = value(a);
      const y = value(b);
      if (x == null) return 1;
      if (y == null) return -1;
      return x < y ? -1 : x > y ? 1 : 0;
    });
    return sort?.descending ? sorted.reverse() : sorted;
  }, [table.runs, sort]);
  const sortBy = (key: SortKey) => {
    const next = {
      key,
      descending: sort?.key === key ? !sort.descending : key !== "title",
    };
    onChange({ layout: { ...layout, sort: next } });
  };
  const head = (key: SortKey, text: string) => (
    <TableHead>
      <button
        type="button"
        className="hover:underline"
        onClick={() => sortBy(key)}
      >
        {text}
        {sort?.key === key ? (sort.descending ? " ↓" : " ↑") : ""}
      </button>
    </TableHead>
  );
  const baseline = table.settings.baseline_run_id;
  return (
    <div className="overflow-x-auto rounded-md border">
      <Table>
        <TableHeader>
          <TableRow>
            {head("title", "运行")}
            {varying.map((d) => (
              <TableHead key={d}>{DIMENSION[d]}</TableHead>
            ))}
            <TableHead className="text-right">任务</TableHead>
            {head("valid_trials", "有效 / 计划")}
            {head("mean", "均分")}
            <TableHead>与基准差</TableHead>
            <TableHead>标准差</TableHead>
            {head("pass_rate", "通过率")}
            <TableHead>计 0 分</TableHead>
            <TableHead>剔除</TableHead>
            <TableHead>用量</TableHead>
            <TableHead>证据</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((row) => (
            <TableRow key={row.run_id}>
              <TableCell className="max-w-[18rem]">
                <Link
                  className="block truncate font-medium text-primary hover:underline"
                  to={`/runs/${row.run_id}`}
                  title={row.name}
                >
                  {rowTitle(row)}
                </Link>
                <div className="flex flex-wrap items-center gap-1 text-xs text-muted-foreground">
                  {row.run_id === baseline && <Pill tone="info">基准</Pill>}
                  {!row.terminal && (
                    <StatusPill value={label(RUN_STATE, row.state)} />
                  )}
                  {row.relation === "rerun" && <span>重跑</span>}
                  <span className="truncate" title={row.name}>
                    {row.owner_username} · {row.name}
                  </span>
                </div>
              </TableCell>
              {varying.map((d) => (
                <TableCell
                  key={d}
                  className="max-w-[14rem] truncate text-xs"
                  title={dimensionValue(table, d, row.run_id)}
                >
                  {dimensionValue(table, d, row.run_id)}
                </TableCell>
              ))}
              <TableCell className="text-right tabular-nums">
                {row.task_count}
              </TableCell>
              <TableCell className="tabular-nums text-xs">
                {row.valid_trials} / {row.compared_trials}
                {row.pending_trials > 0 && (
                  <div className="text-muted-foreground">
                    进行中 {row.pending_trials}
                  </div>
                )}
              </TableCell>
              <TableCell className="font-semibold tabular-nums">
                {fmtNum(row.mean)}
              </TableCell>
              <TableCell className="tabular-nums">
                <Diff value={row.difference?.mean} />
              </TableCell>
              <TableCell className="tabular-nums text-xs">
                {fmtNum(row.stddev)}
              </TableCell>
              <TableCell className="tabular-nums">
                {fmtPct(row.pass_rate)}
              </TableCell>
              <TableCell
                className="tabular-nums text-xs"
                title={sumGroups(row.failed_by_group)}
              >
                {row.failed_trials || "—"}
              </TableCell>
              <TableCell
                className="tabular-nums text-xs"
                title={sumGroups(row.excluded_by_group)}
              >
                {row.excluded_trials || "—"}
              </TableCell>
              <TableCell className="text-xs">
                <Usage row={row} />
              </TableCell>
              <TableCell className="text-xs">
                <span
                  className={cn(
                    evidenceOf(row) !== "available" && "text-muted-foreground",
                  )}
                >
                  {EVIDENCE[evidenceOf(row)] ?? evidenceOf(row)}
                </span>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

function Diff({ value }: { value: number | null | undefined }) {
  if (value == null) return <span className="text-muted-foreground">—</span>;
  const text = `${value > 0 ? "+" : ""}${fmtNum(value)}`;
  return (
    <span
      className={cn(
        value > 0 && "text-emerald-700",
        value < 0 && "text-red-700",
      )}
    >
      {text}
    </span>
  );
}

function Usage({ row }: { row: ComparedRunRow }) {
  const totals = Object.entries(row.usage.known_totals);
  if (!totals.length) return <span className="text-muted-foreground">—</span>;
  return (
    <span title={`${row.usage.known_trials} 个试次报告了用量`}>
      {totals
        .slice(0, 3)
        .map(([k, v]) => `${k} ${fmtNum(v, 2)}`)
        .join(" · ")}
    </span>
  );
}

function MatrixTable({
  table,
  axes,
}: {
  table: Comparison;
  axes: [Dimension, Dimension];
}) {
  const [rowAxis, columnAxis] = axes;
  const values = (axis: Dimension) => [
    ...new Set(table.runs.map((r) => dimensionValue(table, axis, r.run_id))),
  ];
  const rowValues = values(rowAxis);
  const columnValues = values(columnAxis);
  return (
    <div className="overflow-x-auto rounded-md border">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>
              {DIMENSION[rowAxis]} \ {DIMENSION[columnAxis]}
            </TableHead>
            {columnValues.map((c) => (
              <TableHead key={c} className="max-w-[14rem] truncate" title={c}>
                {c}
              </TableHead>
            ))}
          </TableRow>
        </TableHeader>
        <TableBody>
          {rowValues.map((r) => (
            <TableRow key={r}>
              <TableCell
                className="max-w-[14rem] truncate font-medium"
                title={r}
              >
                {r}
              </TableCell>
              {columnValues.map((c) => {
                const runs = table.runs.filter(
                  (x) =>
                    dimensionValue(table, rowAxis, x.run_id) === r &&
                    dimensionValue(table, columnAxis, x.run_id) === c,
                );
                return (
                  <TableCell key={c} className="tabular-nums">
                    {runs.length === 0 ? (
                      <span className="text-muted-foreground">—</span>
                    ) : (
                      runs.map((x, i) => (
                        <span key={x.run_id}>
                          {i > 0 && " / "}
                          <Link
                            to={`/runs/${x.run_id}`}
                            className="font-semibold text-primary hover:underline"
                            title={`${x.name}：有效 ${x.valid_trials}/${x.planned_trials}，通过率 ${fmtPct(x.pass_rate)}`}
                          >
                            {fmtNum(x.mean)}
                          </Link>
                        </span>
                      ))
                    )}
                  </TableCell>
                );
              })}
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

type TaskFilter = "all" | "differs" | "none_passed" | "only" | "dropped";

function passedSome(cell: ComparisonCell | null | undefined) {
  return !!cell && (cell.passed_trials ?? 0) > 0;
}

function TaskTable({ table }: { table: Comparison }) {
  const [filter, setFilter] = useState<TaskFilter>("all");
  const [only, setOnly] = useState(table.runs[0]?.run_id ?? "");
  const prefix = commonPrefix(table.tasks.map((t) => t.task_key));
  const hasPassRule = table.runs.some((r) => r.passed_trials != null);
  const shown = table.tasks.filter((task) => {
    const cells = table.runs.map((r) => task.cells[r.run_id]);
    switch (filter) {
      case "differs":
        return (
          new Set(cells.map((c) => (c?.valid_trials ? c.mean : null))).size > 1
        );
      case "none_passed":
        return task.status === "compared" && cells.every((c) => !passedSome(c));
      case "only":
        return (
          task.status === "compared" &&
          passedSome(task.cells[only]) &&
          table.runs.every(
            (r) => r.run_id === only || !passedSome(task.cells[r.run_id]),
          )
        );
      case "dropped":
        return task.status !== "compared";
      default:
        return true;
    }
  });
  const filters: [TaskFilter, string][] = [
    ["all", "全部"],
    ["differs", "结果不一致"],
    ...(hasPassRule
      ? ([
          ["none_passed", "都没通过"],
          ["only", "只有某个通过"],
        ] as [TaskFilter, string][])
      : []),
    ["dropped", "未参与比较"],
  ];
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2 text-base">
          逐任务
          <span className="text-sm font-normal text-muted-foreground">
            {shown.length} / {table.tasks.length}
          </span>
          <span className="ml-auto flex flex-wrap items-center gap-2 text-sm font-normal">
            <Toggle
              value={filter}
              options={filters}
              onChange={(v) => setFilter(v as TaskFilter)}
            />
            {filter === "only" && (
              <select
                className="h-8 max-w-[14rem] rounded-md border bg-background px-2"
                value={only}
                onChange={(e) => setOnly(e.target.value)}
              >
                {table.runs.map((r) => (
                  <option key={r.run_id} value={r.run_id}>
                    {rowTitle(r)}
                  </option>
                ))}
              </select>
            )}
          </span>
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="max-h-[36rem] overflow-auto rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>任务</TableHead>
                {table.runs.map((r) => (
                  <TableHead
                    key={r.run_id}
                    className="max-w-[10rem] truncate"
                    title={r.name}
                  >
                    {rowTitle(r)}
                  </TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {shown.map((task) => (
                <TableRow
                  key={task.task_key}
                  className={cn(
                    task.status !== "compared" && "text-muted-foreground",
                  )}
                >
                  <TableCell className="max-w-[18rem]">
                    <LongName value={task.task_key} prefix={prefix} />
                    {task.status === "no_valid_trials" && (
                      <div className="text-xs">因剔除未参与比较</div>
                    )}
                    {task.status === "not_common" && (
                      <div className="text-xs">不是共同任务</div>
                    )}
                  </TableCell>
                  {table.runs.map((r) => (
                    <TableCell key={r.run_id} className="tabular-nums">
                      <Cell runId={r.run_id} cell={task.cells[r.run_id]} />
                    </TableCell>
                  ))}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </CardContent>
    </Card>
  );
}

function Cell({
  runId,
  cell,
}: {
  runId: string;
  cell: ComparisonCell | null | undefined;
}) {
  if (!cell)
    return <span className="text-xs text-muted-foreground">未包含</span>;
  const notes = [
    cell.failed_trials
      ? `计 0 分 ${cell.failed_trials}（${sumGroups(cell.failed_by_group)}）`
      : "",
    cell.excluded_trials
      ? `剔除 ${cell.excluded_trials}（${sumGroups(cell.excluded_by_group)}）`
      : "",
  ].filter(Boolean);
  const trial = cell.trial_ids[0];
  return (
    <Link
      to={`/runs/${runId}${trial ? `?trial=${trial}` : ""}`}
      className="hover:underline"
      title={notes.join("\n") || undefined}
    >
      <span
        className={cn(
          "font-medium",
          !cell.valid_trials && "text-muted-foreground",
        )}
      >
        {cellText(cell)}
      </span>
      <span className="text-xs text-muted-foreground">
        {" "}
        ({cell.valid_trials}/{cell.planned_trials})
      </span>
      {cell.failed_trials > 0 && (
        <span className="ml-1 text-xs text-red-700">✗{cell.failed_trials}</span>
      )}
      {cell.excluded_trials > 0 && (
        <span className="ml-1 text-xs text-muted-foreground">
          −{cell.excluded_trials}
        </span>
      )}
    </Link>
  );
}
