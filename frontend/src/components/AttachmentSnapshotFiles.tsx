import { useState } from "react";
import { ErrorAlert, Loading } from "@/components/common";
import { t } from "@/i18n";
import { API, api } from "@/lib/api";
import { fmtBytes, snapshotName } from "@/lib/format";
import { useLoad } from "@/lib/hooks";
import { useTopicScope } from "@/lib/topics";
import type { Attachment, AttachmentDiff, AttachmentFile } from "@/lib/types";

// Larger files are offered for download instead of being loaded into the browser.
const VIEW_MAX_BYTES = 256 * 1024;

function fileUrl(id: string, path: string): string {
  const parts = path.split("/").map(encodeURIComponent).join("/");
  return `${API}/attachments/${encodeURIComponent(id)}/files/${parts}`;
}

function FileView({ id, file }: { id: string; file: AttachmentFile }) {
  const view = useLoad(async () => {
    if (file.size > VIEW_MAX_BYTES) return null;
    const response = await fetch(fileUrl(id, file.path), { credentials: "same-origin" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    try {
      return new TextDecoder("utf-8", { fatal: true }).decode(await response.arrayBuffer());
    } catch {
      return null;
    }
  }, [id, file.path]);
  if (view.loading && view.data === undefined) return <Loading />;
  if (view.error) return <ErrorAlert error={view.error} />;
  if (view.data === null)
    return <p className="text-muted-foreground">{t("attachments.file.binary")}</p>;
  return (
    <pre className="max-h-96 overflow-auto rounded-md bg-background p-2 text-[11px]">
      {view.data}
    </pre>
  );
}

function DiffView({ diff }: { diff: AttachmentDiff }) {
  const rows: [string, string, string][] = [
    ...diff.added.map((f): [string, string, string] => ["+", f.path, "text-emerald-700"]),
    ...diff.removed.map((f): [string, string, string] => ["−", f.path, "text-red-700"]),
    ...diff.changed.map((c): [string, string, string] => [
      "~",
      c.before.sha256 === c.after.sha256 ? t("attachments.diff.modeOnly", { path: c.path }) : c.path,
      "text-amber-700",
    ]),
  ];
  if (rows.length === 0) return <p className="text-muted-foreground">{t("attachments.diff.none")}</p>;
  return (
    <ul className="grid max-h-48 gap-0.5 overflow-auto font-mono text-xs">
      {rows.map(([mark, path, tone]) => (
        <li key={`${mark}${path}`} className={tone}>
          {mark} {path}
        </li>
      ))}
    </ul>
  );
}

export function AttachmentSnapshotFiles({ detail }: { detail: Attachment }) {
  const [viewing, setViewing] = useState<string | null>(null);
  return (
    <div className="grid gap-3 text-xs">
      {detail.description && <p>{detail.description}</p>}
      {(detail.provides.tools.length > 0 || detail.provides.env.length > 0) && (
        <p>
          {t("attachments.detail.provides")}
          {[
            ...detail.provides.tools.map((x) => t("attachments.detail.tool", { name: x })),
            ...detail.provides.env.map((x) => t("attachments.detail.env", { name: x })),
          ].join(t("attachments.separator"))}
        </p>
      )}
      <ul className="grid max-h-80 gap-0.5 overflow-auto font-mono">
        {(detail.files ?? []).map((f) => (
          <li key={f.path} title={f.sha256}>
            <div className="flex flex-wrap items-center gap-2">
              <span>
                {f.executable ? "x " : "  "}
                {f.path} <span className="text-muted-foreground">({fmtBytes(f.size)})</span>
              </span>
              <button
                type="button"
                className="underline-offset-2 hover:underline"
                onClick={() => setViewing(viewing === f.path ? null : f.path)}
              >
                {viewing === f.path ? t("attachments.file.collapse") : t("attachments.file.view")}
              </button>
              <a className="underline-offset-2 hover:underline" href={fileUrl(detail.id, f.path)}>
                {t("attachments.download")}
              </a>
            </div>
            {viewing === f.path && <FileView id={detail.id} file={f} />}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function AttachmentComparison({ detail, baseId }: { detail: Attachment; baseId: string }) {
  const { topicId } = useTopicScope();
  const comparison = useLoad(async () => {
    const [base, diff] = await Promise.all([
      api.get("/attachment-directory/version", { query: { snapshot_id: baseId, topic_id: topicId } }),
      api.get("/attachments/{attachment_id}/diff", { path: { attachment_id: detail.id }, query: { base_id: baseId } }),
    ]);
    return { base: base.data, diff: diff.data };
  }, [baseId, topicId]);
  const base = comparison.data?.base;
  if (!base) return comparison.loading ? <Loading /> : <ErrorAlert error={comparison.error} />;
  if (base.snapshot.name !== detail.name || base.snapshot.kind !== detail.kind || base.snapshot.id === detail.id) {
    return <p role="status">{t("attachments.detail.invalidBase")}</p>;
  }
  return (
    <div className="grid gap-2 text-sm">
      <h3 className="font-medium">{t("attachments.detail.comparison", { snapshot: snapshotName(base.snapshot) })}</h3>
      {topicId && !base.in_topic && <p className="text-muted-foreground">{t("attachments.detail.baseOutsideTopic")}</p>}
      <ErrorAlert error={comparison.error} />
      {comparison.data && <DiffView diff={comparison.data.diff} />}
    </div>
  );
}
