// Attachment snapshots (03 sections 6-7, N-06): members publish a folder, uploading only
// the files the service lacks; each snapshot is fixed and named by its content. Each
// trial of a model run installs the chosen ones in its own container before the agent.
import { Fragment, useMemo, useRef, useState } from "react";
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
import {
  EmptyNote,
  ErrorAlert,
  Loading,
  PageTitle,
  Pill,
  SelectField,
  TextField,
} from "@/components/common";
import { t, Trans } from "@/i18n";
import { API, ApiError, api } from "@/lib/api";
import { ATTACHMENT_EFFECT, fmtBytes, fmtTime, snapshotName } from "@/lib/format";
import { useLoad, useSession } from "@/lib/hooks";
import { sha256 } from "@/lib/sha256";
import type { Attachment, AttachmentDiff, AttachmentFile } from "@/lib/types";

const EXAMPLE = `{
  "schema_version": 1,
  "name": "my-tools",
  "description": "${t("attachments.publish.exampleDescription")}",
  "effects": ["agent_tools"],
  "compatible_harnesses": ["terminus-2"],
  "provides": { "tools": ["mytool"], "env": [] }
}`;
// Larger files are offered for download rather than shown.
const VIEW_MAX_BYTES = 256 * 1024;

interface Picked {
  path: string;
  file: File;
  sha256: string;
  executable: boolean;
}

function fileUrl(id: string, path: string): string {
  const parts = path.split("/").map(encodeURIComponent).join("/");
  return `${API}/attachments/${encodeURIComponent(id)}/files/${parts}`;
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

function FileView({ id, file }: { id: string; file: AttachmentFile }) {
  const view = useLoad(async () => {
    if (file.size > VIEW_MAX_BYTES) return null;
    const response = await fetch(fileUrl(id, file.path), { credentials: "same-origin" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    try {
      return new TextDecoder("utf-8", { fatal: true }).decode(await response.arrayBuffer());
    } catch {
      return null;
    }
  }, [id, file.path]);
  if (view.loading && view.data === undefined) return <Loading />;
  if (view.error) return <ErrorAlert error={view.error} />;
  if (view.data === null)
    return <p className="text-muted-foreground">{t("attachments.file.binary")}</p>;
  return (
    <pre className="max-h-96 overflow-auto rounded-md bg-background p-2 text-[11px]">
      {view.data}
    </pre>
  );
}

function DiffView({ diff }: { diff: AttachmentDiff }) {
  const rows: [string, string, string][] = [
    ...diff.added.map((f): [string, string, string] => ["+", f.path, "text-emerald-700"]),
    ...diff.removed.map((f): [string, string, string] => ["−", f.path, "text-red-700"]),
    ...diff.changed.map((c): [string, string, string] => [
      "~",
      c.before.sha256 === c.after.sha256 ? t("attachments.diff.modeOnly", { path: c.path }) : c.path,
      "text-amber-700",
    ]),
  ];
  if (rows.length === 0) return <p className="text-muted-foreground">{t("attachments.diff.none")}</p>;
  return (
    <ul className="grid gap-0.5 font-mono">
      {rows.map(([mark, path, tone]) => (
        <li key={`${mark}${path}`} className={tone}>
          {mark} {path}
        </li>
      ))}
    </ul>
  );
}

function Detail({ detail, siblings }: { detail: Attachment; siblings: Attachment[] }) {
  const [viewing, setViewing] = useState<string | null>(null);
  const [base, setBase] = useState(detail.parent_id ?? "");
  const diff = useLoad(
    async () =>
      base
        ? (
            await api.get("/attachments/{attachment_id}/diff", {
              path: { attachment_id: detail.id },
              query: { base_id: base },
            })
          ).data
        : null,
    [detail.id, base],
  );
  const others = siblings.filter((s) => s.id !== detail.id);
  return (
    <div className="grid gap-3 text-xs">
      {detail.description && <p>{detail.description}</p>}
      {(detail.provides.tools.length > 0 || detail.provides.env.length > 0) && (
        <p>
          {t("attachments.detail.provides")}
          {[
            ...detail.provides.tools.map((x) => t("attachments.detail.tool", { name: x })),
            ...detail.provides.env.map((x) => t("attachments.detail.env", { name: x })),
          ].join(t("attachments.separator"))}
        </p>
      )}
      <ul className="grid gap-0.5 font-mono">
        {(detail.files ?? []).map((f) => (
          <li key={f.path} title={f.sha256}>
            <div className="flex flex-wrap items-center gap-2">
              <span>
                {f.executable ? "x " : "  "}
                {f.path} <span className="text-muted-foreground">({fmtBytes(f.size)})</span>
              </span>
              <button
                type="button"
                className="underline-offset-2 hover:underline"
                onClick={() => setViewing(viewing === f.path ? null : f.path)}
              >
                {viewing === f.path ? t("attachments.file.collapse") : t("attachments.file.view")}
              </button>
              <a className="underline-offset-2 hover:underline" href={fileUrl(detail.id, f.path)}>
                {t("attachments.download")}
              </a>
            </div>
            {viewing === f.path && <FileView id={detail.id} file={f} />}
          </li>
        ))}
      </ul>
      {others.length > 0 && (
        <div className="grid gap-2">
          <SelectField
            id={`diff-base-${detail.id}`}
            label={t("attachments.detail.compareWith")}
            value={base}
            onChange={(e) => setBase(e.target.value)}
          >
            <option value="">{t("attachments.detail.noCompare")}</option>
            {others.map((s) => (
              <option key={s.id} value={s.id}>
                {snapshotName(s)}
                {s.id === detail.parent_id ? t("attachments.detail.parentMark") : ""} · {fmtTime(s.created_at)}
              </option>
            ))}
          </SelectField>
          {base && diff.loading && !diff.data && <Loading />}
          <ErrorAlert error={diff.error} />
          {base && diff.data && <DiffView diff={diff.data} />}
        </div>
      )}
    </div>
  );
}

export function AttachmentsPage() {
  const { me } = useSession();
  const list = useLoad(async () => (await api.get("/attachments")).data);
  const [open, setOpen] = useState<string | null>(null);
  const [detail, setDetail] = useState<Attachment | null>(null);
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

  const toggle = async (a: Attachment) => {
    if (open === a.id) {
      setOpen(null);
      return;
    }
    setOpen(a.id);
    setDetail(null);
    try {
      setDetail(
        (await api.get("/attachments/{attachment_id}", { path: { attachment_id: a.id } })).data,
      );
    } catch (err) {
      setError(err);
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
  const mayManage = (a: Attachment) => a.owner_id === me.id || me.role === "admin";
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
            <div className="overflow-auto rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>{t("attachments.list.columns.snapshot")}</TableHead>
                    <TableHead>{t("attachments.list.columns.owner")}</TableHead>
                    <TableHead>{t("attachments.list.columns.effects")}</TableHead>
                    <TableHead>Harness</TableHead>
                    <TableHead>{t("attachments.list.columns.content")}</TableHead>
                    <TableHead>{t("attachments.list.columns.state")}</TableHead>
                    <TableHead />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {all.map((a) => {
                    const from = a.parent_id ? byId.get(a.parent_id) : undefined;
                    return (
                      <Fragment key={a.id}>
                        <TableRow>
                          <TableCell>
                            <button
                              type="button"
                              className="text-left"
                              onClick={() => void toggle(a)}
                            >
                              <span className="font-medium underline-offset-2 hover:underline">
                                {a.name}
                              </span>{" "}
                              <span className="font-mono text-xs">{snapshotName(a)}</span>
                            </button>
                            <div
                              className="font-mono text-[11px] text-muted-foreground"
                              title={a.content_digest}
                            >
                              {a.content_digest.slice(0, 19)}…
                            </div>
                            {a.parent_id && (
                              <div className="text-[11px] text-muted-foreground">
                                {t("attachments.list.basedOn", {
                                  name: from ? snapshotName(from) : t("attachments.list.deletedSnapshot"),
                                })}
                              </div>
                            )}
                          </TableCell>
                          <TableCell className="text-xs">
                            {a.owner_username ?? a.owner_id}
                            <div className="text-muted-foreground">{fmtTime(a.created_at)}</div>
                          </TableCell>
                          <TableCell className="text-xs">
                            {a.effects.map((e) => ATTACHMENT_EFFECT[e] ?? e).join(t("attachments.separator"))}
                          </TableCell>
                          <TableCell className="font-mono text-xs">
                            {a.compatible_harnesses.includes("*")
                              ? t("attachments.list.anyHarness")
                              : a.compatible_harnesses.join(", ")}
                          </TableCell>
                          <TableCell className="text-xs">
                            {t("attachments.list.files", { count: a.file_count, size: fmtBytes(a.size_bytes) })}
                            {a.has_bootstrap && (
                              <div>
                                <Pill tone="info">{t("attachments.list.hasBootstrap")}</Pill>
                              </div>
                            )}
                          </TableCell>
                          <TableCell>
                            <Pill tone={a.enabled ? "good" : "muted"}>
                              {a.enabled ? t("attachments.list.enabled") : t("attachments.list.disabled")}
                            </Pill>
                          </TableCell>
                          <TableCell className="whitespace-nowrap text-right">
                            <Button size="sm" variant="outline" asChild>
                              <a href={`${API}/attachments/${encodeURIComponent(a.id)}/archive`}>
                                {t("attachments.download")}
                              </a>
                            </Button>{" "}
                            <Button size="sm" variant="outline" onClick={() => baseOn(a)}>
                              {t("attachments.list.baseOn")}
                            </Button>
                            {mayManage(a) && (
                              <>
                                {" "}
                                <Button
                                  size="sm"
                                  variant="outline"
                                  onClick={() =>
                                    void act(
                                      () =>
                                        api.post("/attachments/{attachment_id}/enabled", {
                                          path: { attachment_id: a.id },
                                          body: { enabled: !a.enabled },
                                        }),
                                      a.enabled ? t("attachments.list.disabledDone") : t("attachments.list.enabledDone"),
                                    )
                                  }
                                >
                                  {a.enabled ? t("attachments.list.disable") : t("attachments.list.enable")}
                                </Button>{" "}
                                <Button
                                  size="sm"
                                  variant="outline"
                                  onClick={() =>
                                    void act(
                                      () =>
                                        api.del("/attachments/{attachment_id}", {
                                          path: { attachment_id: a.id },
                                        }),
                                      t("attachments.list.deleted"),
                                    )
                                  }
                                >
                                  {t("attachments.list.delete")}
                                </Button>
                              </>
                            )}
                          </TableCell>
                        </TableRow>
                        {open === a.id && (
                          <TableRow>
                            <TableCell colSpan={7} className="bg-muted/40">
                              {!detail && <Loading />}
                              {detail && <Detail detail={detail} siblings={sameName(a.name)} />}
                            </TableCell>
                          </TableRow>
                        )}
                      </Fragment>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
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
