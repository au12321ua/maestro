import { useState, type ReactNode } from "react";
import { ChevronDown, ChevronRight, Download, Package } from "lucide-react";
import { Button } from "@/components/ui/button";
import { LongName, StatusPill } from "@/components/common";
import { API } from "@/lib/api";
import {
  ARTIFACT_REASON,
  ARTIFACT_STATUS,
  ATTEMPT_STATE,
  commonPrefix,
  fmtBytes,
  fmtTime,
  label,
} from "@/lib/format";
import { limit, useSession } from "@/lib/hooks";
import type { Artifact, Trial } from "@/lib/types";
import { cn } from "@/lib/utils";

function artifactReason(artifact: Artifact): string | null {
  if (!artifact.status_reason) return null;
  return ARTIFACT_REASON[artifact.status_reason] ?? artifact.status_reason;
}

function ArtifactLink({ artifact }: { artifact: Artifact }) {
  const name =
    artifact.relative_path.split("/").pop() || artifact.relative_path;
  if (artifact.status !== "available") {
    const reason = artifactReason(artifact);
    return (
      <span
        className="text-muted-foreground"
        title={`${artifact.relative_path}${reason ? `：${reason}` : ""}`}
      >
        {name}（{label(ARTIFACT_STATUS, artifact.status)[0]}）
      </span>
    );
  }
  return (
    <a
      className="inline-flex min-w-0 items-center gap-1 text-primary hover:underline"
      href={`${API}/artifacts/${artifact.id}/download`}
      title={artifact.relative_path}
    >
      <Download className="size-3 shrink-0" />
      <span className="break-all">{name}</span>
    </a>
  );
}

/** A folder of the tree: a task, trial, attempt or a directory of a relative path. */
interface Folder {
  key: string;
  title: ReactNode;
  /** Set on directories of a relative path: their name. */
  dir?: string;
  /** Query of the zip for this level and what it holds; directories have neither. */
  archive?: { query: string; what: string };
  /** Open unless toggled: tasks and trials start closed, the rest open. */
  open: boolean;
  folders: Folder[];
  files: Artifact[];
}

function addPaths(folder: Folder, artifacts: Artifact[]) {
  for (const artifact of artifacts) {
    const parts = artifact.relative_path.split("/").filter(Boolean);
    let at = folder;
    for (const part of parts.slice(0, -1)) {
      const key = `${at.key}/${part}`;
      let next = at.folders.find((f) => f.key === key);
      if (!next) {
        next = {
          key,
          title: null,
          dir: part,
          open: true,
          folders: [],
          files: [],
        };
        at.folders.push(next);
      }
      at = next;
    }
    at.files.push(artifact);
  }
  compact(folder);
}

/** A directory holding only one directory is shown as one row: ``a/b/c/``. */
function compact(folder: Folder) {
  folder.folders = folder.folders.map((child) => {
    while (
      child.dir &&
      child.files.length === 0 &&
      child.folders.length === 1
    ) {
      const only = child.folders[0];
      if (!only.dir) break;
      child = { ...only, dir: `${child.dir}/${only.dir}` };
    }
    compact(child);
    return child;
  });
}

function build(
  artifacts: Artifact[],
  trials: Trial[],
  focus: string,
): Folder[] {
  const prefix = commonPrefix(trials.map((t) => t.task_key));
  const tasks = new Map<string, Folder>();
  const roots: Folder[] = [];
  const runLevel = artifacts.filter((a) => !a.trial_id);
  if (runLevel.length > 0) {
    const run: Folder = {
      key: "run",
      title: "运行级文件",
      open: true,
      folders: [],
      files: [],
    };
    addPaths(run, runLevel);
    roots.push(run);
  }
  const sorted = [...trials].sort(
    (a, b) =>
      a.task_key.localeCompare(b.task_key) ||
      a.repetition_index - b.repetition_index,
  );
  for (const trial of sorted) {
    if (focus && trial.id !== focus) continue;
    const own = artifacts.filter((a) => a.trial_id === trial.id);
    if (own.length === 0) continue;
    let task = tasks.get(trial.task_key);
    if (!task) {
      task = {
        key: `task:${trial.task_key}`,
        title: (
          <LongName
            value={trial.task_key}
            prefix={prefix}
            copy={false}
            className="font-mono"
          />
        ),
        archive: {
          query: `task_key=${encodeURIComponent(trial.task_key)}`,
          what: `任务 ${trial.task_key} 的产物`,
        },
        open: !!focus,
        folders: [],
        files: [],
      };
      tasks.set(trial.task_key, task);
      roots.push(task);
    }
    const node: Folder = {
      key: `trial:${trial.id}`,
      title: `试次 #${trial.repetition_index}`,
      archive: {
        query: `trial_id=${trial.id}`,
        what: `试次 #${trial.repetition_index} 的产物`,
      },
      open: !!focus,
      folders: [],
      files: [],
    };
    addPaths(
      node,
      own.filter((a) => !a.attempt_id),
    );
    for (const attempt of [...trial.attempts].sort(
      (a, b) => a.attempt_index - b.attempt_index,
    )) {
      const files = own.filter((a) => a.attempt_id === attempt.id);
      if (files.length === 0) continue;
      const child: Folder = {
        key: `attempt:${attempt.id}`,
        title: (
          <span>
            尝试 {attempt.attempt_index}
            <span className="ml-1 text-xs text-muted-foreground">
              {label(ATTEMPT_STATE, attempt.state)[0]}
            </span>
          </span>
        ),
        archive: {
          query: `attempt_id=${attempt.id}`,
          what: `尝试 ${attempt.attempt_index} 的产物`,
        },
        open: true,
        folders: [],
        files: [],
      };
      addPaths(child, files);
      node.folders.push(child);
    }
    task.folders.push(node);
  }
  if (tasks.size === 1) for (const task of tasks.values()) task.open = true;
  return roots;
}

function everyFile(folder: Folder): Artifact[] {
  return [...folder.files, ...folder.folders.flatMap(everyFile)];
}

const INDENT = 1.25; // rem per level

/** A run's artifacts as task → trial → attempt → path; each of the first three levels,
 *  and the whole run, downloads as a zip once the run has ended. */
export function ArtifactTree({
  runId,
  artifacts,
  trials,
  ended,
  trialId,
  onTrialId,
  onDelete,
}: {
  runId: string;
  artifacts: Artifact[];
  trials: Trial[];
  ended: boolean;
  trialId: string;
  onTrialId: (trialId: string) => void;
  onDelete?: (artifact: Artifact) => Promise<void>;
}) {
  const { capabilities } = useSession();
  const maxBytes = limit(capabilities, "export_max_bytes", Infinity);
  const [toggled, setToggled] = useState<Set<string>>(new Set());
  const roots = build(artifacts, trials, trialId);
  const archiveBase = `${API}/runs/${runId}/artifacts/archive`;

  const toggle = (key: string) =>
    setToggled((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  const zip = (query: string | null, files: Artifact[], what: string) => {
    if (!ended) return null;
    const bytes = files
      .filter((a) => a.status === "available")
      .reduce((sum, a) => sum + (a.size_bytes ?? 0), 0);
    if (bytes > maxBytes)
      return (
        <Button
          size="sm"
          variant="ghost"
          className="h-7 px-2"
          disabled
          title={`超过 ${fmtBytes(maxBytes)} 的打包上限，请下载更小的一层或使用导出`}
        >
          <Package /> {query ? "zip" : "整个运行 zip"}
        </Button>
      );
    return (
      <Button asChild size="sm" variant="ghost" className="h-7 px-2">
        <a
          href={query ? `${archiveBase}?${query}` : archiveBase}
          title={`下载${what}（zip）`}
          aria-label={`下载${what}（zip）`}
        >
          <Package /> {query ? "zip" : "整个运行 zip"}
        </a>
      </Button>
    );
  };

  const rows: ReactNode[] = [];
  const walk = (folder: Folder, depth: number) => {
    const open = folder.open !== toggled.has(folder.key);
    const files = everyFile(folder);
    const bytes = files.reduce((sum, a) => sum + (a.size_bytes ?? 0), 0);
    rows.push(
      <tr key={folder.key} className="border-b bg-muted/30">
        <td className="py-1 pl-2 pr-2" colSpan={4}>
          <div
            className="flex min-w-0 items-center gap-1"
            style={{ paddingLeft: `${depth * INDENT}rem` }}
          >
            <button
              type="button"
              className="inline-flex min-w-0 items-center gap-1 text-left"
              aria-expanded={open}
              onClick={() => toggle(folder.key)}
            >
              {open ? (
                <ChevronDown className="size-4 shrink-0" />
              ) : (
                <ChevronRight className="size-4 shrink-0" />
              )}
              <span className="min-w-0 break-all">
                {folder.dir ? (
                  <span className="font-mono">{folder.dir}/</span>
                ) : (
                  folder.title
                )}
              </span>
            </button>
            <span className="shrink-0 text-xs text-muted-foreground">
              {files.length} 个 · {fmtBytes(bytes)}
            </span>
          </div>
        </td>
        <td className="py-1 text-right" colSpan={onDelete ? 2 : 1}>
          {folder.archive &&
            zip(folder.archive.query, files, folder.archive.what)}
        </td>
      </tr>,
    );
    if (!open) return;
    for (const child of folder.folders) walk(child, depth + 1);
    for (const a of folder.files) {
      rows.push(
        <tr key={a.id} className="border-b last:border-0">
          <td className="max-w-[28rem] py-1 pl-2 pr-2">
            <div style={{ paddingLeft: `${(depth + 1) * INDENT + 1.25}rem` }}>
              <ArtifactLink artifact={a} />
              <span className="ml-2 text-xs text-muted-foreground">
                {a.kind}
              </span>
            </div>
          </td>
          <td className="py-1 pr-2 text-xs tabular-nums">
            {fmtBytes(a.size_bytes)}
          </td>
          <td className="py-1 pr-2">
            <StatusPill value={label(ARTIFACT_STATUS, a.status)} />
            {artifactReason(a) && (
              <div className="mt-1 text-xs text-muted-foreground">
                {artifactReason(a)}
              </div>
            )}
          </td>
          <td className="py-1 pr-2 text-xs">
            {a.expires_at ? (
              <span className={cn(a.status === "expired" && "text-amber-800")}>
                {fmtTime(a.expires_at)}
                {a.status === "expired" && "（已过期）"}
              </span>
            ) : (
              <span className="text-muted-foreground">试次结束后起算</span>
            )}
          </td>
          <td />
          {onDelete && (
            <td className="py-1 text-right">
              {a.status !== "deleted" && a.status !== "deleting" && (
                <Button
                  size="sm"
                  variant="outline"
                  aria-label={`删除产物 ${a.relative_path}`}
                  onClick={() => void onDelete(a)}
                >
                  删除
                </Button>
              )}
            </td>
          )}
        </tr>,
      );
    }
  };
  for (const root of roots) walk(root, 0);

  const trialName = (t: Trial) => `${t.task_key} #${t.repetition_index}`;
  return (
    <div className="grid gap-2">
      <div className="flex min-w-0 flex-wrap items-center gap-2 text-sm">
        <label
          htmlFor="artifacts-of"
          className="shrink-0 text-muted-foreground"
        >
          试次
        </label>
        <select
          id="artifacts-of"
          className="h-8 min-w-0 max-w-full rounded-md border border-input bg-background px-2 text-sm"
          value={trialId}
          onChange={(e) => onTrialId(e.target.value)}
        >
          <option value="">全部（{artifacts.length}）</option>
          {trials.map((t) => (
            <option key={t.id} value={t.id}>
              {trialName(t)}（
              {artifacts.filter((a) => a.trial_id === t.id).length}）
            </option>
          ))}
        </select>
        <span className="ml-auto">
          {ended ? (
            zip(null, artifacts, "整个运行的产物")
          ) : (
            <span className="text-xs text-muted-foreground">
              运行结束后可按任务、试次、尝试打包下载
            </span>
          )}
        </span>
      </div>
      <div className="overflow-x-auto rounded-md border">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b text-left text-xs text-muted-foreground">
              <th className="py-2 pl-2 pr-2 font-medium">文件</th>
              <th className="py-2 pr-2 font-medium">大小</th>
              <th className="py-2 pr-2 font-medium">状态</th>
              <th className="py-2 pr-2 font-medium">到期</th>
              <th />
              {onDelete && <th />}
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </div>
  );
}
