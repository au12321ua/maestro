// Collections (06 section 8): run lists the team keeps up, each with a specification fixed
// at creation; and the snapshots that froze a comparison.
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { Plus } from "lucide-react";
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
import {
  EmptyNote,
  ErrorAlert,
  Loading,
  PageTitle,
  TextAreaField,
  TextField,
} from "@/components/common";
import { ScenarioPicker } from "@/pages/ComparePage";
import { t } from "@/i18n";
import { api } from "@/lib/api";
import { DIMENSION, SCENARIO } from "@/lib/comparison";
import { fmtTime } from "@/lib/format";
import { useLoad } from "@/lib/hooks";
import type { Dimension, Scenario, VariableDimension } from "@/lib/types";

export function CollectionsPage() {
  const collections = useLoad(async () => (await api.get("/collections")).data);
  const snapshots = useLoad(
    async () => (await api.get("/analysis-snapshots")).data,
  );
  const [creating, setCreating] = useState(false);
  return (
    <div className="grid gap-4">
      <PageTitle title={t("comparison.collections.title")}>
        <Button size="sm" onClick={() => setCreating(!creating)}>
          <Plus />
          {t("comparison.collections.new")}
        </Button>
      </PageTitle>
      <p className="text-sm text-muted-foreground">
        {t("comparison.collections.intro")}
      </p>
      {creating && <NewCollection onClose={() => setCreating(false)} />}
      <ErrorAlert error={collections.error} />
      {collections.loading && !collections.data && <Loading />}
      {collections.data && collections.data.length === 0 && (
        <EmptyNote>{t("comparison.collections.empty")}</EmptyNote>
      )}
      {collections.data && collections.data.length > 0 && (
        <div className="overflow-x-auto rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t("comparison.shared.name")}</TableHead>
                <TableHead>{t("comparison.shared.scenario")}</TableHead>
                <TableHead>{t("comparison.collections.dataset")}</TableHead>
                <TableHead className="text-right">
                  {t("comparison.shared.run")}
                </TableHead>
                <TableHead>{t("comparison.collections.owner")}</TableHead>
                <TableHead>{t("comparison.collections.updatedAt")}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {collections.data.map((c) => (
                <TableRow key={c.id}>
                  <TableCell className="max-w-[24rem]">
                    <Link
                      className="block truncate font-medium text-primary hover:underline"
                      to={`/collections/${c.id}`}
                      title={c.name}
                    >
                      {c.name}
                    </Link>
                    {c.description && (
                      <div
                        className="truncate text-xs text-muted-foreground"
                        title={c.description}
                      >
                        {c.description}
                      </div>
                    )}
                  </TableCell>
                  <TableCell className="text-xs">
                    {SCENARIO[c.scenario].label}
                    <div className="text-muted-foreground">
                      {t("comparison.shared.variable")}
                      {c.variable_dimensions
                        .map((d) => DIMENSION[d as Dimension])
                        .join(t("comparison.shared.listJoin")) ||
                        t("comparison.value.none")}
                    </div>
                  </TableCell>
                  <TableCell className="text-xs">
                    {c.fixed_labels.dataset}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {c.run_count}
                  </TableCell>
                  <TableCell className="text-xs">{c.owner_username}</TableCell>
                  <TableCell className="text-xs">
                    {fmtTime(c.updated_at)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            {t("comparison.shared.snapshots")}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <ErrorAlert error={snapshots.error} />
          {snapshots.data && snapshots.data.length === 0 && (
            <EmptyNote>{t("comparison.collections.noSnapshots")}</EmptyNote>
          )}
          {snapshots.data && snapshots.data.length > 0 && (
            <SnapshotList items={snapshots.data} />
          )}
        </CardContent>
      </Card>
    </div>
  );
}

export function SnapshotList({
  items,
}: {
  items: {
    id: string;
    name: string;
    owner_username?: string | null;
    run_count: number;
    created_at: string;
    pat_id?: string | null;
  }[];
}) {
  return (
    <ul className="grid gap-1 text-sm">
      {items.map((s) => (
        <li key={s.id} className="flex min-w-0 flex-wrap items-center gap-2">
          <Link
            className="truncate text-primary hover:underline"
            to={`/snapshots/${s.id}`}
          >
            {s.name}
          </Link>
          <span className="text-xs text-muted-foreground">
            {t("comparison.collections.snapshotMeta", {
              count: s.run_count,
              owner: s.owner_username,
            })}
            {s.pat_id ? t("comparison.shared.pat") : ""} · {fmtTime(s.created_at)}
          </span>
        </li>
      ))}
    </ul>
  );
}

function NewCollection({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate();
  const runs = useLoad(
    async () => (await api.get("/runs", { query: { limit: 100 } })).data,
  );
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [scenario, setScenario] = useState<Scenario>("models");
  const [variable, setVariable] = useState<VariableDimension[]>(["model"]);
  const [baseRun, setBaseRun] = useState("");
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const create = async () => {
    setBusy(true);
    setError(undefined);
    try {
      const res = await api.post("/collections", {
        body: {
          name: name.trim(),
          description,
          scenario,
          variable_dimensions: scenario === "custom" ? variable : null,
          base: { run_id: baseRun },
          run_ids: [baseRun],
        },
      });
      navigate(`/collections/${res.data.id}`);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          {t("comparison.collections.new")}
        </CardTitle>
      </CardHeader>
      <CardContent className="grid gap-3">
        <TextField
          id="new-collection-name"
          label={t("comparison.shared.name")}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <TextAreaField
          id="new-collection-description"
          label={t("comparison.shared.purpose")}
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
        <ScenarioPicker
          scenario={scenario}
          variable={variable}
          onChange={(s, v) => {
            setScenario(s);
            setVariable(v);
          }}
        />
        <label className="grid gap-1 text-sm">
          {t("comparison.collections.baseRun")}
          <select
            className="h-9 rounded-md border bg-background px-2"
            value={baseRun}
            onChange={(e) => setBaseRun(e.target.value)}
          >
            <option value="">{t("comparison.collections.pickRun")}</option>
            {(runs.data ?? []).map((r) => (
              <option key={r.id} value={r.id}>
                {t("comparison.collections.runOption", {
                  name: r.name,
                  owner: r.owner.username,
                  time: fmtTime(r.accepted_at),
                })}
              </option>
            ))}
          </select>
          <span className="text-xs text-muted-foreground">
            {t("comparison.collections.fromNewRun")}
          </span>
        </label>
        <ErrorAlert error={error} />
        <div className="flex gap-2">
          <Button
            disabled={busy || !name.trim() || !baseRun}
            onClick={() => void create()}
          >
            {t("comparison.shared.create")}
          </Button>
          <Button variant="ghost" onClick={onClose}>
            {t("comparison.shared.cancel")}
          </Button>
        </div>
      </CardContent>
    </Card>
  );
}
