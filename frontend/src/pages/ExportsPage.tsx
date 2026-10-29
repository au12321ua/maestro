import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Download, Loader2, RefreshCw } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { EmptyNote, ErrorAlert, Loading, PageTitle, StatusPill } from "@/components/common";
import { API, api } from "@/lib/api";
import { EXPORT_STATE, fmtBytes, fmtTime, label, shortId } from "@/lib/format";
import { useInterval } from "@/lib/hooks";
import type { Export } from "@/lib/types";
import { t } from "@/i18n";

const PENDING = new Set(["queued", "building"]);

function count(summary: Export["summary"], key: keyof NonNullable<Export["summary"]>): string {
  const value = summary?.[key];
  return typeof value === "number" ? String(value) : "—";
}

export function ExportsPage() {
  const [exports, setExports] = useState<Export[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [error, setError] = useState<unknown>();
  const [loading, setLoading] = useState(true);

  const load = useCallback(async (after: string | null) => {
    setLoading(true);
    try {
      const res = await api.get("/exports", { query: { cursor: after } });
      setExports((prev) => (after ? [...prev, ...res.data] : res.data));
      setCursor(res.meta.next_cursor ?? null);
      setError(undefined);
    } catch (err) {
      setError(err);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(null);
  }, [load]);

  const pending = exports.some((e) => PENDING.has(e.state));
  useInterval(() => void load(null), 2000, pending);

  return (
    <div>
      <PageTitle title={t("common.nav.exports")}>
        {pending && (
          <span className="flex items-center gap-1 text-xs text-muted-foreground">
            <Loader2 className="size-3 animate-spin" />
            {t("exports.building", { count: 2 })}
          </span>
        )}
        <Button variant="outline" size="sm" onClick={() => void load(null)}>
          <RefreshCw />
          {t("exports.refresh")}
        </Button>
      </PageTitle>
      <p className="mb-4 text-sm text-muted-foreground">
        {t("exports.intro")}
      </p>
      <ErrorAlert error={error} />
      {exports.length === 0 && !loading && !error && <EmptyNote>{t("exports.empty")}</EmptyNote>}
      {exports.length > 0 && (
        <div className="rounded-md border bg-card">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t("exports.column.export")}</TableHead>
                <TableHead>{t("exports.column.state")}</TableHead>
                <TableHead>{t("exports.column.runs")}</TableHead>
                <TableHead className="text-right">{t("exports.column.size")}</TableHead>
                <TableHead className="text-right">{t("exports.column.files")}</TableHead>
                <TableHead>{t("exports.column.created")}</TableHead>
                <TableHead>{t("exports.column.expires")}</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {exports.map((item) => (
                <TableRow key={item.id} data-testid="export-row">
                  <TableCell className="font-mono text-xs" title={item.id}>
                    {shortId(item.id, 16)}
                    {item.digest && (
                      <div className="text-muted-foreground" title={item.digest}>
                        {shortId(item.digest, 20)}
                      </div>
                    )}
                  </TableCell>
                  <TableCell>
                    <StatusPill value={label(EXPORT_STATE, item.state)} />
                    {item.state === "failed" && item.error && (
                      <div className="mt-1 max-w-xs text-xs text-red-700">
                        {t("common.format.withDetails", {
                          base: item.error.message ?? item.error.code,
                          details: item.error.code,
                        })}
                      </div>
                    )}
                  </TableCell>
                  <TableCell className="text-xs">
                    {item.request.run_ids.slice(0, 3).map((id) => (
                      <div key={id}>
                        <Link className="font-mono text-primary hover:underline" to={`/runs/${id}`}>
                          {shortId(id)}
                        </Link>
                      </div>
                    ))}
                    {item.request.run_ids.length > 3 && (
                      <div className="text-muted-foreground">
                        {t("exports.moreRuns", { count: item.request.run_ids.length })}
                      </div>
                    )}
                    {!item.request.include_artifacts && (
                      <div className="text-muted-foreground">{t("exports.noArtifacts")}</div>
                    )}
                  </TableCell>
                  <TableCell className="text-right text-xs tabular-nums">
                    {fmtBytes(item.size_bytes)}
                  </TableCell>
                  <TableCell className="text-right text-xs tabular-nums">
                    {count(item.summary, "files")} / {count(item.summary, "missing")} /{" "}
                    {count(item.summary, "warnings")}
                  </TableCell>
                  <TableCell className="text-xs">{fmtTime(item.created_at)}</TableCell>
                  <TableCell className="text-xs">{fmtTime(item.expires_at)}</TableCell>
                  <TableCell className="text-right">
                    {item.state === "ready" && (
                      <Button asChild size="sm" variant="outline">
                        <a href={item.links.download ?? `${API}/exports/${item.id}/download`}>
                          <Download />
                          {t("exports.download")}
                        </a>
                      </Button>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
      {loading && exports.length === 0 && <Loading />}
      {cursor && !loading && (
        <div className="mt-4 flex justify-center">
          <Button variant="outline" onClick={() => void load(cursor)}>
            {t("exports.loadMore")}
          </Button>
        </div>
      )}
    </div>
  );
}
