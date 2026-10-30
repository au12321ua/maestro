// One collection (06 section 8): its fixed specification, the comparison of its runs with
// the settings the team keeps, its members and history, and snapshots of it.
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Camera, CopyPlus, Play, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ComparisonView } from "@/components/ComparisonView";
import {
  BackLink,
  ErrorAlert,
  Loading,
  PageTitle,
  StatusPill,
} from "@/components/common";
import { SaveSnapshot } from "@/pages/ComparePage";
import { SnapshotList } from "@/pages/CollectionsPage";
import { Trans, t } from "@/i18n";
import { api, errorText } from "@/lib/api";
import { DIMENSION, SCENARIO, valueText } from "@/lib/comparison";
import { RUN_STATE, fmtTime, label } from "@/lib/format";
import { useLoad } from "@/lib/hooks";
import type {
  Collection,
  ComparisonSettingsInput,
  Dimension,
} from "@/lib/types";

const ACTION: Record<string, string> = t("comparison.collection.action", {
  returnObjects: true,
});

export function CollectionPage() {
  const { collectionId = "" } = useParams();
  const navigate = useNavigate();
  const path = { collection_id: collectionId };
  const collection = useLoad(
    async () => (await api.get("/collections/{collection_id}", { path })).data,
    [collectionId],
  );
  const [view, setView] = useState<ComparisonSettingsInput>({});
  const table = useLoad(
    async () =>
      (
        await api.post("/collections/{collection_id}/comparison", {
          path,
          body: Object.keys(view).length ? view : null,
        })
      ).data,
    [collectionId, JSON.stringify(view)],
  );
  const snapshots = useLoad(
    async () =>
      (
        await api.get("/analysis-snapshots", {
          query: { collection_id: collectionId },
        })
      ).data,
    [collectionId],
  );
  const events = useLoad(
    async () =>
      (await api.get("/collections/{collection_id}/events", { path })).data,
    [collectionId],
  );
  const [panel, setPanel] = useState<"snapshot" | "add" | null>(null);

  const c = collection.data;
  if (!c)
    return collection.error ? (
      <div>
        <BackLink to="/collections">{t("common.nav.back.collections")}</BackLink>
        <ErrorAlert error={collection.error} />
      </div>
    ) : (
      <Loading />
    );
  const can = (action: string) =>
    c.allowed_actions.includes(action) && !c.deletion;
  // Members are named by what varies, as in the comparison above.
  const titles = new Map(
    (table.data?.runs ?? []).map((r) => [r.run_id, r.title] as const),
  );
  const reloadAll = async () => {
    await Promise.all([collection.reload(), table.reload(), events.reload()]);
  };

  const changeSettings = async (change: ComparisonSettingsInput) => {
    if (!can("edit")) {
      setView({ ...view, ...change });
      return;
    }
    try {
      await api.put("/collections/{collection_id}", {
        path,
        body: { settings: change },
      });
      await reloadAll();
    } catch (err) {
      toast.error(
        t("comparison.collection.saveSettingsFailed", { error: errorText(err) }),
      );
    }
  };
  const remove = async (runId: string) => {
    if (!window.confirm(t("comparison.collection.removeConfirm"))) return;
    try {
      await api.del("/collections/{collection_id}/runs/{run_id}", {
        path: { ...path, run_id: runId },
      });
      await reloadAll();
    } catch (err) {
      toast.error(
        t("comparison.collection.removeFailed", { error: errorText(err) }),
      );
    }
  };
  const rename = async (runId: string, current: string | null) => {
    const value = window.prompt(
      t("comparison.collection.renamePrompt"),
      current ?? "",
    );
    if (value === null) return;
    try {
      await api.put("/collections/{collection_id}/runs/{run_id}", {
        path: { ...path, run_id: runId },
        body: { label: value.trim() || null },
      });
      await reloadAll();
    } catch (err) {
      toast.error(
        t("comparison.collection.editFailed", { error: errorText(err) }),
      );
    }
  };
  const deleteCollection = async () => {
    if (!window.confirm(t("comparison.collection.deleteConfirm")))
      return;
    try {
      await api.del("/collections/{collection_id}", { path });
      toast.success(t("comparison.shared.movedToBin"));
      navigate("/collections");
    } catch (err) {
      toast.error(
        t("comparison.shared.deleteFailed", { error: errorText(err) }),
      );
    }
  };

  return (
    <div className="grid gap-4">
      <PageTitle
        title={c.name}
        back={{ to: "/collections", label: t("common.nav.back.collections") }}
      >
        {can("submit") && (
          <Button asChild size="sm">
            <Link to={`/runs/new?collection=${c.id}`}>
              <Play />
              {t("comparison.collection.newRun")}
            </Link>
          </Button>
        )}
        {can("add_runs") && (
          <Button
            size="sm"
            variant="outline"
            onClick={() => setPanel(panel === "add" ? null : "add")}
          >
            {t("comparison.collection.addRuns")}
          </Button>
        )}
        {can("snapshot") && (
          <Button
            size="sm"
            variant="outline"
            onClick={() => setPanel(panel === "snapshot" ? null : "snapshot")}
          >
            <Camera />
            {t("comparison.shared.saveSnapshot")}
          </Button>
        )}
        {c.allowed_actions.includes("copy") && (
          <Button asChild size="sm" variant="outline">
            <Link to={`/runs/new?copy_collection=${c.id}`}>
              <CopyPlus />
              {t("comparison.collection.copySpec")}
            </Link>
          </Button>
        )}
        {can("delete") && (
          <Button
            size="sm"
            variant="outline"
            onClick={() => void deleteCollection()}
          >
            <Trash2 />
            {t("comparison.shared.delete")}
          </Button>
        )}
      </PageTitle>
      {c.deletion && (
        <ErrorAlert
          error={new Error(t("comparison.collection.deletedError"))}
          title={t("comparison.collection.deletedTitle")}
        />
      )}
      <Spec collection={c} />
      {panel === "add" && (
        <AddRuns collection={c} onDone={() => void reloadAll()} />
      )}
      {panel === "snapshot" && table.data && (
        <SaveSnapshot
          table={table.data}
          collectionId={c.id}
          onClose={() => setPanel(null)}
        />
      )}
      <ErrorAlert error={table.error} />
      {table.loading && !table.data && <Loading />}
      {table.data && (
        <>
          {!can("edit") && Object.keys(view).length > 0 && (
            <p className="text-xs text-muted-foreground">
              {t("comparison.collection.viewOnly")}
            </p>
          )}
          <ComparisonView
            table={table.data}
            axes={c.variable_dimensions as Dimension[]}
            onChange={(change) => void changeSettings(change)}
            csvName={`${c.name}.csv`}
          />
        </>
      )}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            {t("comparison.collection.runsTitle", { n: c.run_count })}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <div className="overflow-x-auto rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t("comparison.shared.run")}</TableHead>
                  <TableHead>{t("comparison.collection.label")}</TableHead>
                  <TableHead>{t("comparison.shared.state")}</TableHead>
                  <TableHead>{t("comparison.collection.addedBy")}</TableHead>
                  <TableHead>{t("comparison.collection.addedAt")}</TableHead>
                  <TableHead />
                </TableRow>
              </TableHeader>
              <TableBody>
                {(c.members ?? []).map((m) => (
                  <TableRow key={m.run_id}>
                    <TableCell className="max-w-[20rem]">
                      <Link
                        className="block truncate text-primary hover:underline"
                        to={`/runs/${m.run_id}`}
                        title={m.run_name}
                      >
                        {m.run_name}
                      </Link>
                      {titles.get(m.run_id) && (
                        <div className="truncate text-xs text-muted-foreground">
                          {titles.get(m.run_id)}
                        </div>
                      )}
                    </TableCell>
                    <TableCell className="text-xs">{m.label ?? "—"}</TableCell>
                    <TableCell>
                      {m.deletion ? (
                        <span className="text-xs text-muted-foreground">
                          {t("comparison.collection.memberDeleted")}
                        </span>
                      ) : (
                        <StatusPill value={label(RUN_STATE, m.run_state)} />
                      )}
                    </TableCell>
                    <TableCell className="text-xs">
                      {m.added_by_username}
                    </TableCell>
                    <TableCell className="text-xs">
                      {fmtTime(m.added_at)}
                    </TableCell>
                    <TableCell className="whitespace-nowrap text-right">
                      {can("edit") && (
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => void rename(m.run_id, m.label)}
                        >
                          {t("comparison.collection.label")}
                        </Button>
                      )}
                      {can("remove_runs") && (
                        <Button
                          size="sm"
                          variant="ghost"
                          onClick={() => void remove(m.run_id)}
                        >
                          {t("comparison.collection.remove")}
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            {t("comparison.shared.snapshots")}
          </CardTitle>
        </CardHeader>
        <CardContent>
          {snapshots.data && snapshots.data.length === 0 && (
            <p className="text-sm text-muted-foreground">
              {t("comparison.shared.noSnapshots")}
            </p>
          )}
          {snapshots.data && snapshots.data.length > 0 && (
            <SnapshotList items={snapshots.data} />
          )}
        </CardContent>
      </Card>
      <details className="rounded-md border p-3 text-sm">
        <summary className="cursor-pointer">
          {t("comparison.collection.history")}
        </summary>
        <ul className="mt-2 grid gap-1">
          {(events.data ?? []).map((e) => (
            <li key={e.id} className="text-xs">
              {fmtTime(e.at)} · {e.actor_username}
              {e.pat_id ? t("comparison.shared.pat") : ""} · {ACTION[e.action] ?? e.action}
              {e.run_id && (
                <>
                  {" "}
                  <Link
                    className="text-primary hover:underline"
                    to={`/runs/${e.run_id}`}
                  >
                    {e.run_id}
                  </Link>
                </>
              )}
            </li>
          ))}
        </ul>
      </details>
    </div>
  );
}

function Spec({ collection: c }: { collection: Collection }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          {t("comparison.collection.spec.title")}
        </CardTitle>
      </CardHeader>
      <CardContent className="grid gap-2 text-sm">
        {c.description && (
          <p className="whitespace-pre-wrap">{c.description}</p>
        )}
        <p>
          <Trans
            i18nKey="comparison.collection.spec.scenario"
            values={{
              scenario: SCENARIO[c.scenario].label,
              dimensions:
                c.variable_dimensions
                  .map((d) => DIMENSION[d as Dimension])
                  .join(t("comparison.shared.listJoin")) ||
                t("comparison.value.none"),
            }}
            components={{ b: <b /> }}
          />
        </p>
        <div className="grid gap-1 md:grid-cols-2">
          {Object.entries(c.fixed_labels).map(([dimension, text]) => (
            <div
              key={dimension}
              className="min-w-0 truncate"
              title={valueText(text)}
            >
              <span className="text-muted-foreground">
                {t("comparison.collection.spec.fixed", {
                  dimension: DIMENSION[dimension as Dimension],
                })}
              </span>
              {dimension === "dataset" && c.template.dataset_version_id ? (
                <Link
                  className="text-primary underline"
                  to={`/datasets?version=${c.template.dataset_version_id}`}
                >
                  {valueText(text)}
                </Link>
              ) : (
                valueText(text)
              )}
            </div>
          ))}
        </div>
        <p className="text-xs text-muted-foreground">
          {t("comparison.collection.spec.immutable", {
            owner: c.owner_username,
            time: fmtTime(c.created_at),
          })}
          {c.base_run_id && (
            <Trans
              i18nKey="comparison.collection.spec.baseRun"
              values={{ id: c.base_run_id }}
              components={{
                link: (
                  <Link className="underline" to={`/runs/${c.base_run_id}`} />
                ),
              }}
            />
          )}
          {c.copied_from && (
            <Trans
              i18nKey="comparison.collection.spec.copiedFrom"
              components={{
                link: (
                  <Link
                    className="underline"
                    to={`/collections/${c.copied_from}`}
                  />
                ),
              }}
            />
          )}
        </p>
      </CardContent>
    </Card>
  );
}

function AddRuns({
  collection,
  onDone,
}: {
  collection: Collection;
  onDone: () => void;
}) {
  const path = { collection_id: collection.id };
  const candidates = useLoad(
    async () =>
      (await api.get("/collections/{collection_id}/candidates", { path })).data,
    [collection.id, collection.run_count],
  );
  const [chosen, setChosen] = useState<Set<string>>(new Set());
  const [error, setError] = useState<unknown>();
  const all = candidates.data ?? [];
  const fitting = all.filter((x) => x.mismatches.length === 0);
  const others = all.filter((x) => x.mismatches.length > 0);
  const toggle = (id: string) => {
    const next = new Set(chosen);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setChosen(next);
  };
  const add = async () => {
    setError(undefined);
    try {
      await api.post("/collections/{collection_id}/runs", {
        path,
        body: { runs: [...chosen].map((run_id) => ({ run_id })) },
      });
      toast.success(
        t("comparison.collection.add.added", { count: chosen.size }),
      );
      setChosen(new Set());
      onDone();
    } catch (err) {
      setError(err);
    }
  };
  const row = (x: (typeof all)[number], pick: boolean) => (
    <TableRow key={x.run_id}>
      {pick && (
        <TableCell className="w-8">
          <input
            type="checkbox"
            aria-label={t("comparison.collection.add.select", { name: x.name })}
            checked={chosen.has(x.run_id)}
            onChange={() => toggle(x.run_id)}
          />
        </TableCell>
      )}
      <TableCell className="max-w-[16rem]">
        <Link
          className="block truncate text-primary hover:underline"
          to={`/runs/${x.run_id}`}
          title={x.name}
        >
          {x.name}
        </Link>
        <div className="text-xs text-muted-foreground">
          {x.owner_username} · {fmtTime(x.accepted_at)}
          {x.relation === "rerun" && ` · ${t("comparison.shared.rerun")}`}
        </div>
      </TableCell>
      <TableCell className="text-xs">{x.model}</TableCell>
      <TableCell className="text-xs">{x.harness}</TableCell>
      <TableCell>
        {pick ? (
          <StatusPill value={label(RUN_STATE, x.state)} />
        ) : (
          <span className="text-xs text-muted-foreground">
            {t("comparison.collection.add.differs", {
              dimensions: [
                ...new Set(
                  x.mismatches.map((m) => DIMENSION[m.dimension as Dimension]),
                ),
              ].join(t("comparison.shared.listJoin")),
            })}
          </span>
        )}
      </TableCell>
    </TableRow>
  );
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          {t("comparison.collection.addRuns")}
        </CardTitle>
      </CardHeader>
      <CardContent className="grid gap-3">
        <ErrorAlert error={candidates.error} />
        {candidates.loading && !candidates.data && <Loading />}
        {candidates.data && (
          <>
            <p className="text-sm text-muted-foreground">
              {t("comparison.collection.add.fitting", { count: fitting.length })}
              {others.length > 0 &&
                t("comparison.collection.add.others", { count: others.length })}
            </p>
            {fitting.length > 0 && (
              <div className="max-h-[24rem] overflow-auto rounded-md border">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="w-8">
                        <input
                          type="checkbox"
                          aria-label={t("comparison.collection.add.selectAll")}
                          checked={fitting.every((x) => chosen.has(x.run_id))}
                          onChange={() =>
                            setChosen(
                              fitting.every((x) => chosen.has(x.run_id))
                                ? new Set()
                                : new Set(fitting.map((x) => x.run_id)),
                            )
                          }
                        />
                      </TableHead>
                      <TableHead>{t("comparison.shared.run")}</TableHead>
                      <TableHead>{t("comparison.shared.model")}</TableHead>
                      <TableHead>Harness</TableHead>
                      <TableHead>{t("comparison.shared.state")}</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>{fitting.map((x) => row(x, true))}</TableBody>
                </Table>
              </div>
            )}
            <ErrorAlert
              error={error}
              title={t("comparison.collection.add.notAdded")}
            />
            <div>
              <Button
                size="sm"
                disabled={chosen.size === 0}
                onClick={() => void add()}
              >
                {t("comparison.collection.add.addSelected")}
                {chosen.size > 0 &&
                  t("comparison.collection.add.selectedCount", {
                    n: chosen.size,
                  })}
              </Button>
            </div>
            {others.length > 0 && (
              <details className="text-sm">
                <summary className="cursor-pointer text-muted-foreground">
                  {t("comparison.collection.add.mismatched", {
                    n: others.length,
                  })}
                </summary>
                <div className="mt-2 max-h-[24rem] overflow-auto rounded-md border">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>{t("comparison.shared.run")}</TableHead>
                        <TableHead>{t("comparison.shared.model")}</TableHead>
                        <TableHead>Harness</TableHead>
                        <TableHead>
                          {t("comparison.collection.add.reason")}
                        </TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>{others.map((x) => row(x, false))}</TableBody>
                  </Table>
                </div>
              </details>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}
