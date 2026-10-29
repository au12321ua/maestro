// Experiment plans (04 section 1): saved configurations the team shares. The creator edits;
// others copy to change; the creator or an administrator archives or deletes.
import { useState } from "react";
import { Link } from "react-router-dom";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { EmptyNote, ErrorAlert, Loading, PageTitle, Pill, TextField } from "@/components/common";
import { ApiError, api } from "@/lib/api";
import { fmtTime } from "@/lib/format";
import { useLoad } from "@/lib/hooks";
import type { Plan, PlanTemplate } from "@/lib/types";
import { t } from "@/i18n";

const FIELD_NAME: Record<string, string> = t("plans.fieldName", { returnObjects: true });

function Editor({ plan, onDone }: { plan: Plan; onDone: () => void }) {
  const [name, setName] = useState(plan.name);
  const [description, setDescription] = useState(plan.description);
  const [template, setTemplate] = useState(JSON.stringify(plan.template, null, 2));
  const [error, setError] = useState<unknown>();
  const save = async () => {
    setError(undefined);
    try {
      let parsed: unknown;
      try {
        parsed = JSON.parse(template);
      } catch {
        throw new Error(t("plans.editor.invalidJson"));
      }
      await api.put("/plans/{plan_id}", {
        path: { plan_id: plan.id },
        body: {
          expected_revision: plan.revision,
          name,
          description,
          template: parsed as PlanTemplate,
        },
      });
      toast.success(t("plans.editor.saved"));
      onDone();
    } catch (err) {
      setError(err);
    }
  };
  const fieldErrors = error instanceof ApiError ? error.body.field_errors : [];
  return (
    <div className="grid gap-2">
      <TextField id={`plan-${plan.id}-name`} label={t("plans.name")} value={name} onChange={(e) => setName(e.target.value)} />
      <TextField
        id={`plan-${plan.id}-description`}
        label={t("plans.editor.description")}
        value={description}
        onChange={(e) => setDescription(e.target.value)}
      />
      <label className="grid gap-1 text-sm">
        {t("plans.editor.template")}
        <textarea
          className="min-h-48 rounded-md border bg-background p-2 font-mono text-xs"
          value={template}
          onChange={(e) => setTemplate(e.target.value)}
        />
      </label>
      <ErrorAlert error={error} title={t("plans.editor.saveFailed")} />
      {fieldErrors.length > 0 && (
        <ul className="font-mono text-xs text-destructive">
          {fieldErrors.map((e, i) => (
            <li key={i}>
              {e.path} · {e.code}
              {e.message ? ` · ${e.message}` : ""}
            </li>
          ))}
        </ul>
      )}
      <div className="flex gap-2">
        <Button size="sm" onClick={() => void save()}>
          {t("plans.editor.save")}
        </Button>
        <Button size="sm" variant="outline" onClick={onDone}>
          {t("plans.editor.cancel")}
        </Button>
      </div>
    </div>
  );
}

export function PlansPage() {
  const [archived, setArchived] = useState(false);
  const list = useLoad(
    async () => (await api.get("/plans", { query: { include_archived: archived } })).data,
    [archived],
  );
  const [editing, setEditing] = useState<string | null>(null);
  const [error, setError] = useState<unknown>();
  const act = async (fn: () => Promise<unknown>, done: string) => {
    setError(undefined);
    try {
      await fn();
      toast.success(done);
      await list.reload();
    } catch (err) {
      setError(err);
    }
  };

  return (
    <div className="grid gap-4">
      <PageTitle title={t("plans.title")}>
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={archived}
            onChange={(e) => setArchived(e.target.checked)}
          />
          {t("plans.showArchived")}
        </label>
      </PageTitle>
      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t("plans.teamPlans")}</CardTitle>
          <CardDescription>
            {t("plans.description")}
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 text-sm">
          {list.loading && !list.data && <Loading />}
          <ErrorAlert error={list.error ?? error} />
          {list.data && list.data.length === 0 && (
            <EmptyNote>{t("plans.empty")}</EmptyNote>
          )}
          {list.data && list.data.length > 0 && (
            <div className="overflow-auto rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t("plans.name")}</TableHead>
                    <TableHead>{t("plans.column.owner")}</TableHead>
                    <TableHead>{t("plans.column.fields")}</TableHead>
                    <TableHead>{t("plans.column.revision")}</TableHead>
                    <TableHead />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {list.data.map((plan) => (
                    <TableRow key={plan.id}>
                      <TableCell>
                        <span className="font-medium">{plan.name}</span>
                        {plan.archived_at && (
                          <span className="ml-2">
                            <Pill tone="muted">{t("plans.archived")}</Pill>
                          </span>
                        )}
                        {plan.description && (
                          <div className="text-xs text-muted-foreground">{plan.description}</div>
                        )}
                        {editing === plan.id && (
                          <div className="mt-2">
                            <Editor
                              plan={plan}
                              onDone={() => {
                                setEditing(null);
                                void list.reload();
                              }}
                            />
                          </div>
                        )}
                      </TableCell>
                      <TableCell className="text-xs">{plan.owner_username}</TableCell>
                      <TableCell className="text-xs">
                        {Object.keys(plan.template)
                          .map((k) => FIELD_NAME[k] ?? k)
                          .join(t("plans.fieldSeparator")) || "—"}
                      </TableCell>
                      <TableCell className="text-xs">
                        {plan.revision}
                        <div className="text-muted-foreground">{fmtTime(plan.updated_at)}</div>
                      </TableCell>
                      <TableCell className="whitespace-nowrap text-right">
                        {plan.allowed_actions.includes("use") && (
                          <Button asChild size="sm" variant="outline">
                            <Link to={`/runs/new?plan=${plan.id}`}>{t("plans.action.use")}</Link>
                          </Button>
                        )}{" "}
                        {plan.allowed_actions.includes("edit") && (
                          <Button size="sm" variant="outline" onClick={() => setEditing(plan.id)}>
                            {t("plans.action.edit")}
                          </Button>
                        )}{" "}
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() =>
                            void act(
                              () =>
                                api.post("/plans/{plan_id}/copy", {
                                  path: { plan_id: plan.id },
                                  body: {},
                                }),
                              t("plans.action.copied"),
                            )
                          }
                        >
                          {t("plans.action.copy")}
                        </Button>{" "}
                        {plan.allowed_actions.includes("archive") && (
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() =>
                              void act(
                                () =>
                                  api.post("/plans/{plan_id}/archive", {
                                    path: { plan_id: plan.id },
                                    body: {
                                      archived: plan.archived_at == null,
                                    },
                                  }),
                                plan.archived_at == null ? t("plans.archived") : t("plans.action.unarchived"),
                              )
                            }
                          >
                            {plan.archived_at == null ? t("plans.action.archive") : t("plans.action.unarchive")}
                          </Button>
                        )}{" "}
                        {plan.allowed_actions.includes("delete") && (
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() =>
                              void act(
                                () => api.del("/plans/{plan_id}", { path: { plan_id: plan.id } }),
                                t("plans.action.deleted"),
                              )
                            }
                          >
                            {t("plans.action.delete")}
                          </Button>
                        )}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
