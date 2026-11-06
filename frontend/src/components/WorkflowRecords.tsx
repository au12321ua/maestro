import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyNote, ErrorAlert, Loading } from "@/components/common";
import { t } from "@/i18n";
import { api } from "@/lib/api";
import { fmtTime } from "@/lib/format";
import { useLoad } from "@/lib/hooks";

export function WorkflowRecords({
  targetType,
  targetId,
}: {
  targetType: "collection" | "run" | "attachment" | "dataset_version";
  targetId: string;
}) {
  const records = useLoad(
    async () =>
      (
        await api.get("/records", {
          query: { target_type: targetType, target_id: targetId },
        })
      ).data,
    [targetType, targetId],
  );
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{t("common.records.title")}</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-4 text-sm">
        <ErrorAlert error={records.error} />
        {records.loading && !records.data && <Loading />}
        {records.data?.length === 0 && (
          <EmptyNote>{t("common.records.empty")}</EmptyNote>
        )}
        {records.data?.map((record) => (
          <article key={record.id} className="min-w-0 rounded-md border p-3">
            <p className="break-words font-medium">{record.title}</p>
            <p className="break-all text-xs text-muted-foreground">
              {record.kind} · {record.author_username} ·{" "}
              {fmtTime(record.created_at)}
              {" · "}
              {t("common.records.revisions", { count: record.revision })}
              {record.pat_id && ` · ${t("common.records.pat")}`}
            </p>
            {record.revision > 1 && (
              <p className="text-xs text-muted-foreground">
                {t("common.records.updated", {
                  time: fmtTime(record.updated_at),
                })}
              </p>
            )}
            {record.target.snapshot_id && (
              <p className="break-all text-xs text-muted-foreground">
                {t("common.records.snapshot", {
                  id: record.target.snapshot_id,
                })}
              </p>
            )}
            <details className="mt-2">
              <summary className="cursor-pointer text-xs">
                {t("common.records.body")}
              </summary>
              <pre className="mt-2 max-h-96 overflow-auto rounded-md bg-muted p-3 text-xs">
                {JSON.stringify(record.body, null, 2)}
              </pre>
              {record.references.length > 0 && (
                <pre className="mt-2 max-h-48 overflow-auto rounded-md bg-muted p-3 text-xs">
                  {JSON.stringify(record.references, null, 2)}
                </pre>
              )}
            </details>
            {record.excerpts.length > 0 && (
              <div className="mt-3 grid gap-2">
                <p className="text-xs text-muted-foreground">
                  {t("common.records.excerpts")}
                </p>
                {record.excerpts.map((excerpt, index) => (
                  <blockquote key={index} className="border-l-2 pl-3">
                    <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words text-xs">
                      {excerpt.text}
                    </pre>
                    {excerpt.reference && (
                      <p className="break-all text-xs text-muted-foreground">
                        {excerpt.reference.type} · {excerpt.reference.id}
                      </p>
                    )}
                  </blockquote>
                ))}
              </div>
            )}
          </article>
        ))}
      </CardContent>
    </Card>
  );
}
