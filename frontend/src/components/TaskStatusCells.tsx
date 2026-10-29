import { TableCell } from "@/components/ui/table";
import { Pill } from "@/components/common";
import { t } from "@/i18n";
import { blockerText, CACHE_STATE, fmtTime, type Tone } from "@/lib/format";
import type { Task } from "@/lib/types";

const ADMISSION_TEXT = t("runs.taskStatus.admission", { returnObjects: true });
const ADMISSION: Record<string, [string, Tone]> = {
  admitted: [ADMISSION_TEXT.admitted, "good"],
  pending: [ADMISSION_TEXT.pending, "warn"],
  first_failed: [ADMISSION_TEXT.first_failed, "bad"],
  revoked: [ADMISSION_TEXT.revoked, "bad"],
};
const AVAILABILITY_TEXT = t("runs.taskStatus.availability", { returnObjects: true });
const AVAILABILITY: Record<string, [string, Tone]> = {
  available: [AVAILABILITY_TEXT.available, "good"],
  unavailable: [AVAILABILITY_TEXT.unavailable, "bad"],
  unknown: [AVAILABILITY_TEXT.unknown, "muted"],
};

/** Admission, content availability, local cache, can_submit and reasons for one task row.
 * The three facts are separate: a cold cache never blocks submission (02 section 5). */
export function TaskStatusCells({ task }: { task: Task }) {
  const [availability, tone] = AVAILABILITY[task.content.availability] ?? [
    task.content.availability,
    "neutral" as Tone,
  ];
  const cache = CACHE_STATE[task.local_preparation.cache_state] ?? [
    task.local_preparation.cache_state,
    "neutral" as Tone,
  ];
  const restoreError = task.local_preparation.last_restore_error;
  const notes = [
    ...task.blockers.map(blockerText),
    ...task.warnings.map((w) =>
      w.code === "PREPARATION_CACHE_MISS" ? t("runs.taskStatus.cacheMiss") : w.code,
    ),
  ];
  if (restoreError?.message) notes.push(t("runs.taskStatus.restoreFailed", { error: String(restoreError.message) }));
  return (
    <>
      <TableCell>
        {task.admission ? (
          <Pill tone={ADMISSION[task.admission.state]?.[1] ?? "neutral"}>
            {ADMISSION[task.admission.state]?.[0] ?? task.admission.state}
          </Pill>
        ) : (
          <Pill tone="muted">{t("runs.taskStatus.notAdmitted")}</Pill>
        )}
      </TableCell>
      <TableCell>
        <Pill
          tone={tone}
          title={task.content.resources
            .map((r) =>
              t("runs.taskStatus.resource", {
                kind: r.resource_kind,
                digest: r.digest,
                availability: r.availability,
              }),
            )
            .concat(t("runs.taskStatus.checkedAt", { time: fmtTime(task.content.checked_at) }))
            .join("\n")}
        >
          {availability}
        </Pill>
      </TableCell>
      <TableCell>
        <Pill tone={cache[1]} title={t("runs.taskStatus.location", { id: task.local_preparation.location_id })}>
          {cache[0]}
        </Pill>
      </TableCell>
      <TableCell>
        {task.can_submit ? (
          <Pill tone="good">{t("runs.taskStatus.submittable")}</Pill>
        ) : (
          <Pill tone="bad">{t("runs.taskStatus.notSubmittable")}</Pill>
        )}
      </TableCell>
      <TableCell className="max-w-md text-xs text-muted-foreground">
        {notes.length === 0 ? "—" : notes.join(t("common.format.listSeparator"))}
      </TableCell>
    </>
  );
}
