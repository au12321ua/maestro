import { useState } from "react";
import { Link } from "react-router-dom";
import { Download, Loader2, RefreshCw, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  EmptyNote,
  ErrorAlert,
  Loading,
  PageTitle,
  StatusPill,
} from "@/components/common";
import { API, api } from "@/lib/api";
import { EXPORT_STATE, fmtBytes, fmtTime, label, shortId } from "@/lib/format";
import { useInterval, useLoad } from "@/lib/hooks";
import type { Export } from "@/lib/types";
import { t, translateCode } from "@/i18n";

const PENDING = new Set(["queued", "building"]);

function count(
  summary: Export["summary"],
  key: keyof NonNullable<Export["summary"]>,
): string {
  const value = summary?.[key];
  return typeof value === "number" ? String(value) : "—";
}

export function ExportsPage() {
  const [pages, setPages] = useState<string[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  const [notice, setNotice] = useState("");
  const [failures, setFailures] = useState<{ id: string; reason: string }[]>(
    [],
  );
  const cursor = pages.at(-1);
  const page = useLoad(
    async () => ({
      ...(await api.get("/exports", { query: { cursor, limit: 25 } })),
      cursor,
    }),
    [cursor],
  );
  const current = page.data?.cursor === cursor ? page.data : undefined;
  const exports = current?.data ?? [];
  const next = current?.meta.next_cursor;
  const pending = exports.some((item) => PENDING.has(item.state));
  useInterval(
    () => void page.reload(),
    2000,
    pending && !busy && !page.loading,
  );

  function toggle(id: string) {
    setSelected((previous) => {
      const updated = new Set(previous);
      if (updated.has(id)) updated.delete(id);
      else updated.add(id);
      return updated;
    });
  }

  async function selectExpired() {
    setBusy(true);
    setError(undefined);
    try {
      const { data } = await api.get("/exports/expired-selection");
      setSelected(new Set(data.ids));
      setNotice(t("exports.expiredSelected", { count: data.count }));
      setFailures([]);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  }

  async function recycle(ids: string[]) {
    if (!window.confirm(t("exports.confirmRecycle", { count: ids.length })))
      return;
    setBusy(true);
    setError(undefined);
    setNotice("");
    setFailures([]);
    const completed = new Set<string>();
    const failed = [];
    try {
      for (let offset = 0; offset < ids.length; offset += 500) {
        const { data } = await api.post("/exports/bulk-delete", {
          body: { ids: ids.slice(offset, offset + 500) },
        });
        for (const item of data.items) {
          if (item.status === "failed") {
            const reason = item.error
              ? (translateCode(item.error.code) ?? item.error.message)
              : t("exports.recycleFailed");
            failed.push({ id: item.id, reason });
          } else {
            completed.add(item.id);
          }
        }
      }
    } catch (err) {
      setError(err);
    } finally {
      setSelected((previous) => {
        const remaining = new Set(previous);
        for (const id of ids) {
          if (completed.has(id)) remaining.delete(id);
          else remaining.add(id);
        }
        return remaining;
      });
      setFailures(failed);
      setNotice(t("exports.recycled", { count: completed.size }));
      await page.reload();
      setBusy(false);
    }
  }

  return (
    <div>
      <PageTitle title={t("common.nav.exports")}>
        {pending && (
          <span className="flex items-center gap-1 text-xs text-muted-foreground">
            <Loader2 className="size-3 animate-spin" />
            {t("exports.building", { count: 2 })}
          </span>
        )}
        <Button
          variant="outline"
          size="sm"
          disabled={page.loading || busy}
          onClick={() => void page.reload()}
        >
          <RefreshCw />
          {t("exports.refresh")}
        </Button>
      </PageTitle>
      <p className="mb-4 text-sm text-muted-foreground">{t("exports.intro")}</p>
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <Button
          size="sm"
          variant="outline"
          disabled={busy}
          onClick={() => void selectExpired()}
        >
          {t("exports.selectExpired")}
        </Button>
        {selected.size > 0 && (
          <>
            <Button
              size="sm"
              variant="destructive"
              disabled={busy}
              onClick={() => void recycle([...selected])}
            >
              <Trash2 />
              {t("exports.recycleSelected", { count: selected.size })}
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={busy}
              onClick={() => {
                setSelected(new Set());
                setNotice("");
              }}
            >
              {t("exports.clearSelection")}
            </Button>
          </>
        )}
      </div>
      <ErrorAlert error={error ?? page.error} />
      {notice && (
        <p role="status" className="mb-2 text-sm">
          {notice}
        </p>
      )}
      {failures.length > 0 && (
        <ul
          role="alert"
          className="mb-3 list-inside list-disc text-sm text-destructive"
        >
          {failures.map(({ id, reason }) => (
            <li key={id}>
              {id} · {reason}
            </li>
          ))}
        </ul>
      )}
      {exports.length === 0 && !page.loading && !page.error && !error && (
        <EmptyNote>
          {cursor ? t("exports.emptyPage") : t("exports.empty")}
        </EmptyNote>
      )}
      {exports.length > 0 && (
        <div className="rounded-md border bg-card">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-8">
                  <span className="sr-only">{t("exports.select")}</span>
                </TableHead>
                <TableHead>{t("exports.column.export")}</TableHead>
                <TableHead>{t("exports.column.state")}</TableHead>
                <TableHead>{t("exports.column.runs")}</TableHead>
                <TableHead className="text-right">
                  {t("exports.column.size")}
                </TableHead>
                <TableHead className="text-right">
                  {t("exports.column.files")}
                </TableHead>
                <TableHead>{t("exports.column.created")}</TableHead>
                <TableHead>{t("exports.column.expires")}</TableHead>
                <TableHead />
              </TableRow>
            </TableHeader>
            <TableBody>
              {exports.map((item) => (
                <TableRow key={item.id} data-testid="export-row">
                  <TableCell>
                    <input
                      type="checkbox"
                      aria-label={t("exports.selectExport", { id: item.id })}
                      checked={selected.has(item.id)}
                      disabled={busy || PENDING.has(item.state)}
                      onChange={() => toggle(item.id)}
                    />
                  </TableCell>
                  <TableCell className="font-mono text-xs" title={item.id}>
                    {shortId(item.id, 16)}
                    {item.digest && (
                      <div
                        className="text-muted-foreground"
                        title={item.digest}
                      >
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
                        <Link
                          className="font-mono text-primary hover:underline"
                          to={`/runs/${id}`}
                        >
                          {shortId(id)}
                        </Link>
                      </div>
                    ))}
                    {item.request.run_ids.length > 3 && (
                      <div className="text-muted-foreground">
                        {t("exports.moreRuns", {
                          count: item.request.run_ids.length,
                        })}
                      </div>
                    )}
                    {!item.request.include_artifacts && (
                      <div className="text-muted-foreground">
                        {t("exports.noArtifacts")}
                      </div>
                    )}
                  </TableCell>
                  <TableCell className="text-right text-xs tabular-nums">
                    {fmtBytes(item.size_bytes)}
                  </TableCell>
                  <TableCell className="text-right text-xs tabular-nums">
                    {count(item.summary, "files")} /{" "}
                    {count(item.summary, "missing")} /{" "}
                    {count(item.summary, "warnings")}
                  </TableCell>
                  <TableCell className="text-xs">
                    {fmtTime(item.created_at)}
                  </TableCell>
                  <TableCell className="text-xs">
                    {fmtTime(item.expires_at)}
                  </TableCell>
                  <TableCell>
                    <div className="flex justify-end gap-1">
                      {item.state === "ready" && (
                        <Button asChild size="sm" variant="outline">
                          <a
                            href={
                              item.links.download ??
                              `${API}/exports/${item.id}/download`
                            }
                          >
                            <Download />
                            {t("exports.download")}
                          </a>
                        </Button>
                      )}
                      {!PENDING.has(item.state) && (
                        <Button
                          variant="outline"
                          size="sm"
                          disabled={busy}
                          onClick={() => void recycle([item.id])}
                          aria-label={t("exports.recycleExport", {
                            id: item.id,
                          })}
                        >
                          <Trash2 />
                          {t("exports.recycle")}
                        </Button>
                      )}
                    </div>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
      {page.loading && !current && <Loading />}
      {(cursor || next) && (
        <div className="mt-4 flex items-center justify-center gap-3">
          <Button
            variant="outline"
            disabled={busy || !cursor}
            onClick={() => setPages(pages.slice(0, -1))}
          >
            {t("exports.previous")}
          </Button>
          <span className="text-sm">
            {t("exports.page", { number: pages.length + 1 })}
          </span>
          <Button
            variant="outline"
            disabled={busy || !next}
            onClick={() => next && setPages([...pages, next])}
          >
            {t("exports.next")}
          </Button>
        </div>
      )}
    </div>
  );
}
