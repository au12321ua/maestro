// Attachment snapshots (03 sections 6-7, N-06): members publish a folder, uploading only
// the files the service lacks; each snapshot is fixed and named by its content. Each
// trial of a model run installs the chosen ones in its own container before the agent.
import { useMemo, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { AttachmentHistory, type HistoryActions } from "@/components/AttachmentHistory";
import {
  EmptyNote,
  ErrorAlert,
  Loading,
  PageTitle,
  SelectField,
  TextField,
} from "@/components/common";
import { t, Trans } from "@/i18n";
import { ApiError, api } from "@/lib/api";
import { fmtBytes, fmtTime, snapshotName } from "@/lib/format";
import { useLoad, useSession } from "@/lib/hooks";
import { sha256 } from "@/lib/sha256";
import type { Attachment } from "@/lib/types";

const EXAMPLE = `{
  "schema_version": 1,
  "name": "my-tools",
  "description": "${t("attachments.publish.exampleDescription")}",
  "effects": ["agent_tools"],
  "compatible_harnesses": ["terminus-2"],
  "provides": { "tools": ["mytool"], "env": [] }
}`;
interface Picked {
  path: string;
  file: File;
  sha256: string;
  executable: boolean;
}

/** The folder's files below its top directory, leaving out `.git`. */
async function pick(files: File[], parent: Attachment | undefined): Promise<Picked[]> {
  const before = new Map((parent?.files ?? []).map((f) => [f.path, f]));
  const picked: Picked[] = [];
  for (const file of files) {
    const parts = (file.webkitRelativePath || file.name).split("/");
    const path = parts.length > 1 ? parts.slice(1).join("/") : parts[0];
    if (path.split("/").includes(".git")) continue;
    // Browsers do not tell a file's mode: keep the parent's, or guess from a shebang.
    const head = new Uint8Array(await file.slice(0, 2).arrayBuffer());
    const executable = before.get(path)?.executable ?? (head[0] === 0x23 && head[1] === 0x21);
    picked.push({ path, file, sha256: await sha256(file), executable });
  }
  return picked.sort((a, b) => (a.path < b.path ? -1 : 1));
}

async function manifestName(files: File[]): Promise<string | null> {
  const manifest = files.find((f) => /^[^/]+\/manifest\.json$/.test(f.webkitRelativePath));
  if (!manifest) return null;
  try {
    const name: unknown = JSON.parse(await manifest.text())?.name;
    return typeof name === "string" ? name : null;
  } catch {
    return null;
  }
}

export function AttachmentsPage() {
  const { me } = useSession();
  const list = useLoad(async () => (await api.get("/attachments")).data);
  const [params] = useSearchParams();
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState<string | null>(null);
  const [picked, setPicked] = useState<Picked[]>([]);
  const [name, setName] = useState<string | null>(null);
  const [label, setLabel] = useState("");
  const [parentId, setParentId] = useState("");
  const input = useRef<HTMLInputElement>(null);
  const publishCard = useRef<HTMLDivElement>(null);

  const all = useMemo(() => list.data ?? [], [list.data]);
  const byId = new Map(all.map((a) => [a.id, a]));
  const sameName = (n: string | null) => all.filter((a) => a.name === n);

  const choose = async (chosen: FileList | null) => {
    setError(undefined);
    if (!chosen || chosen.length === 0) return;
    const files = Array.from(chosen);
    setBusy(t("attachments.progress.reading"));
    try {
      const found = await manifestName(files);
      // The newest snapshot of the same name is the default parent; one chosen with
      // "base a new snapshot on this" stays when the names match.
      const chosenParent = byId.get(parentId);
      const parent =
        chosenParent && chosenParent.name === found ? chosenParent : sameName(found).at(-1);
      const withFiles = parent
        ? (await api.get("/attachments/{attachment_id}", { path: { attachment_id: parent.id } }))
            .data
        : undefined;
      setPicked(await pick(files, withFiles));
      setName(found);
      setParentId(parent?.id ?? "");
    } catch (err) {
      setError(err);
    } finally {
      setBusy(null);
    }
  };

  const publish = async () => {
    setError(undefined);
    try {
      setBusy(t("attachments.progress.checking"));
      const shas = [...new Set(picked.map((p) => p.sha256))];
      const { missing } = (
        await api.post("/attachment-contents/missing", { body: { sha256: shas } })
      ).data;
      const contents = new Map(picked.map((p) => [p.sha256, p.file]));
      for (const [index, sha] of missing.entries()) {
        setBusy(t("attachments.progress.uploading", { index: index + 1, total: missing.length }));
        await api.upload("/attachment-contents/{sha256}", { sha256: sha }, contents.get(sha)!);
      }
      setBusy(t("attachments.progress.publishing"));
      const res = await api.post("/attachments", {
        body: {
          files: picked.map(({ path, sha256, executable }) => ({ path, sha256, executable })),
          label: label.trim() || null,
          parent_id: parentId || null,
        },
      });
      toast.success(
        t("attachments.publish.done", {
          name: res.data.name,
          snapshot: snapshotName(res.data),
          count: missing.length,
        }),
      );
      setPicked([]);
      setLabel("");
      setName(null);
      setParentId("");
      if (input.current) input.current.value = "";
      await list.reload();
    } catch (err) {
      setError(err);
    } finally {
      setBusy(null);
    }
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
    setParentId(a.id);
    setPicked([]);
    setName(null);
    if (input.current) input.current.value = "";
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
  const fieldErrors = error instanceof ApiError ? error.body.field_errors : [];
  const parent = byId.get(parentId);

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
          {parent && !picked.length && (
            <p className="text-xs">
              {t("attachments.publish.basedOn", { name: parent.name, snapshot: snapshotName(parent) })}
            </p>
          )}
          <input
            ref={input}
            type="file"
            multiple
            className="text-sm"
            onChange={(e) => void choose(e.target.files)}
            {...({ webkitdirectory: "" } as Record<string, string>)}
          />
          {picked.length > 0 && (
            <div className="grid gap-3">
              <p className="text-xs">
                {t("attachments.publish.picked", {
                  name: name
                    ? t("attachments.publish.pickedName", { name })
                    : t("attachments.publish.noName"),
                  count: picked.length,
                  size: fmtBytes(picked.reduce((n, p) => n + p.file.size, 0)),
                })}
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
                  onChange={(e) => setParentId(e.target.value)}
                >
                  <option value="">{t("attachments.publish.noParent")}</option>
                  {sameName(name).map((s) => (
                    <option key={s.id} value={s.id}>
                      {snapshotName(s)} · {fmtTime(s.created_at)}
                    </option>
                  ))}
                </SelectField>
              </div>
              <div className="max-h-72 overflow-auto rounded-md border p-2">
                <p className="mb-1 text-xs text-muted-foreground">
                  {t("attachments.publish.modeHint")}
                </p>
                <ul className="grid gap-0.5 font-mono text-xs">
                  {picked.map((p, i) => (
                    <li key={p.path}>
                      <label className="flex items-center gap-2">
                        <input
                          type="checkbox"
                          checked={p.executable}
                          onChange={(e) =>
                            setPicked(
                              picked.map((q, j) =>
                                j === i ? { ...q, executable: e.target.checked } : q,
                              ),
                            )
                          }
                        />
                        {p.path}
                        <span className="text-muted-foreground">({fmtBytes(p.file.size)})</span>
                      </label>
                    </li>
                  ))}
                </ul>
              </div>
              <div>
                <Button disabled={busy !== null} onClick={() => void publish()}>
                  {busy ?? t("attachments.publish.submit")}
                </Button>
              </div>
            </div>
          )}
          {busy && !picked.length && <Loading text={busy} />}
          <ErrorAlert error={error} />
          {fieldErrors.length > 0 && (
            <ul className="grid gap-0.5 font-mono text-xs text-destructive">
              {fieldErrors.map((e, i) => (
                <li key={i}>
                  {e.path} · {e.code}
                  {e.message ? ` · ${e.message}` : ""}
                </li>
              ))}
            </ul>
          )}
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
