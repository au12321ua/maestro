import { useState } from "react";
import { ErrorAlert, Loading } from "@/components/common";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { t } from "@/i18n";
import { ApiError, api } from "@/lib/api";
import {
  ARTIFACT_REASON,
  ARTIFACT_STATUS,
  fmtBytes,
  fmtTime,
  label,
  shortId,
} from "@/lib/format";
import type { components } from "@/lib/schema";

const PATH = "/runs/{run_id}/artifact-retention";
type Retention = components["schemas"]["ArtifactRetention"];
type RetentionFile = components["schemas"]["ArtifactRetentionFile"];

function FileList({
  files,
  reason,
}: {
  files: RetentionFile[];
  reason: "status" | "expiry";
}) {
  return (
    <ul className="grid max-h-44 gap-1 overflow-auto text-xs">
      {files.map((file) => (
        <li
          key={file.artifact_id}
          className="flex flex-wrap justify-between gap-x-3"
        >
          <span className="min-w-0 break-all">
            {file.relative_path}
            {file.attempt_id && (
              <span
                className="ml-2 text-muted-foreground"
                title={file.attempt_id}
              >
                {shortId(file.attempt_id)}
              </span>
            )}
          </span>
          <span className="text-muted-foreground">
            {reason === "expiry"
              ? fmtTime(file.expires_at)
              : (ARTIFACT_REASON[file.status_reason ?? ""] ??
                label(ARTIFACT_STATUS, file.status)[0])}
          </span>
        </li>
      ))}
    </ul>
  );
}

export function RunArtifactRetention({
  runId,
  retainedBytes,
  recycled,
  onChange,
}: {
  runId: string;
  retainedBytes: number | null | undefined;
  recycled: boolean;
  onChange: () => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [view, setView] = useState<Retention>();
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const options = { path: { run_id: runId } };
  const load = async () => {
    setBusy(true);
    setError(undefined);
    try {
      setView((await api.get(PATH, options)).data);
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  const change = async (action: "retain" | "release") => {
    setBusy(true);
    setError(undefined);
    try {
      const send = action === "retain" ? api.put : api.del;
      setView((await send(PATH, options)).data);
      await onChange();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  const missing =
    view?.files.filter((file) => file.status !== "available") ?? [];
  const expiredOnRelease =
    view?.files.filter(
      (file) =>
        file.retained && file.past_expiry && file.status === "available",
    ) ?? [];
  const canRetain =
    !recycled &&
    view?.files.some((file) => file.status === "available" && !file.retained);
  const canRelease = view?.files.some((file) => file.retained);
  const shortfall =
    error instanceof ApiError
      ? error.body.suggested_actions?.find(
          (action) => action.type === "release_artifact_retention",
        )?.shortfall_bytes
      : undefined;

  return (
    <Card className="mb-3">
      <CardHeader className="flex-row flex-wrap items-center justify-between gap-2 py-4">
        <CardTitle className="text-sm">
          {retainedBytes != null
            ? t("runs.retention.retainedSize", {
                size: fmtBytes(retainedBytes),
              })
            : t("runs.retention.title")}
        </CardTitle>
        <Button
          size="sm"
          variant="outline"
          aria-expanded={open}
          disabled={busy}
          onClick={() => {
            setOpen(!open);
            if (!open) void load();
          }}
        >
          {t(
            open
              ? "runs.retention.close"
              : retainedBytes == null && !recycled
                ? "runs.retention.retain"
                : "runs.retention.manage",
          )}
        </Button>
      </CardHeader>
      {open && (
        <CardContent className="grid gap-3 text-sm">
          {typeof shortfall === "number" ? (
            <Alert variant="destructive">
              <AlertDescription>
                {t("runs.retention.shortfall", { size: fmtBytes(shortfall) })}
              </AlertDescription>
            </Alert>
          ) : (
            <ErrorAlert error={error} />
          )}
          {!view && busy && <Loading />}
          {view && (
            <>
              <p>
                {t("runs.retention.quota", {
                  used: fmtBytes(view.used_bytes),
                  quota: fmtBytes(view.quota_bytes),
                })}
              </p>
              <p>
                {t("runs.retention.available", {
                  size: fmtBytes(view.available_bytes),
                  additional: fmtBytes(view.additional_bytes),
                })}
              </p>
              {missing.length > 0 && (
                <details>
                  <summary className="w-fit cursor-pointer">
                    {t("runs.retention.missing", { count: missing.length })}
                  </summary>
                  <FileList files={missing} reason="status" />
                </details>
              )}
              {canRelease && (
                <p className="text-muted-foreground">
                  {t("runs.retention.releaseHint")}
                </p>
              )}
              {expiredOnRelease.length > 0 && (
                <Alert>
                  <AlertDescription>
                    <p>
                      {t("runs.retention.releaseExpired", {
                        count: expiredOnRelease.length,
                      })}
                    </p>
                    <FileList files={expiredOnRelease} reason="expiry" />
                  </AlertDescription>
                </Alert>
              )}
              <div className="flex flex-wrap gap-2">
                {canRetain && (
                  <Button
                    size="sm"
                    disabled={busy}
                    onClick={() => void change("retain")}
                  >
                    {t("runs.retention.confirm")}
                  </Button>
                )}
                {canRelease && (
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={busy}
                    onClick={() => void change("release")}
                  >
                    {t("runs.retention.release")}
                  </Button>
                )}
              </div>
            </>
          )}
        </CardContent>
      )}
    </Card>
  );
}
