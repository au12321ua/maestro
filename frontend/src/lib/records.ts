// Loading a target's workflow records, shared by the records section and the pages that
// show the count in a tab or a button at the top.
import { t } from "@/i18n";
import { api } from "@/lib/api";
import { useLoad, type Loaded } from "@/lib/hooks";
import type { RecordTargetType, WorkflowRecord } from "@/lib/types";

/** The element the "records" buttons at the top of a page scroll to. */
export const RECORDS_ANCHOR = "workflow-records";

export function useRecords(targetType: RecordTargetType, targetId: string) {
  return useLoad(
    async () =>
      (
        await api.get("/records", {
          query: { target_type: targetType, target_id: targetId },
        })
      ).data,
    [targetType, targetId],
  );
}

/** "记录（n）", or just "记录" while loading. */
export function recordsLabel(records: Loaded<WorkflowRecord[]>) {
  return records.data
    ? t("common.records.withCount", { n: records.data.length })
    : t("common.records.short");
}

