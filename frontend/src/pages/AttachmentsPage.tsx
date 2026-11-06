// Attachment snapshots: members publish a folder, uploading only
// the files the service lacks; each snapshot is fixed and named by its content. Each
// trial of a model run installs the chosen ones in its own container before the agent.
import { useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { toast } from "sonner";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { AttachmentHistory, type HistoryActions } from "@/components/AttachmentHistory";
import { FolderUpload, type Chosen, type FolderBase } from "@/components/FolderUpload";
import {
  EmptyNote,
  ErrorAlert,
  Loading,
  PageTitle,
  SelectField,
  TextField,
} from "@/components/common";
import { t, Trans } from "@/i18n";
import { api } from "@/lib/api";
import { entries, type LocalFile } from "@/lib/folder";
import { fmtTime, snapshotName } from "@/lib/format";
import { limit, useLoad, useSession } from "@/lib/hooks";
import type { Attachment } from "@/lib/types";

const EXAMPLE = `{
  "schema_version": 1,
  "name": "my-tools",
  "description": "${t("attachments.publish.exampleDescription")}",
  "effects": ["agent_tools"],
  "compatible_harnesses": ["terminus-2"],
  "provides": { "tools": ["mytool"], "env": [] }
}`;
/** The attachment name in the folder's manifest.json, if it has one. */
async function manifestName(files: LocalFile[]): Promise<string | null> {
  const manifest = files.find((f) => f.path === "manifest.json");
  if (!manifest) return null;
  try {
    const name: unknown = JSON.parse(await manifest.file.text())?.name;
    return typeof name === "string" ? name : null;
  } catch {
    return null;
  }
}

export function AttachmentsPage() {
  const { me } = useSession();
  const list = useLoad(async () => (await api.get("/attachments")).data);
  const [params] = useSearchParams();
  const { capabilities } = useSession();
  const [error, setError] = useState<unknown>();
  const [name, setName] = useState<string | null>(null);
  const [label, setLabel] = useState("");
  const [parentId, setParentId] = useState("");
  const [base, setBase] = useState<FolderBase>();
  const publishCard = useRef<HTMLDivElement>(null);

  const all = useMemo(() => list.data ?? [], [list.data]);
  const byId = new Map(all.map((a) => [a.id, a]));
  const sameName = (n: string | null) => all.filter((a) => a.name === n);

  /** The parent snapshot's files decide carried-over executable bits and the preview. */
  const chooseParent = async (id: string) => {
    setParentId(id);
    const parent = byId.get(id);
    if (!parent) {
      setBase(undefined);
      return;
    }
    const detail = (await api.get("/attachments/{attachment_id}", { path: { attachment_id: id } }))
      .data;
    setBase({ label: `${parent.name} · ${snapshotName(parent)}`, files: detail.files ?? [] });
  };

  const chooseFolder = async (_folder: string, files: LocalFile[]) => {
    const found = await manifestName(files);
    setName(found);
    // The newest snapshot of the same name is the default parent; one chosen with
    // "base a new snapshot on this" stays when the names match.
    const chosenParent = byId.get(parentId);
    const parent =
      chosenParent && chosenParent.name === found ? chosenParent : sameName(found).at(-1);
    await chooseParent(parent?.id ?? "");
  };

  const publish = async ({ files, uploaded }: Chosen) => {
    const res = await api.post("/attachments", {
      body: {
        files: entries(files),
        label: label.trim() || null,
        parent_id: parentId || null,
      },
    });
    toast.success(
      t("attachments.publish.done", {
        name: res.data.name,
        snapshot: snapshotName(res.data),
        count: uploaded,
      }),
    );
    setLabel("");
    setName(null);
    setParentId("");
    setBase(undefined);
    await list.reload();
  };

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
  const baseOn = (a: Attachment) => {
    void chooseParent(a.id).catch(setError);
    publishCard.current?.scrollIntoView({ behavior: "smooth" });
  };
  const actions: HistoryActions = {
    mayManage: (a) => a.owner_id === me.id || me.role === "admin",
    onBaseOn: baseOn,
    onToggleEnabled: (a) =>
      void act(
        () =>
          api.post("/attachments/{attachment_id}/enabled", {
            path: { attachment_id: a.id },
            body: { enabled: !a.enabled },
          }),
        a.enabled ? t("attachments.list.disabledDone") : t("attachments.list.enabledDone"),
      ),
    onDelete: (a) =>
      void act(
        () => api.del("/attachments/{attachment_id}", { path: { attachment_id: a.id } }),
        t("attachments.list.deleted"),
      ),
    onError: setError,
  };

  return (
    <div className="grid gap-4">
      <PageTitle title={t("attachments.title")} />
      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t("attachments.list.title")}</CardTitle>
          <CardDescription>
            <Trans
              i18nKey="attachments.list.description"
              components={{ dir: <code className="mx-1" />, code: <code /> }}
            />
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 text-sm">
          {list.loading && !list.data && <Loading />}
          <ErrorAlert error={list.error} />
          {list.data && list.data.length === 0 && <EmptyNote>{t("attachments.list.empty")}</EmptyNote>}
          {all.length > 0 && (
            <AttachmentHistory
              all={all}
              actions={actions}
              focus={{
                snapshot: params.get("snapshot") ?? "",
                base: params.get("base") ?? undefined,
                name: params.get("name") ?? undefined,
              }}
            />
          )}
        </CardContent>
      </Card>

      <Card ref={publishCard}>
        <CardHeader>
          <CardTitle className="text-base">{t("attachments.publish.title")}</CardTitle>
          <CardDescription>
            {t("attachments.publish.description")}
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 text-sm">
          <ErrorAlert error={error} />
          <FolderUpload
            base={base}
            maxFiles={limit(capabilities, "attachment_max_files", 10000)}
            maxBytes={limit(capabilities, "attachment_max_bytes", 1024 ** 3)}
            submitLabel={t("attachments.publish.submit")}
            check={(files) =>
              files.some((f) => f.path === "manifest.json")
                ? []
                : [t("attachments.publish.noManifest")]
            }
            onFolder={chooseFolder}
            onSubmit={publish}
          >
            {() => (
              <div className="grid gap-3">
                <p className="text-xs">
                  {name
                    ? t("attachments.publish.pickedName", { name })
                    : t("attachments.publish.noName")}
                </p>
                <div className="grid gap-3 md:grid-cols-2">
                  <TextField
                    id="attachment-label"
                    label={t("attachments.publish.label")}
                    value={label}
                    placeholder={t("attachments.publish.labelPlaceholder")}
                    onChange={(e) => setLabel(e.target.value)}
                  />
                  <SelectField
                    id="attachment-parent"
                    label={t("attachments.publish.parent")}
                    value={parentId}
                    onChange={(e) => void chooseParent(e.target.value).catch(setError)}
                  >
                    <option value="">{t("attachments.publish.noParent")}</option>
                    {sameName(name).map((s) => (
                      <option key={s.id} value={s.id}>
                        {snapshotName(s)} · {fmtTime(s.created_at)}
                      </option>
                    ))}
                  </SelectField>
                </div>
              </div>
            )}
          </FolderUpload>
          <details>
            <summary className="cursor-pointer text-xs text-muted-foreground">
              {t("attachments.publish.exampleSummary")}
            </summary>
            <pre className="mt-2 overflow-auto rounded-md bg-muted p-3 text-xs">{EXAMPLE}</pre>
            <p className="mt-1 text-xs text-muted-foreground">
              {t("attachments.publish.exampleNote")}
            </p>
          </details>
        </CardContent>
      </Card>
    </div>
  );
}
