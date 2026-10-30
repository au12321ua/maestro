// Published attachments, one block per attachment name with its snapshots as a tree of
// what was made from what (03 section 6): oldest at the top, a straight run of versions
// in one line, and an indent only where a snapshot has been built on more than once. Once
// the block is open, each snapshot says how many files it changed from its parent.
// Opening a snapshot lists its files and compares it with another snapshot of the name.
import { useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ErrorAlert, Loading, Pill, SelectField } from "@/components/common";
import { t } from "@/i18n";
import { API, api } from "@/lib/api";
import { ATTACHMENT_EFFECT, fmtBytes, fmtTime, snapshotName } from "@/lib/format";
import { useLoad } from "@/lib/hooks";
import type { Attachment, AttachmentDiff, AttachmentFile } from "@/lib/types";
import { cn } from "@/lib/utils";

// Larger files are offered for download rather than shown.
const VIEW_MAX_BYTES = 256 * 1024;
// Change summaries are asked for this many snapshots of a block at most.
const SUMMARY_MAX = 30;

function fileUrl(id: string, path: string): string {
  const parts = path.split("/").map(encodeURIComponent).join("/");
  return `${API}/attachments/${encodeURIComponent(id)}/files/${parts}`;
}

interface Group {
  name: string;
  /** Newest first. */
  snapshots: Attachment[];
}

const newer = (a: Attachment, b: Attachment) => (a.created_at < b.created_at ? 1 : -1);

/** Snapshots of one name together; the block published most recently comes first. */
function groupByName(all: Attachment[]): Group[] {
  const names = new Map<string, Attachment[]>();
  for (const a of all) names.set(a.name, [...(names.get(a.name) ?? []), a]);
  return [...names]
    .map(([name, snapshots]) => ({ name, snapshots: snapshots.sort(newer) }))
    .sort((a, b) => newer(a.snapshots[0], b.snapshots[0]));
}

interface TreeNode {
  a: Attachment;
  children: TreeNode[];
}

/** Snapshots linked to their parents, oldest first. A snapshot whose parent was deleted,
 * or that has none, starts a tree of its own. */
function buildTrees(snapshots: Attachment[]): TreeNode[] {
  const nodes = new Map(snapshots.map((a) => [a.id, { a, children: [] as TreeNode[] }]));
  const roots: TreeNode[] = [];
  for (const node of [...nodes.values()].sort((x, y) => -newer(x.a, y.a))) {
    const parent = node.a.parent_id ? nodes.get(node.a.parent_id) : undefined;
    (parent ? parent.children : roots).push(node);
  }
  return roots;
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

function Detail({
  detail,
  siblings,
  initialBase,
}: {
  detail: Attachment;
  siblings: Attachment[];
  initialBase?: string;
}) {
  const [viewing, setViewing] = useState<string | null>(null);
  const [base, setBase] = useState(initialBase ?? detail.parent_id ?? "");
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
                {s.id === detail.parent_id ? t("attachments.detail.parentMark") : ""} ·{" "}
                {fmtTime(s.created_at)}
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

/** How many files a snapshot added, removed and changed from its parent. */
function Changes({ diff }: { diff: AttachmentDiff }) {
  const { added, removed, changed } = diff;
  if (added.length + removed.length + changed.length === 0)
    return <span>{t("attachments.history.noChange")}</span>;
  return (
    <span className="font-mono" title={t("attachments.history.legend")}>
      {added.length > 0 && <span className="mr-1.5 text-emerald-700">+{added.length}</span>}
      {removed.length > 0 && <span className="mr-1.5 text-red-700">−{removed.length}</span>}
      {changed.length > 0 && <span className="mr-1.5 text-amber-700">~{changed.length}</span>}
    </span>
  );
}

export interface HistoryActions {
  mayManage: (a: Attachment) => boolean;
  onBaseOn: (a: Attachment) => void;
  onToggleEnabled: (a: Attachment) => void;
  onDelete: (a: Attachment) => void;
  onError: (err: unknown) => void;
}

function Tree({
  group,
  byId,
  actions,
  focus,
}: {
  group: Group;
  byId: Map<string, Attachment>;
  actions: HistoryActions;
  focus?: { snapshot: string; base?: string };
}) {
  const [open, setOpen] = useState<string | null>(focus?.snapshot ?? null);
  const [detail, setDetail] = useState<Attachment | null>(null);
  const ids = group.snapshots.map((s) => s.id).join();
  const trees = useMemo(() => buildTrees(group.snapshots), [group.snapshots]);
  const summaries = useLoad(async () => {
    const found = await Promise.all(
      group.snapshots
        .filter((s) => s.parent_id)
        .slice(0, SUMMARY_MAX)
        .map(async (s): Promise<[string, AttachmentDiff | null]> => {
          try {
            const res = await api.get("/attachments/{attachment_id}/diff", {
              path: { attachment_id: s.id },
            });
            return [s.id, res.data];
          } catch {
            return [s.id, null]; // the parent is gone: nothing to compare with
          }
        }),
    );
    return new Map(found);
  }, [ids]);

  const show = async (id: string) => {
    setDetail(null);
    try {
      setDetail((await api.get("/attachments/{attachment_id}", { path: { attachment_id: id } })).data);
    } catch (err) {
      actions.onError(err);
    }
  };
  useEffect(() => {
    if (focus?.snapshot) void show(focus.snapshot);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focus?.snapshot]);
  const toggle = (id: string) => {
    if (open === id) {
      setOpen(null);
      return;
    }
    setOpen(id);
    void show(id);
  };

  const latest = group.snapshots[0];
  const row = (node: TreeNode) => {
    const a = node.a;
    const summary = summaries.data?.get(a.id);
    const parent = a.parent_id ? byId.get(a.parent_id) : undefined;
    const isRoot = !a.parent_id || !group.snapshots.some((s) => s.id === a.parent_id);
    // Where a snapshot is not placed under its parent, say what it was made from.
    const origin = !isRoot
      ? null
      : a.parent_id
        ? t("attachments.history.basedOn", {
            name: parent ? snapshotName(parent) : t("attachments.list.deletedSnapshot"),
          })
        : trees.length > 1
          ? t("attachments.history.separate")
          : t("attachments.history.first");
    return (
      <li key={a.id} className="relative">
        <span
          className={cn(
            "absolute top-1.5 -left-[1.72rem] size-2.5 rounded-full border-2 border-background ring-1 ring-border",
            a.id === latest.id ? "bg-primary" : "bg-muted-foreground/60",
          )}
        />
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <button type="button" className="text-left" onClick={() => toggle(a.id)}>
            <span className="font-mono text-sm font-medium underline-offset-2 hover:underline">
              {snapshotName(a)}
            </span>
          </button>
          {a.id === latest.id && <Pill tone="info">{t("attachments.history.latest")}</Pill>}
          {!a.enabled && <Pill tone="muted">{t("attachments.list.disabled")}</Pill>}
          {a.has_bootstrap && <Pill tone="neutral">{t("attachments.list.hasBootstrap")}</Pill>}
          {node.children.length > 1 && (
            <Pill tone="warn">{t("attachments.history.branches", { count: node.children.length })}</Pill>
          )}
          <span className="text-xs text-muted-foreground">
            {fmtTime(a.created_at)} · {a.owner_username ?? a.owner_id}
          </span>
          <span className="ml-auto flex flex-wrap gap-0.5">
            <Button size="sm" variant="ghost" className="h-7 px-2 text-xs" asChild>
              <a href={`${API}/attachments/${encodeURIComponent(a.id)}/archive`}>
                {t("attachments.download")}
              </a>
            </Button>
            <Button
              size="sm"
              variant="ghost"
              className="h-7 px-2 text-xs"
              onClick={() => actions.onBaseOn(a)}
            >
              {t("attachments.list.baseOn")}
            </Button>
            {actions.mayManage(a) && (
              <>
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-7 px-2 text-xs"
                  onClick={() => actions.onToggleEnabled(a)}
                >
                  {a.enabled ? t("attachments.list.disable") : t("attachments.list.enable")}
                </Button>
                <Button
                  size="sm"
                  variant="ghost"
                  className="h-7 px-2 text-xs"
                  onClick={() => actions.onDelete(a)}
                >
                  {t("attachments.list.delete")}
                </Button>
              </>
            )}
          </span>
        </div>
        <div className="mt-0.5 flex flex-wrap items-center gap-x-3 text-xs text-muted-foreground">
          {a.parent_id && summary && (
            <span>
              {t("attachments.history.changes")}
              <Changes diff={summary} />
            </span>
          )}
          {origin && <span className="text-foreground">{origin}</span>}
          <span>{t("attachments.list.files", { count: a.file_count, size: fmtBytes(a.size_bytes) })}</span>
          <span className="font-mono" title={a.content_digest}>
            {a.content_digest.slice(7, 19)}
          </span>
        </div>
        {open === a.id && (
          <div className="mt-2 rounded-md bg-muted/40 p-3">
            {!detail && <Loading />}
            {detail && detail.id === a.id && (
              <Detail
                detail={detail}
                siblings={group.snapshots}
                initialBase={focus?.snapshot === a.id ? focus.base : undefined}
              />
            )}
          </div>
        )}
      </li>
    );
  };

  /** A line of versions, each made from the one above; where one was built on more than
   * once, every follow-up starts an indented line of its own. */
  const chain = (start: TreeNode) => {
    const rows = [row(start)];
    let end = start;
    while (end.children.length === 1) {
      end = end.children[0];
      rows.push(row(end));
    }
    return (
      <>
        {rows}
        {end.children.map((child, i) => (
          <li key={`branch-${child.a.id}`} className="grid gap-2">
            <span className="text-xs font-medium text-muted-foreground">
              ↳ {t("attachments.history.branch", { n: i + 1 })}
            </span>
            <ol className="relative ml-1 grid gap-4 border-l-2 border-primary/30 pl-5">
              {chain(child)}
            </ol>
          </li>
        ))}
      </>
    );
  };

  return (
    <div className="grid gap-5">
      {trees.map((root) => (
        <ol key={root.a.id} className="relative ml-2 grid gap-4 border-l pl-5">
          {chain(root)}
        </ol>
      ))}
    </div>
  );
}

function GroupBlock({
  group,
  byId,
  actions,
  expanded,
  onToggle,
  focus,
}: {
  group: Group;
  byId: Map<string, Attachment>;
  actions: HistoryActions;
  expanded: boolean;
  onToggle: () => void;
  focus?: { snapshot: string; base?: string };
}) {
  const latest = group.snapshots[0];
  return (
    <div className="rounded-md border">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2">
        <button
          type="button"
          className="flex min-w-0 flex-1 items-start gap-2 text-left"
          aria-expanded={expanded}
          onClick={onToggle}
        >
          {expanded ? (
            <ChevronDown className="mt-0.5 size-4 shrink-0" />
          ) : (
            <ChevronRight className="mt-0.5 size-4 shrink-0" />
          )}
          <span className="min-w-0">
            <span className="break-all font-medium">{group.name}</span>
            <span className="ml-2 text-xs text-muted-foreground">
              {t("attachments.history.count", { count: group.snapshots.length })}
              {" · "}
              {t("attachments.history.latestIs", {
                snapshot: snapshotName(latest),
                time: fmtTime(latest.created_at),
                owner: latest.owner_username ?? latest.owner_id,
              })}
            </span>
            <span className="block text-xs text-muted-foreground">
              {latest.effects.map((e) => ATTACHMENT_EFFECT[e] ?? e).join(t("attachments.separator"))}
              {" · "}
              {latest.compatible_harnesses.includes("*")
                ? t("attachments.list.anyHarness")
                : latest.compatible_harnesses.join(", ")}
            </span>
          </span>
        </button>
        <Button size="sm" variant="outline" onClick={() => actions.onBaseOn(latest)}>
          {t("attachments.history.baseOnLatest")}
        </Button>
      </div>
      {expanded && (
        <div className="border-t px-3 py-4">
          <Tree group={group} byId={byId} actions={actions} focus={focus} />
        </div>
      )}
    </div>
  );
}

export function AttachmentHistory({
  all,
  actions,
  focus,
}: {
  all: Attachment[];
  actions: HistoryActions;
  /** From the address: open this snapshot, compared with `base` when given. */
  focus?: { snapshot: string; base?: string; name?: string };
}) {
  const groups = useMemo(() => groupByName(all), [all]);
  const byId = useMemo(() => new Map(all.map((a) => [a.id, a])), [all]);
  const focused = focus?.snapshot ? byId.get(focus.snapshot)?.name : focus?.name;
  // Most recently published first; that one starts open, as does the one an address names.
  const [expanded, setExpanded] = useState<Set<string>>(
    new Set([...(groups[0] ? [groups[0].name] : []), ...(focused ? [focused] : [])]),
  );
  const flip = (name: string) => {
    const next = new Set(expanded);
    if (!next.delete(name)) next.add(name);
    setExpanded(next);
  };
  return (
    <div className="grid gap-3">
      {groups.map((g) => (
        <GroupBlock
          key={g.name}
          group={g}
          byId={byId}
          actions={actions}
          expanded={expanded.has(g.name)}
          onToggle={() => flip(g.name)}
          focus={focus?.snapshot && byId.get(focus.snapshot)?.name === g.name ? focus : undefined}
        />
      ))}
    </div>
  );
}
