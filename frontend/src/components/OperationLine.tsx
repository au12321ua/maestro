// One Operation (dataset import, first preparation, eviction) followed until it ends.
import { useState } from "react";
import { Loading, StatusPill } from "@/components/common";
import { t } from "@/i18n";
import { api } from "@/lib/api";
import { OPERATION_STATUS, shortId } from "@/lib/format";
import { useInterval, useLoad } from "@/lib/hooks";
import type { Operation } from "@/lib/types";

const PHASES: Record<string, string> = t("datasets.phase", { returnObjects: true });

const TERMINAL = new Set(["succeeded", "failed", "cancelled", "interrupted"]);

/** Polls one Operation until it ends, then calls `onDone` once. */
export function OperationLine({
  id,
  label,
  onDone,
}: {
  id: string;
  label: string;
  onDone?: (op: Operation) => void;
}) {
  const [notified, setNotified] = useState(false);
  const op = useLoad(
    async () => (await api.get("/operations/{operation_id}", { path: { operation_id: id } })).data,
    [id],
  );
  const running = !op.data || !TERMINAL.has(op.data.status);
  useInterval(() => void op.reload(), 1000, running);
  if (op.data && !running && !notified) {
    setNotified(true);
    onDone?.(op.data);
  }
  if (!op.data) return <Loading text={`${label}…`} />;
  // Imports and first preparations count tasks; other kinds have no task count.
  const progress =
    op.data.kind === "dataset_import" || op.data.kind === "preparation_first"
      ? op.data.progress
      : null;
  const prepared = op.data.kind === "preparation_first" ? op.data.progress : null;
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-md border px-3 py-2 text-sm">
      <span className="font-medium">{label}</span>
      <StatusPill value={OPERATION_STATUS[op.data.status] ?? [op.data.status, "neutral"]} />
      {op.data.phase && running && (
        <span className="text-muted-foreground">{PHASES[op.data.phase] ?? op.data.phase}</span>
      )}
      {progress?.tasks_total != null && (
        <span className="text-xs text-muted-foreground">
          {t("datasets.operation.tasks", {
            done: progress.tasks_done ?? 0,
            count: progress.tasks_total,
          })}
          {prepared?.admitted != null &&
            t("datasets.operation.admitted", { n: prepared.admitted })}
          {prepared?.failed ? t("datasets.operation.failed", { n: prepared.failed }) : ""}
        </span>
      )}
      {op.data.error && (
        <span className="text-xs text-destructive">
          {t("datasets.operation.error", {
            message: op.data.error.message ?? op.data.error.code,
            code: op.data.error.code,
          })}
        </span>
      )}
      <code className="ml-auto text-xs text-muted-foreground">{shortId(op.data.id, 16)}</code>
    </div>
  );
}

