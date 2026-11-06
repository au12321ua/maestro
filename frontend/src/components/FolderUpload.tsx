// Choosing a local folder and sending it: the same panel for an attachment snapshot and an
// uploaded dataset version. It reads and hashes the files, decides their executable bits,
// shows what changed from the version this one is based on, uploads only the contents the
// service lacks, and then hands the file list to the caller, who checks and records it.
import { useMemo, useRef, useState, type ReactNode } from "react";
import { FolderOpen, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ErrorAlert } from "@/components/common";
import { t } from "@/i18n";
import {
  countChanges,
  diffFiles,
  diffTasks,
  entries,
  fromDrop,
  fromInput,
  readFiles,
  uploadMissing,
  withModes,
  type BaseFile,
  type Change,
  type LocalFile,
  type ModedFile,
  type PickedFile,
} from "@/lib/folder";
import { fmtBytes } from "@/lib/format";
import { cn } from "@/lib/utils";

/** Hashing reads a whole file into memory; larger single files go through the SDK. */
const SINGLE_FILE_MAX = 512 * 1024 * 1024;

const MARK: Record<Change, string> = { added: "+", removed: "−", changed: "~", same: "=" };
const MARK_TONE: Record<Change, string> = {
  added: "text-emerald-700",
  removed: "text-red-700",
  changed: "text-amber-700",
  same: "text-muted-foreground",
};

export interface FolderBase {
  /** How the base is named to people, e.g. "my-tools · v3". */
  label: string;
  files: BaseFile[];
}

export interface Chosen {
  folder: string;
  files: ModedFile[];
  /** How many contents had to be uploaded (the rest the service already had). */
  uploaded: number;
}

interface Progress {
  text: string;
  done?: number;
  total?: number;
}

export function FolderUpload({
  base,
  tasks = false,
  maxFiles,
  maxBytes,
  submitLabel,
  blocked,
  check,
  onFolder,
  onSubmit,
  children,
}: {
  /** What the new version is based on: its files decide carried-over executable bits. */
  base?: FolderBase;
  /** Top-level folders are tasks: count them and summarize changes per task. */
  tasks?: boolean;
  maxFiles: number;
  maxBytes: number;
  submitLabel: string;
  /** Why submitting is not possible yet (a missing field), shown by the button. */
  blocked?: string;
  /** Problems with the chosen files that stop submitting. */
  check?: (files: ModedFile[]) => string[];
  /** Called once a folder is chosen, before hashing: lets the caller pick a base. */
  onFolder?: (folder: string, files: LocalFile[]) => Promise<void> | void;
  /** Records the version; contents are already uploaded. Resolves when done. */
  onSubmit: (chosen: Chosen) => Promise<void>;
  /** Fields between the summary and the file lists (label, base, names). */
  children?: (chosen: Omit<Chosen, "uploaded">) => ReactNode;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [folder, setFolder] = useState("");
  const [picked, setPicked] = useState<PickedFile[] | null>(null);
  const [modes, setModes] = useState<Map<string, boolean>>(new Map());
  const [progress, setProgress] = useState<Progress | null>(null);
  const [error, setError] = useState<unknown>();
  const [dragging, setDragging] = useState(false);
  const [changedOnly, setChangedOnly] = useState(true);

  const files = useMemo(
    () => (picked ? withModes(picked, base?.files, modes) : []),
    [picked, base, modes],
  );
  const changes = useMemo(() => (base ? diffFiles(base.files, entries(files)) : null), [base, files]);
  const byPath = new Map(changes?.map((c) => [c.path, c.change]) ?? []);
  const fileCounts = changes && countChanges(changes);
  const taskCounts = changes && tasks ? countChanges(diffTasks(changes)) : null;
  const taskNames = new Set(files.filter((f) => f.path.includes("/")).map((f) => f.path.split("/")[0]));
  const size = files.reduce((n, f) => n + f.size, 0);
  const executables = files.filter((f) => f.executable);

  const limits: string[] = [];
  if (picked && files.length > maxFiles)
    limits.push(t("common.folder.tooManyFiles", { count: files.length, max: maxFiles }));
  if (picked && size > maxBytes)
    limits.push(t("common.folder.tooLarge", { size: fmtBytes(size), max: fmtBytes(maxBytes) }));
  const problems = picked ? [...limits, ...(check?.(files) ?? [])] : [];

  const reset = () => {
    setPicked(null);
    setFolder("");
    setModes(new Map());
    setError(undefined);
    if (input.current) input.current.value = "";
  };

  const load = async (chosen: { folder: string; files: LocalFile[] } | null) => {
    setError(undefined);
    if (!chosen) {
      setError(new Error(t("common.folder.dropOneFolder")));
      return;
    }
    if (chosen.files.length === 0) return;
    const huge = chosen.files.find((f) => f.file.size > SINGLE_FILE_MAX);
    if (huge) {
      setError(
        new Error(
          t("common.folder.fileTooLarge", {
            path: huge.path,
            size: fmtBytes(huge.file.size),
            max: fmtBytes(SINGLE_FILE_MAX),
          }),
        ),
      );
      return;
    }
    setPicked(null);
    setModes(new Map());
    setFolder(chosen.folder);
    try {
      await onFolder?.(chosen.folder, chosen.files);
      const read = await readFiles(chosen.files, (done, total) =>
        setProgress({ text: t("common.folder.reading"), done, total }),
      );
      setPicked(read);
    } catch (err) {
      setError(err);
    } finally {
      setProgress(null);
    }
  };

  const submit = async () => {
    setError(undefined);
    try {
      setProgress({ text: t("common.folder.checking") });
      const uploaded = await uploadMissing(files, (done, total) =>
        setProgress({ text: t("common.folder.uploading"), done, total }),
      );
      setProgress({ text: t("common.folder.recording") });
      await onSubmit({ folder, files, uploaded });
      reset();
    } catch (err) {
      setError(err);
    } finally {
      setProgress(null);
    }
  };

  const setMode = (path: string, value: boolean) =>
    setModes((m) => new Map(m).set(path, value));
  const busy = progress !== null;
  const shown = changes
    ? changes.filter((c) => !changedOnly || c.change !== "same")
    : files.map((f) => ({ path: f.path, change: "added" as Change }));
  const filesByPath = new Map(files.map((f) => [f.path, f]));

  return (
    <div className="grid gap-3 text-sm">
      <div
        data-testid="folder-drop"
        className={cn(
          "grid gap-1 rounded-md border-2 border-dashed p-4 text-center",
          dragging && "border-primary bg-accent",
        )}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault();
          setDragging(false);
          if (busy) return;
          void fromDrop(e.dataTransfer.items).then(load, setError);
        }}
      >
        <div className="flex flex-wrap items-center justify-center gap-2">
          <Button type="button" variant="outline" disabled={busy} onClick={() => input.current?.click()}>
            <FolderOpen />
            {t("common.folder.choose")}
          </Button>
          <span className="text-muted-foreground">{t("common.folder.orDrop")}</span>
        </div>
        <p className="text-xs text-muted-foreground">
          {base
            ? t("common.folder.basedOn", { base: base.label })
            : t("common.folder.noBase")}
        </p>
        <input
          ref={input}
          type="file"
          multiple
          hidden
          aria-label={t("common.folder.choose")}
          data-testid="folder-input"
          onChange={(e) => void load(fromInput(e.target.files))}
          {...({ webkitdirectory: "" } as Record<string, string>)}
        />
      </div>

      {picked && (
        <>
          <div className="grid gap-0.5">
            <p className="font-medium" data-testid="folder-summary">
              {t("common.folder.summary", {
                folder: folder || "—",
                count: files.length,
                size: fmtBytes(size),
              })}
              {tasks && t("common.folder.taskCount", { count: taskNames.size })}
            </p>
            {base && fileCounts && (
              <p className="text-xs text-muted-foreground" data-testid="folder-diff">
                {t("common.folder.diff", { base: base.label, ...fileCounts })}
              </p>
            )}
            {base && taskCounts && (
              <p className="text-xs" data-testid="folder-task-diff">
                {t("common.folder.taskDiff", { base: base.label, ...taskCounts })}
              </p>
            )}
          </div>

          {children?.({ folder, files })}

          {problems.length > 0 && (
            <ul className="grid gap-0.5 text-xs text-destructive" data-testid="folder-problems">
              {problems.map((p) => (
                <li key={p}>{p}</li>
              ))}
            </ul>
          )}

          <div className="grid gap-1 rounded-md border p-2">
            <p className="text-xs font-medium">
              {t("common.folder.executables", { count: executables.length })}
              <span className="ml-1 font-normal text-muted-foreground">
                {t("common.folder.executablesHint")}
              </span>
            </p>
            {executables.length === 0 && (
              <p className="text-xs text-muted-foreground">{t("common.folder.noExecutables")}</p>
            )}
            <ul className="grid max-h-48 gap-0.5 overflow-auto font-mono text-xs">
              {executables.map((f) => (
                <li key={f.path}>
                  <label className="flex items-center gap-2">
                    <input
                      type="checkbox"
                      checked
                      aria-label={t("common.folder.executableOf", { path: f.path })}
                      onChange={() => setMode(f.path, false)}
                    />
                    <span className="min-w-0 break-all">{f.path}</span>
                    <span className="shrink-0 font-sans text-muted-foreground">
                      {t(`common.folder.mode.${f.mode}`, { base: base?.label ?? "" })}
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          </div>

          <details className="rounded-md border p-2">
            <summary className="cursor-pointer text-xs font-medium">
              {t("common.folder.allFiles", { count: files.length })}
            </summary>
            {changes && (
              <label className="mt-2 flex items-center gap-2 text-xs">
                <input
                  type="checkbox"
                  checked={changedOnly}
                  onChange={(e) => setChangedOnly(e.target.checked)}
                />
                {t("common.folder.changedOnly")}
              </label>
            )}
            <p className="mt-1 text-xs text-muted-foreground">{t("common.folder.modeHint")}</p>
            <ul className="mt-1 grid max-h-72 gap-0.5 overflow-auto font-mono text-xs">
              {shown.map(({ path, change }) => {
                const f = filesByPath.get(path);
                return (
                  <li key={path} className="flex items-center gap-2">
                    {f ? (
                      <input
                        type="checkbox"
                        checked={f.executable}
                        aria-label={t("common.folder.executableOf", { path })}
                        onChange={(e) => setMode(path, e.target.checked)}
                      />
                    ) : (
                      <span className="inline-block w-[13px]" />
                    )}
                    {changes && (
                      <span className={cn("w-3 shrink-0", MARK_TONE[byPath.get(path) ?? change])}>
                        {MARK[byPath.get(path) ?? change]}
                      </span>
                    )}
                    <span className={cn("min-w-0 break-all", !f && "line-through text-muted-foreground")}>
                      {path}
                    </span>
                    {f && <span className="shrink-0 text-muted-foreground">{fmtBytes(f.size)}</span>}
                  </li>
                );
              })}
            </ul>
          </details>

          <div className="flex flex-wrap items-center gap-2">
            <Button
              disabled={busy || problems.length > 0 || !!blocked || files.length === 0}
              onClick={() => void submit()}
            >
              {submitLabel}
            </Button>
            <Button type="button" variant="ghost" disabled={busy} onClick={reset}>
              <RotateCcw />
              {t("common.folder.reset")}
            </Button>
            {blocked && !busy && <span className="text-xs text-muted-foreground">{blocked}</span>}
          </div>
        </>
      )}

      {progress && <ProgressLine progress={progress} />}
      <ErrorAlert error={error} />
    </div>
  );
}

function ProgressLine({ progress }: { progress: Progress }) {
  const { text, done, total } = progress;
  const ratio = total ? Math.min(1, (done ?? 0) / total) : null;
  return (
    <div className="grid gap-1 text-xs" role="status" data-testid="folder-progress">
      <span>
        {text}
        {total != null && ` ${done ?? 0}/${total}`}
      </span>
      {ratio != null && (
        <div className="h-1.5 overflow-hidden rounded bg-muted">
          <div className="h-full bg-primary transition-all" style={{ width: `${ratio * 100}%` }} />
        </div>
      )}
    </div>
  );
}
