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
import { API, ApiError, api } from "@/lib/api";
import { ATTACHMENT_EFFECT, fmtBytes, fmtTime, snapshotName } from "@/lib/format";
import { useLoad, useSession } from "@/lib/hooks";
import { sha256 } from "@/lib/sha256";
import type { Attachment, AttachmentDiff, AttachmentFile } from "@/lib/types";

const EXAMPLE = `{
  "schema_version": 1,
  "name": "my-tools",
  "description": "说明（可选）",
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
    return <p className="text-muted-foreground">二进制或较大的文件，请下载查看。</p>;
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
      c.before.sha256 === c.after.sha256 ? `${c.path}（仅可执行位）` : c.path,
      "text-amber-700",
    ]),
  ];
  if (rows.length === 0) return <p className="text-muted-foreground">文件没有差异。</p>;
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
          提供：
          {[
            ...detail.provides.tools.map((x) => `工具 ${x}`),
            ...detail.provides.env.map((x) => `环境变量 ${x}`),
          ].join("、")}
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
                {viewing === f.path ? "收起" : "查看"}
              </button>
              <a className="underline-offset-2 hover:underline" href={fileUrl(detail.id, f.path)}>
                下载
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
            label="与另一快照比较"
            value={base}
            onChange={(e) => setBase(e.target.value)}
          >
            <option value="">不比较</option>
            {others.map((s) => (
              <option key={s.id} value={s.id}>
                {snapshotName(s)}
                {s.id === detail.parent_id ? "（父快照）" : ""} · {fmtTime(s.created_at)}
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
    setBusy("读取文件…");
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
      setBusy("核对已有内容…");
      const shas = [...new Set(picked.map((p) => p.sha256))];
      const { missing } = (
        await api.post("/attachment-contents/missing", { body: { sha256: shas } })
      ).data;
      const contents = new Map(picked.map((p) => [p.sha256, p.file]));
      for (const [index, sha] of missing.entries()) {
        setBusy(`上传 ${index + 1}/${missing.length}…`);
        await api.upload("/attachment-contents/{sha256}", { sha256: sha }, contents.get(sha)!);
      }
      setBusy("发布…");
      const res = await api.post("/attachments", {
        body: {
          files: picked.map(({ path, sha256, executable }) => ({ path, sha256, executable })),
          label: label.trim() || null,
          parent_id: parentId || null,
        },
      });
      toast.success(
        `已发布 ${res.data.name} ${snapshotName(res.data)}（上传 ${missing.length} 个文件）`,
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
      <PageTitle title="附件" />
      <Card>
        <CardHeader>
          <CardTitle className="text-base">已发布的快照</CardTitle>
          <CardDescription>
            附件是随模型运行加载的文件，例如工具、SDK
            或安装脚本。每次发布得到一份内容固定的快照，内容相同就是同一份；可以基于任意一份快照再发布，分出多个方向。运行时，每个试次在自己的容器里复制附件到
            <code className="mx-1">/opt/maestro/attachments/</code>
            并运行其中的 <code>bootstrap.sh</code>，然后才开始 Agent；安装可以联网，Agent
            和验证阶段仍按平台网络限制执行。
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 text-sm">
          {list.loading && !list.data && <Loading />}
          <ErrorAlert error={list.error} />
          {list.data && list.data.length === 0 && <EmptyNote>还没有发布附件。</EmptyNote>}
          {all.length > 0 && (
            <div className="overflow-auto rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>附件与快照</TableHead>
                    <TableHead>发布者</TableHead>
                    <TableHead>影响范围</TableHead>
                    <TableHead>Harness</TableHead>
                    <TableHead>内容</TableHead>
                    <TableHead>状态</TableHead>
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
                                基于 {from ? snapshotName(from) : "已删除的快照"}
                              </div>
                            )}
                          </TableCell>
                          <TableCell className="text-xs">
                            {a.owner_username ?? a.owner_id}
                            <div className="text-muted-foreground">{fmtTime(a.created_at)}</div>
                          </TableCell>
                          <TableCell className="text-xs">
                            {a.effects.map((e) => ATTACHMENT_EFFECT[e] ?? e).join("、")}
                          </TableCell>
                          <TableCell className="font-mono text-xs">
                            {a.compatible_harnesses.includes("*")
                              ? "任意"
                              : a.compatible_harnesses.join(", ")}
                          </TableCell>
                          <TableCell className="text-xs">
                            {a.file_count} 个文件 · {fmtBytes(a.size_bytes)}
                            {a.has_bootstrap && (
                              <div>
                                <Pill tone="info">含安装脚本</Pill>
                              </div>
                            )}
                          </TableCell>
                          <TableCell>
                            <Pill tone={a.enabled ? "good" : "muted"}>
                              {a.enabled ? "可用" : "停用"}
                            </Pill>
                          </TableCell>
                          <TableCell className="whitespace-nowrap text-right">
                            <Button size="sm" variant="outline" asChild>
                              <a href={`${API}/attachments/${encodeURIComponent(a.id)}/archive`}>
                                下载
                              </a>
                            </Button>{" "}
                            <Button size="sm" variant="outline" onClick={() => baseOn(a)}>
                              基于此发布
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
                                      a.enabled ? "已停用" : "已启用",
                                    )
                                  }
                                >
                                  {a.enabled ? "停用" : "启用"}
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
                                      "已移入回收站",
                                    )
                                  }
                                >
                                  删除
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
          <CardTitle className="text-base">发布快照</CardTitle>
          <CardDescription>
            选择附件所在的文件夹，根目录里放 manifest.json；可选 bootstrap.sh 由 bash
            运行，工作目录是附件目录，环境变量 MAESTRO_ATTACHMENT_DIR
            指向它。只上传服务端还没有的文件，发布只检查不运行；文件名只能用字母、数字和
            ._+@-，.git 目录不上传。标签可选，在同名附件内唯一，发布后不能改。
          </CardDescription>
        </CardHeader>
        <CardContent className="grid gap-3 text-sm">
          {parent && !picked.length && (
            <p className="text-xs">
              将基于 {parent.name} {snapshotName(parent)} 发布；请选择改好的文件夹。
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
                {name ? `附件 ${name}` : "没有在 manifest.json 中找到名称"} · {picked.length}{" "}
                个文件 · {fmtBytes(picked.reduce((n, p) => n + p.file.size, 0))}
              </p>
              <div className="grid gap-3 md:grid-cols-2">
                <TextField
                  id="attachment-label"
                  label="标签（可选）"
                  value={label}
                  placeholder="例如 v2、try-cache"
                  onChange={(e) => setLabel(e.target.value)}
                />
                <SelectField
                  id="attachment-parent"
                  label="父快照"
                  value={parentId}
                  onChange={(e) => setParentId(e.target.value)}
                >
                  <option value="">无</option>
                  {sameName(name).map((s) => (
                    <option key={s.id} value={s.id}>
                      {snapshotName(s)} · {fmtTime(s.created_at)}
                    </option>
                  ))}
                </SelectField>
              </div>
              <div className="max-h-72 overflow-auto rounded-md border p-2">
                <p className="mb-1 text-xs text-muted-foreground">
                  浏览器读不到文件权限：勾选需要可执行的文件（默认沿用父快照，否则按是否以 #!
                  开头判断）。
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
                  {busy ?? "上传并发布"}
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
              manifest.json 示例
            </summary>
            <pre className="mt-2 overflow-auto rounded-md bg-muted p-3 text-xs">{EXAMPLE}</pre>
            <p className="mt-1 text-xs text-muted-foreground">
              effects 目前支持
              model_call、agent_tools、task_environment；改变验证或评分的附件暂不支持。compatible_harnesses
              填 Harness 适配器名，或 ["*"] 表示任意。provides
              用于发现两个附件之间的冲突。快照没有版本号，用发布时的标签区分。
            </p>
          </details>
        </CardContent>
      </Card>
    </div>
  );
}
