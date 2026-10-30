// A frozen comparison (06 section 8): its numbers never change; whether each run's files
// still open is checked now. Notes are their authors', apart from the computed table.
import { useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ComparisonView } from "@/components/ComparisonView";
import {
  BackLink,
  ErrorAlert,
  Loading,
  PageTitle,
  TextAreaField,
} from "@/components/common";
import { Trans, t } from "@/i18n";
import { api, errorText } from "@/lib/api";
import { DIMENSION, SCENARIO } from "@/lib/comparison";
import { fmtTime } from "@/lib/format";
import { useLoad } from "@/lib/hooks";
import type { Dimension, Snapshot, SnapshotNote } from "@/lib/types";

export function SnapshotPage() {
  const { snapshotId = "" } = useParams();
  const navigate = useNavigate();
  const path = { snapshot_id: snapshotId };
  const snapshot = useLoad(
    async () =>
      (await api.get("/analysis-snapshots/{snapshot_id}", { path })).data,
    [snapshotId],
  );
  const s = snapshot.data;
  if (!s)
    return snapshot.error ? (
      <div>
        <BackLink to="/collections">{t("common.nav.back.collections")}</BackLink>
        <ErrorAlert error={snapshot.error} />
      </div>
    ) : (
      <Loading />
    );

  const remove = async () => {
    if (!window.confirm(t("comparison.snapshot.deleteConfirm"))) return;
    try {
      await api.del("/analysis-snapshots/{snapshot_id}", { path });
      toast.success(t("comparison.shared.movedToBin"));
      navigate(
        s.collection_id ? `/collections/${s.collection_id}` : "/collections",
      );
    } catch (err) {
      toast.error(
        t("comparison.shared.deleteFailed", { error: errorText(err) }),
      );
    }
  };

  return (
    <div className="grid gap-4">
      <PageTitle
        title={t("comparison.snapshot.title", { name: s.name })}
        back={
          s.collection_id
            ? { to: `/collections/${s.collection_id}`, label: t("common.nav.back.collection") }
            : { to: "/collections", label: t("common.nav.back.collections") }
        }
      >
        {s.allowed_actions.includes("delete") && !s.deletion && (
          <Button size="sm" variant="outline" onClick={() => void remove()}>
            <Trash2 />
            {t("comparison.shared.delete")}
          </Button>
        )}
      </PageTitle>
      <Card>
        <CardContent className="grid gap-1 pt-6 text-sm">
          <p>
            {t("comparison.snapshot.meta", {
              owner: s.owner_username,
              pat: s.pat_id ? t("comparison.shared.pat") : "",
              time: fmtTime(s.created_at),
              count: s.run_count,
            })}
            {s.collection_id && (
              <Trans
                i18nKey="comparison.snapshot.from"
                components={{
                  link: (
                    <Link
                      className="ml-1 underline"
                      to={`/collections/${s.collection_id}`}
                    />
                  ),
                }}
              />
            )}
          </p>
          {s.spec && (
            <p className="text-muted-foreground">
              {t("comparison.snapshot.spec", {
                scenario: SCENARIO[s.spec.scenario].label,
                dimensions:
                  s.spec.variable_dimensions
                    .map((d) => DIMENSION[d as Dimension])
                    .join(t("comparison.shared.listJoin")) ||
                  t("comparison.value.none"),
              })}
            </p>
          )}
          <p className="text-xs text-muted-foreground">
            {t("comparison.snapshot.frozenHint")}
          </p>
        </CardContent>
      </Card>
      <Notes snapshot={s} onChange={() => void snapshot.reload()} />
      <ComparisonView
        table={s.result}
        evidence={s.evidence}
        axes={s.spec?.variable_dimensions as Dimension[] | undefined}
        csvName={`${s.name}.csv`}
      />
    </div>
  );
}

function Notes({
  snapshot,
  onChange,
}: {
  snapshot: Snapshot;
  onChange: () => void;
}) {
  const [text, setText] = useState("");
  const [editing, setEditing] = useState<SnapshotNote | null>(null);
  const path = { snapshot_id: snapshot.id };
  const save = async () => {
    try {
      if (editing) {
        await api.put("/analysis-snapshots/{snapshot_id}/notes/{note_id}", {
          path: { ...path, note_id: editing.id },
          body: { body: text },
        });
      } else {
        await api.post("/analysis-snapshots/{snapshot_id}/notes", {
          path,
          body: { body: text },
        });
      }
      setText("");
      setEditing(null);
      onChange();
    } catch (err) {
      toast.error(t("comparison.shared.saveFailed", { error: errorText(err) }));
    }
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">
          {t("comparison.snapshot.notes.title")}
        </CardTitle>
      </CardHeader>
      <CardContent className="grid gap-3">
        <p className="text-xs text-muted-foreground">
          {t("comparison.snapshot.notes.hint")}
        </p>
        {snapshot.notes.length === 0 && (
          <p className="text-sm text-muted-foreground">
            {t("comparison.snapshot.notes.none")}
          </p>
        )}
        {snapshot.notes.map((n) => (
          <div key={n.id} className="rounded-md border p-3 text-sm">
            <div className="mb-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
              {n.author_username}
              {n.pat_id ? t("comparison.shared.pat") : ""} · {fmtTime(n.updated_at)}
              {n.editable && (
                <button
                  type="button"
                  className="underline"
                  onClick={() => {
                    setEditing(n);
                    setText(n.body);
                  }}
                >
                  {t("comparison.snapshot.notes.edit")}
                </button>
              )}
            </div>
            <p className="whitespace-pre-wrap break-words">{n.body}</p>
          </div>
        ))}
        {snapshot.allowed_actions.includes("note") && !snapshot.deletion && (
          <div className="grid gap-2">
            <TextAreaField
              id="snapshot-note-body"
              label={
                editing
                  ? t("comparison.snapshot.notes.editLabel")
                  : t("comparison.snapshot.notes.writeLabel")
              }
              value={text}
              onChange={(e) => setText(e.target.value)}
            />
            <div className="flex gap-2">
              <Button
                size="sm"
                disabled={!text.trim()}
                onClick={() => void save()}
              >
                {t("comparison.shared.save")}
              </Button>
              {editing && (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    setEditing(null);
                    setText("");
                  }}
                >
                  {t("comparison.shared.cancel")}
                </Button>
              )}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
