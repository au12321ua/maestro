import { t } from "@/i18n";
import { ApiError, api, errorText } from "@/lib/api";

const BATCH_SIZE = 500;

type BulkRecyclePath = "/attachments/bulk-delete" | "/exports/bulk-delete";

export interface BulkRecycleFailure {
  id: string;
  reason: string;
  outcome: "failed" | "unknown" | "unattempted";
}

/** Sends only the IDs captured for confirmation, stopping after a request-level failure. */
export async function bulkRecycle(path: BulkRecyclePath, ids: readonly string[]) {
  const pending = [...new Set(ids)];
  const completed = new Set<string>();
  const failures: BulkRecycleFailure[] = [];

  for (let start = 0; start < pending.length; start += BATCH_SIZE) {
    const batch = pending.slice(start, start + BATCH_SIZE);
    try {
      const { data } = await api.post(path, { body: { ids: batch } });
      const results = new Map(data.items.map((item) => [item.id, item]));
      for (const id of batch) {
        const item = results.get(id);
        if (item?.status === "deleted" || item?.status === "already_deleted") {
          completed.add(id);
        } else if (item?.status === "failed") {
          const reason = item.error
            ? errorText(new ApiError(0, { ...item.error, field_errors: item.error.field_errors ?? [] }))
            : t("common.recycle.failed");
          failures.push({ id, outcome: "failed", reason });
        } else {
          failures.push({ id, outcome: "unknown", reason: t("common.recycle.missingResult") });
        }
      }
    } catch (error) {
      const unknown = !(error instanceof ApiError) || error.indeterminate;
      const text = errorText(error);
      const reason = unknown ? t("common.recycle.unknown", { reason: text }) : text;
      for (const id of batch) failures.push({ id, outcome: unknown ? "unknown" : "failed", reason });
      const unattempted = t("common.recycle.unattempted");
      for (const id of pending.slice(start + BATCH_SIZE)) failures.push({ id, outcome: "unattempted", reason: unattempted });
      break;
    }
  }

  return { completed, failures };
}
