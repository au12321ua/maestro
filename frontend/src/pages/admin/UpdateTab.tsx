import { useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ErrorAlert, Loading, Pill, StatusPill } from "@/components/common";
import { Markdown } from "@/components/Markdown";
import { ApiError, api } from "@/lib/api";
import { useInterval, useLoad, useSession } from "@/lib/hooks";
import { fmtTime, label, RUN_STATE } from "@/lib/format";
import type { ServiceUpdate, UpdateStatus } from "@/lib/types";
import { t } from "@/i18n";

function Field({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="break-words">{value}</div>
    </div>
  );
}

function UpdateCard({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">{title}</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-4 text-sm">{children}</CardContent>
    </Card>
  );
}

function UpdateRecord({ update }: { update: ServiceUpdate }) {
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <Field label={t("admin.update.version")} value={update.version} />
      <Field
        label={t("admin.update.requestedBy")}
        value={update.requested_by?.username ?? t("admin.update.system")}
      />
      <Field
        label={t("admin.update.createdAt")}
        value={fmtTime(update.created_at)}
      />
      {update.finished_at && (
        <Field
          label={t("admin.update.finishedAt")}
          value={fmtTime(update.finished_at)}
        />
      )}
      {update.finished_by && (
        <Field label={t("admin.update.cancelledBy")} value={update.finished_by.username} />
      )}
    </div>
  );
}

export function UpdateTab() {
  const { capabilities } = useSession();
  const canUpdate = capabilities?.actions["admin.update"]?.web_session === true;
  const [status, setStatus] = useState<UpdateStatus>();
  const snapshot = useRef<UpdateStatus | undefined>(undefined);
  const recovering = useRef(false);
  const revision = useRef(0);
  const [restarting, setRestarting] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<unknown>();
  const [confirmVersion, setConfirmVersion] = useState<string | null>(null);
  const [readNotes, setReadNotes] = useState(false);
  const accept = (next: UpdateStatus) => {
    snapshot.current = next;
    setStatus(next);
    recovering.current = false;
    setRestarting(false);
  };
  const transient = (error: unknown) =>
    error instanceof ApiError && [0, 502, 503].includes(error.status);
  const loaded = useLoad(async () => {
    const generation = revision.current;
    try {
      const next = (await api.get("/admin/update")).data;
      if (generation === revision.current) accept(next);
      return next;
    } catch (error) {
      if (
        snapshot.current &&
        (snapshot.current.pending || recovering.current) &&
        transient(error)
      ) {
        setRestarting(true);
        return snapshot.current;
      }
      throw error;
    }
  });
  useInterval(
    () => {
      if (!loaded.loading && !busy) void loaded.reload();
    },
    3000,
    !!status?.pending || restarting,
  );

  const perform = async (
    action: "check" | "request" | "cancel" | "now",
    version?: string,
  ) => {
    if (busy) return;
    revision.current++;
    setBusy(action);
    setActionError(undefined);
    try {
      const response =
        action === "request"
          ? await api.post("/admin/update/request", {
              body: { version: version! },
            })
          : action === "check"
            ? await api.post("/admin/update/check")
            : action === "cancel"
              ? await api.post("/admin/update/cancel")
              : await api.post("/admin/update/now");
      accept(response.data);
      setConfirmVersion(null);
      setReadNotes(false);
    } catch (error) {
      if (
        (snapshot.current?.pending || action === "request") &&
        transient(error)
      ) {
        recovering.current = true;
        setRestarting(true);
      } else setActionError(error);
    } finally {
      setBusy(null);
      void loaded.reload();
    }
  };
  const spinner = (action: string) =>
    busy === action && (
      <Loader2 className="size-4 animate-spin" aria-hidden="true" />
    );
  const pending = status?.pending;
  const latest = status?.latest;
  const last = status?.last;

  return (
    <div className="grid gap-4" data-testid="updates">
      <ErrorAlert error={restarting ? undefined : loaded.error} />
      {!!loaded.error && !status && (
        <div>
          <Button
            variant="outline"
            disabled={loaded.loading}
            onClick={() => void loaded.reload()}
          >
            {t("admin.actions.refresh")}
          </Button>
        </div>
      )}
      <ErrorAlert error={actionError} />
      {!status && loaded.loading && <Loading />}
      {restarting && (
        <p
          role="status"
          className="rounded-md border border-blue-200 bg-blue-50 p-3 text-sm text-blue-900"
        >
          {t("admin.update.restarting")}
        </p>
      )}
      {status && (
        <>
          <UpdateCard title={t("admin.update.versionTitle")}>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field
                label={t("admin.update.installed")}
                value={status.current_version}
              />
              <Field
                label={t("admin.update.repository")}
                value={status.repository}
              />
              <Field
                label={t("admin.update.checkedAt")}
                value={fmtTime(status.checked_at)}
              />
            </div>
            <p className="text-xs text-muted-foreground">
              {t("admin.update.repositoryHint")}
            </p>
            {status.check_error && (
              <p role="status" className="break-words text-amber-800">
                {t("admin.update.checkError", { error: status.check_error })}
              </p>
            )}
            <div>
              <Button
                variant="outline"
                disabled={!!busy || !canUpdate || !status.enabled}
                onClick={() => void perform("check")}
              >
                {spinner("check")}
                {t("admin.update.check")}
              </Button>
            </div>
            {latest && (
              <div className="grid gap-4 border-t pt-4">
                <div className="grid gap-3 sm:grid-cols-2">
                  <Field
                    label={t("admin.update.latest")}
                    value={latest.version}
                  />
                  <Field
                    label={t("admin.update.releaseName")}
                    value={latest.name}
                  />
                  <Field
                    label={t("admin.update.publishedAt")}
                    value={fmtTime(latest.published_at)}
                  />
                  <a
                    href={latest.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="text-primary underline"
                  >
                    {t("admin.update.releaseLink")}
                  </a>
                </div>
                {status.update_available && !pending && (
                  <>
                    {latest.highlights.map((highlight, index) => (
                      <div
                        key={index}
                        className="rounded-md border border-amber-300 bg-amber-50 p-3 text-amber-900"
                      >
                        <h3 className="mb-2 font-semibold">
                          {highlight.title}
                        </h3>
                        <Markdown>{highlight.text}</Markdown>
                      </div>
                    ))}
                    <details>
                      <summary className="cursor-pointer font-medium">
                        {t("admin.update.notes")}
                      </summary>
                      <Markdown className="mt-3">{latest.notes}</Markdown>
                    </details>
                    {confirmVersion === latest.version ? (
                      <div className="grid gap-3 rounded-md border p-4">
                        <p>
                          {t("admin.update.confirmRequest", {
                            version: confirmVersion,
                          })}
                        </p>
                        <label className="flex items-center gap-2">
                          <input
                            type="checkbox"
                            checked={readNotes}
                            onChange={(event) =>
                              setReadNotes(event.target.checked)
                            }
                          />
                          {t("admin.update.readNotes")}
                        </label>
                        <div className="flex flex-wrap gap-2">
                          <Button
                            disabled={!readNotes || !!busy || !canUpdate}
                            onClick={() => void perform("request", confirmVersion)}
                          >
                            {spinner("request")}
                            {t("admin.update.updateTo", {
                              version: confirmVersion,
                            })}
                          </Button>
                          <Button
                            variant="outline"
                            disabled={!!busy}
                            onClick={() => {
                              setConfirmVersion(null);
                              setReadNotes(false);
                            }}
                          >
                            {t("admin.update.dismiss")}
                          </Button>
                        </div>
                      </div>
                    ) : (
                      <div>
                        <Button
                          disabled={!!busy || !canUpdate || !status.enabled}
                          onClick={() => {
                            setConfirmVersion(latest.version);
                            setReadNotes(false);
                          }}
                        >
                          {t("admin.update.updateTo", {
                            version: latest.version,
                          })}
                        </Button>
                      </div>
                    )}
                  </>
                )}
                {!status.update_available && (
                  <p className="text-muted-foreground">
                    {status.current_version.includes(".dev")
                      ? t("admin.update.preview", { version: latest.version })
                      : t("admin.update.upToDate")}
                  </p>
                )}
              </div>
            )}
          </UpdateCard>
          {pending && (
            <UpdateCard
              title={
                pending.state === "draining"
                  ? t("admin.update.draining", { version: pending.version })
                  : t("admin.update.requested")
              }
            >
              <UpdateRecord update={pending} />
              {pending.state === "draining" ? (
                <>
                  <p>
                    {t("admin.update.counts", {
                      active: status.in_flight.active_runs,
                      queued: status.in_flight.queued_runs,
                    })}
                  </p>
                  <h3 className="font-medium">{t("admin.update.runs")}</h3>
                  {status.in_flight.runs.length ? (
                    <ul className="grid gap-2">
                      {status.in_flight.runs.map((run) => (
                        <li
                          key={run.id}
                          className="flex min-w-0 flex-wrap items-center gap-2"
                        >
                          <Link
                            to={`/runs/${run.id}`}
                            title={run.name}
                            className="min-w-0 max-w-full truncate text-primary underline"
                          >
                            {run.name}
                          </Link>
                          <StatusPill value={label(RUN_STATE, run.state)} />
                          <span className="break-all text-muted-foreground">
                            {run.owner.username}
                          </span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="text-muted-foreground">
                      {t("admin.update.none")}
                    </p>
                  )}
                  <h3 className="font-medium">
                    {t("admin.update.operations")}
                  </h3>
                  {status.in_flight.operations.length ? (
                    <ul className="grid gap-2">
                      {status.in_flight.operations.map((operation) => (
                        <li
                          key={operation.id}
                          className="flex flex-wrap gap-2 break-all"
                        >
                          <code>{operation.kind}</code>
                          <span>{operation.phase ?? "—"}</span>
                          <span className="text-muted-foreground">
                            {operation.owner?.username ??
                              t("admin.update.system")}
                          </span>
                        </li>
                      ))}
                    </ul>
                  ) : (
                    <p className="text-muted-foreground">
                      {t("admin.update.none")}
                    </p>
                  )}
                </>
              ) : (
                <>
                  {pending.progress && (
                    <Field
                      label={t("admin.update.phase")}
                      value={t(
                        `admin.update.phases.${pending.progress.phase}`,
                        { defaultValue: pending.progress.phase },
                      )}
                    />
                  )}
                  <p>{t("admin.update.restartHint")}</p>
                </>
              )}
              <div className="flex flex-wrap gap-2">
                <Button
                  variant="outline"
                  disabled={!!busy || !canUpdate}
                  onClick={() => {
                    if (window.confirm(t("admin.update.confirmCancel")))
                      void perform("cancel");
                  }}
                >
                  {spinner("cancel")}
                  {t("admin.update.cancel")}
                </Button>
                {pending.state === "draining" && (
                  <Button
                    variant="destructive"
                    className="h-auto min-h-9 whitespace-normal"
                    disabled={!!busy || !canUpdate}
                    onClick={() => {
                      if (window.confirm(t("admin.update.confirmNow")))
                        void perform("now");
                    }}
                  >
                    {spinner("now")}
                    {t("admin.update.now")}
                  </Button>
                )}
              </div>
            </UpdateCard>
          )}
          {last && (
            <UpdateCard title={t("admin.update.last")}>
              <div>
                <Pill
                  tone={
                    last.state === "succeeded"
                      ? "good"
                      : last.state === "failed"
                        ? "bad"
                        : "muted"
                  }
                >
                  {t(`admin.update.states.${last.state}`, {
                    defaultValue: last.state,
                  })}
                </Pill>
              </div>
              <UpdateRecord update={last} />
              {last.result?.message && (
                <p className="whitespace-pre-wrap break-words">
                  {last.result.message}
                </p>
              )}
              {last.state === "failed" && last.result?.rolled_back && (
                <p className="text-amber-800">{t("admin.update.rolledBack")}</p>
              )}
            </UpdateCard>
          )}
        </>
      )}
    </div>
  );
}
