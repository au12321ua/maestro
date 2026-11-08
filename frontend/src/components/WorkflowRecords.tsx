// Workflow records on a run, collection, attachment name or dataset version: read like a
// comment thread, written by any member. Only the author edits (with the revision they saw,
// so a concurrent edit is refused instead of overwritten); deleting moves a record to the
// recycle bin. A record has Markdown text written for people and a JSON body for programs;
// the body is shown as a readable outline with the raw JSON behind a toggle, and is only
// edited under "advanced". References and evidence excerpts are checked against the
// objects they cite and are written through the SDK; here they are shown as links.
import { useId, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { History, MessageSquareText, Pencil, Plus, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { EmptyNote, ErrorAlert, Loading } from "@/components/common";
import { Markdown, ReadableJson } from "@/components/Markdown";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { t } from "@/i18n";
import { api, ApiError, errorText } from "@/lib/api";
import { fmtTime } from "@/lib/format";
import { useLoad, useSession, type Loaded } from "@/lib/hooks";
import { RECORDS_ANCHOR, recordsLabel } from "@/lib/records";
import type { RecordRevision, RecordTargetType, WorkflowRecord } from "@/lib/types";

const KIND = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/;

type Reference = WorkflowRecord["references"][number];
type Location = WorkflowRecord["reference_locations"][number];

/** Where a reference opens. Trials and artifacts need their run, which the record
 * supplies; without it (the object is gone) the reference is shown as plain text. */
function referenceHref(ref: Reference, locations: Location[]): string | null {
  const id = encodeURIComponent(ref.id);
  const at = locations.find((l) => l.type === ref.type && l.id === ref.id);
  switch (ref.type) {
    case "run":
      return `/runs/${id}`;
    case "collection":
      return `/collections/${id}`;
    case "analysis_snapshot":
      return `/snapshots/${id}`;
    case "attachment_snapshot":
      return `/attachments?snapshot=${id}`;
    case "trial":
      return at ? `/runs/${encodeURIComponent(at.run_id)}?trial=${id}` : null;
    case "artifact": {
      if (!at) return null;
      const query = new URLSearchParams({ tab: "artifacts" });
      if (at.trial_id) query.set("artifacts_of", at.trial_id);
      return `/runs/${encodeURIComponent(at.run_id)}?${query}`;
    }
    default:
      return null;
  }
}

function ReferenceLink({ value, locations }: { value: Reference; locations: Location[] }) {
  const href = referenceHref(value, locations);
  const label = (
    <>
      {t(`common.records.referenceType.${value.type}`, { defaultValue: value.type })}
      {" · "}
      <code>{value.id}</code>
    </>
  );
  return href ? (
    <Link to={href} className="break-all text-primary underline-offset-2 hover:underline">
      {label}
    </Link>
  ) : (
    <span className="break-all">{label}</span>
  );
}

/** "Records (n)" at the top of a page whose records section is further down. */
export function RecordsJumpButton({ records }: { records: Loaded<WorkflowRecord[]> }) {
  return (
    <Button
      size="sm"
      variant="outline"
      onClick={() =>
        document
          .getElementById(RECORDS_ANCHOR)
          ?.scrollIntoView({ behavior: "smooth", block: "start" })
      }
    >
      <MessageSquareText />
      {recordsLabel(records)}
    </Button>
  );
}

interface Draft {
  kind: string;
  title: string;
  text: string;
  /** JSON text; empty means an empty body. */
  body: string;
}

type Content = { kind: string; title: string; text: string; body: Record<string, unknown> };

function emptyDraft(targetType: RecordTargetType): Draft {
  return targetType === "dataset_version"
    ? { kind: "provenance", title: t("common.records.provenanceTitle"), text: "", body: "" }
    : { kind: "note", title: "", text: "", body: "" };
}

function bodyText(body: Record<string, unknown>) {
  return Object.keys(body).length === 0 ? "" : JSON.stringify(body, null, 2);
}

export function WorkflowRecords({
  targetType,
  targetId,
  records,
  readOnly,
  anchor = true,
}: {
  targetType: RecordTargetType;
  targetId: string;
  /** From useRecords, so the page can show the count in its own tab or button. */
  records: Loaded<WorkflowRecord[]>;
  /** The target is in the recycle bin: its records stay readable but nothing is added. */
  readOnly?: boolean;
  /** Whether this section is the page's scroll target (one per page). */
  anchor?: boolean;
}) {
  const { me } = useSession();
  const [adding, setAdding] = useState(false);
  const canWrite =
    !readOnly && (me.pat_permissions == null || me.pat_permissions.includes("experiment"));
  const kinds = [
    ...new Set([
      emptyDraft(targetType).kind,
      "note",
      ...(records.data ?? []).map((r) => r.kind),
    ]),
  ];

  const create = async (content: Content) => {
    await api.post("/records", {
      body: { target: { type: targetType, id: targetId }, ...content },
    });
    setAdding(false);
    toast.success(t("common.records.added"));
    await records.reload();
  };

  const addButton = canWrite && !adding && (
    <Button size="sm" variant="outline" onClick={() => setAdding(true)}>
      <Plus />
      {t("common.records.add")}
    </Button>
  );

  return (
    <Card id={anchor ? RECORDS_ANCHOR : undefined} className="scroll-mt-4">
      <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-2">
        <CardTitle className="text-base">
          {records.data
            ? t("common.records.titleCount", { n: records.data.length })
            : t("common.records.title")}
        </CardTitle>
        {records.data && records.data.length > 0 && addButton}
      </CardHeader>
      <CardContent className="grid gap-4 text-sm">
        <ErrorAlert error={records.error} />
        {records.loading && !records.data && <Loading />}
        {readOnly && (
          <p className="text-xs text-muted-foreground">{t("common.records.readOnly")}</p>
        )}
        {adding && (
          <RecordForm
            initial={emptyDraft(targetType)}
            textHint={targetType === "dataset_version" ? t("common.records.provenanceHint") : undefined}
            kinds={kinds}
            submitLabel={t("common.records.save")}
            onSubmit={create}
            onCancel={() => setAdding(false)}
          />
        )}
        {records.data?.length === 0 && !adding && (
          <EmptyNote>
            <div className="grid justify-items-center gap-3">
              <span>{t("common.records.empty")}</span>
              {addButton}
            </div>
          </EmptyNote>
        )}
        {records.data?.map((record) => (
          <RecordItem
            key={record.id}
            record={record}
            kinds={kinds}
            onChanged={() => records.reload()}
          />
        ))}
      </CardContent>
    </Card>
  );
}

function RecordItem({
  record,
  kinds,
  onChanged,
}: {
  record: WorkflowRecord;
  kinds: string[];
  onChanged: () => Promise<void>;
}) {
  const [editing, setEditing] = useState(false);
  const canEdit = record.allowed_actions.includes("edit");
  const canDelete = record.allowed_actions.includes("delete");

  const save = async (content: Content) => {
    await api.put("/records/{record_id}", {
      path: { record_id: record.id },
      body: { expected_revision: record.revision, ...content },
    });
    setEditing(false);
    toast.success(t("common.records.saved"));
    await onChanged();
  };
  const remove = async () => {
    if (!window.confirm(t("common.records.confirmDelete", { title: record.title }))) return;
    try {
      await api.del("/records/{record_id}", { path: { record_id: record.id } });
      toast.success(t("common.records.movedToBin"));
      await onChanged();
    } catch (err) {
      toast.error(errorText(err));
    }
  };

  if (editing) {
    return (
      <article className="min-w-0 rounded-md border p-3" data-testid="workflow-record">
        <RecordForm
          initial={{
            kind: record.kind,
            title: record.title,
            text: record.text,
            body: bodyText(record.body),
          }}
          kinds={kinds}
          submitLabel={t("common.records.saveEdit")}
          onSubmit={save}
          onCancel={() => setEditing(false)}
          onReload={async () => {
            setEditing(false);
            await onChanged();
          }}
        />
      </article>
    );
  }

  return (
    <article className="min-w-0 rounded-md border p-3" data-testid="workflow-record">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div className="grid min-w-0 gap-0.5">
          <p className="break-words font-medium">{record.title}</p>
          <p className="break-all text-xs text-muted-foreground">
            {record.kind} · {record.author_username} · {fmtTime(record.created_at)}
            {" · "}
            {t("common.records.revisions", { count: record.revision })}
            {record.pat_id && ` · ${t("common.records.pat")}`}
          </p>
          {record.revision > 1 && (
            <p className="text-xs text-muted-foreground">
              {t("common.records.updated", { time: fmtTime(record.updated_at) })}
            </p>
          )}
          {record.target.snapshot_id && (
            <p className="break-all text-xs text-muted-foreground">
              {t("common.records.snapshot", { id: record.target.snapshot_id })}
            </p>
          )}
        </div>
        {(canEdit || canDelete) && (
          <div className="flex shrink-0 gap-1">
            {canEdit && (
              <Button size="sm" variant="ghost" onClick={() => setEditing(true)}>
                <Pencil />
                {t("common.records.edit")}
              </Button>
            )}
            {canDelete && (
              <Button size="sm" variant="ghost" onClick={() => void remove()}>
                <Trash2 />
                {t("common.records.delete")}
              </Button>
            )}
          </div>
        )}
      </div>
      {record.text && <Markdown className="mt-2">{record.text}</Markdown>}
      <RecordBody value={record.body} />
      {record.references.length > 0 && (
        <details className="mt-2">
          <summary className="cursor-pointer text-xs">
            {t("common.records.references", { count: record.references.length })}
          </summary>
          <ul className="mt-1 grid gap-0.5 text-xs">
            {record.references.map((ref) => (
              <li key={`${ref.type}:${ref.id}`}>
                <ReferenceLink value={ref} locations={record.reference_locations} />
              </li>
            ))}
          </ul>
        </details>
      )}
      {record.excerpts.length > 0 && (
        <div className="mt-3 grid gap-2">
          <p className="text-xs text-muted-foreground">{t("common.records.excerpts")}</p>
          {record.excerpts.map((excerpt, index) => (
            <blockquote key={index} className="border-l-2 pl-3">
              <pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words text-xs">
                {excerpt.text}
              </pre>
              {excerpt.reference && (
                <p className="text-xs text-muted-foreground">
                  <ReferenceLink value={excerpt.reference} locations={record.reference_locations} />
                </p>
              )}
            </blockquote>
          ))}
        </div>
      )}
      {record.revision > 1 && <RecordHistory recordId={record.id} revision={record.revision} />}
    </article>
  );
}

/** The body as a readable outline, with its JSON behind a toggle. An empty one is left out. */
function RecordBody({ value }: { value: Record<string, unknown> }) {
  if (Object.keys(value).length === 0) return null;
  return (
    <div className="mt-2 grid gap-1 rounded-md bg-muted/40 p-3" data-testid="record-body">
      <ReadableJson value={value} />
      <details className="mt-1">
        <summary className="cursor-pointer text-xs text-muted-foreground">
          {t("common.records.viewJson")}
        </summary>
        <pre className="mt-1 max-h-96 overflow-auto whitespace-pre-wrap break-words rounded bg-muted p-2 text-xs">
          {JSON.stringify(value, null, 2)}
        </pre>
      </details>
    </div>
  );
}

function RecordHistory({ recordId, revision }: { recordId: string; revision: number }) {
  const [open, setOpen] = useState(false);
  return (
    <details className="mt-2" onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
      <summary className="cursor-pointer text-xs">
        <History className="mr-1 inline size-3" />
        {t("common.records.history")}
      </summary>
      {open && <RevisionList key={revision} recordId={recordId} />}
    </details>
  );
}

function RevisionList({ recordId }: { recordId: string }) {
  const history = useLoad(
    async () =>
      (await api.get("/records/{record_id}/history", { path: { record_id: recordId } })).data,
    [recordId],
  );
  return (
    <div className="mt-2 grid gap-2">
      <ErrorAlert error={history.error} />
      {history.loading && !history.data && <Loading />}
      {[...(history.data ?? [])].reverse().map((rev: RecordRevision) => (
        <div
          key={rev.revision}
          className="rounded-md border border-dashed p-2"
          data-testid="record-revision"
        >
          <p className="break-all text-xs text-muted-foreground">
            {t("common.records.revisionLine", {
              revision: rev.revision,
              actor: rev.actor_username,
              time: fmtTime(rev.at),
            })}
            {rev.pat_id && ` · ${t("common.records.pat")}`}
          </p>
          <p className="break-words text-xs">
            <span className="text-muted-foreground">{rev.kind} · </span>
            {rev.title}
          </p>
          {rev.text && <Markdown className="mt-1 text-xs">{rev.text}</Markdown>}
          <RecordBody value={rev.body} />
        </div>
      ))}
    </div>
  );
}

function RecordForm({
  initial,
  textHint,
  kinds,
  submitLabel,
  onSubmit,
  onCancel,
  onReload,
}: {
  initial: Draft;
  /** What the text usually says for this kind of target. */
  textHint?: string;
  kinds: string[];
  submitLabel: string;
  onSubmit: (content: Content) => Promise<void>;
  onCancel: () => void;
  /** For an edit: reload the record after someone else changed it. */
  onReload?: () => Promise<void>;
}) {
  const id = useId();
  const [draft, setDraft] = useState(initial);
  const [problems, setProblems] = useState<Partial<Record<keyof Draft, string>>>({});
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  const set = (key: keyof Draft) => (value: string) => setDraft((d) => ({ ...d, [key]: value }));

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    const found: Partial<Record<keyof Draft, string>> = {};
    const kind = draft.kind.trim();
    if (!KIND.test(kind)) found.kind = t("common.records.kindInvalid");
    if (!draft.title.trim()) found.title = t("common.records.titleRequired");
    let body: unknown = {};
    if (draft.body.trim()) {
      try {
        body = JSON.parse(draft.body);
      } catch {
        found.body = t("common.records.bodyNotJson");
      }
    }
    if (!found.body && (body === null || typeof body !== "object" || Array.isArray(body))) {
      found.body = t("common.records.bodyNotObject");
    }
    setProblems(found);
    if (Object.keys(found).length > 0) return;
    setBusy(true);
    setError(undefined);
    try {
      await onSubmit({
        kind,
        title: draft.title.trim(),
        text: draft.text,
        body: body as Record<string, unknown>,
      });
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };
  const conflict = error instanceof ApiError && error.code === "RECORD_REVISION_CHANGED";

  return (
    <form
      className="grid gap-3 rounded-md border bg-muted/30 p-3"
      onSubmit={(e) => void submit(e)}
    >
      <div className="grid gap-3 sm:grid-cols-[12rem_minmax(0,1fr)]">
        <Field id={`${id}-kind`} label={t("common.records.kind")} error={problems.kind}>
          <Input
            id={`${id}-kind`}
            list={`${id}-kinds`}
            value={draft.kind}
            onChange={(e) => set("kind")(e.target.value)}
            aria-invalid={problems.kind ? true : undefined}
          />
          <datalist id={`${id}-kinds`}>
            {kinds.map((k) => (
              <option key={k} value={k} />
            ))}
          </datalist>
        </Field>
        <Field id={`${id}-title`} label={t("common.records.recordTitle")} error={problems.title}>
          <Input
            id={`${id}-title`}
            value={draft.title}
            onChange={(e) => set("title")(e.target.value)}
            aria-invalid={problems.title ? true : undefined}
          />
        </Field>
      </div>
      <Field
        id={`${id}-text`}
        label={t("common.records.textLabel")}
        hint={textHint ?? t("common.records.textHint")}
      >
        <textarea
          id={`${id}-text`}
          className="min-h-32 rounded-md border bg-background p-2 text-sm"
          value={draft.text}
          onChange={(e) => set("text")(e.target.value)}
        />
      </Field>
      <details open={!!initial.body || !!problems.body} className="grid gap-2">
        <summary className="cursor-pointer text-xs text-muted-foreground">
          {t("common.records.advanced")}
        </summary>
        <div className="mt-2">
          <Field
            id={`${id}-body`}
            label={t("common.records.bodyLabel")}
            hint={t("common.records.bodyHint")}
            error={problems.body}
          >
            <textarea
              id={`${id}-body`}
              className="min-h-24 rounded-md border bg-background p-2 font-mono text-xs"
              value={draft.body}
              spellCheck={false}
              placeholder="{}"
              onChange={(e) => set("body")(e.target.value)}
              aria-invalid={problems.body ? true : undefined}
            />
          </Field>
        </div>
      </details>
      {conflict ? (
        <div
          role="alert"
          className="grid gap-2 rounded-md border border-amber-300 bg-amber-50 p-2 text-xs text-amber-900"
        >
          <p>{t("common.records.conflict")}</p>
          {onReload && (
            <div>
              <Button type="button" size="sm" variant="outline" onClick={() => void onReload()}>
                {t("common.records.reload")}
              </Button>
            </div>
          )}
        </div>
      ) : (
        <ErrorAlert error={error} />
      )}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" size="sm" disabled={busy}>
          {submitLabel}
        </Button>
        <Button type="button" size="sm" variant="ghost" onClick={onCancel}>
          {t("common.records.cancel")}
        </Button>
      </div>
    </form>
  );
}

function Field({
  id,
  label,
  hint,
  error,
  children,
}: {
  id: string;
  label: string;
  hint?: string;
  error?: string;
  children: ReactNode;
}) {
  return (
    <div className="grid min-w-0 content-start gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      {children}
      {hint && !error && <p className="text-xs text-muted-foreground">{hint}</p>}
      {error && <p className="text-xs text-destructive">{error}</p>}
    </div>
  );
}
