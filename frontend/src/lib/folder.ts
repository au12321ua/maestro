// A local folder sent to the platform's content store: attachments and uploaded datasets
// share it up to "the contents are in place"; each then checks and records its own way.
// Files are identified by path, SHA-256 and the executable bit. Browsers do not expose a
// file's mode, so the bit is carried over from the version this one is based on, guessed
// for new paths, and shown for correction before anything is sent.
import { api } from "@/lib/api";
import { sha256 } from "@/lib/sha256";

/** A file of the version a new one is based on, as the service lists it. */
export interface BaseFile {
  path: string;
  sha256: string;
  executable: boolean;
}

/** Why a file is marked executable or not before anyone changes it. */
export type ModeSource = "base" | "shebang" | "elf" | "none" | "manual";

/** A hashed local file; its executable bit is decided against a base by `withModes`. */
export interface PickedFile {
  path: string;
  file: File;
  size: number;
  sha256: string;
  guessed: "shebang" | "elf" | "none";
}

export interface ModedFile extends PickedFile {
  executable: boolean;
  mode: ModeSource;
}

/** A file from the folder input or a drop, with its path below the chosen folder. */
export interface LocalFile {
  path: string;
  file: File;
}

/** The chosen folder's name and its files below it, from a folder input. */
export function fromInput(list: FileList | null): { folder: string; files: LocalFile[] } {
  const files: LocalFile[] = [];
  let folder = "";
  for (const file of Array.from(list ?? [])) {
    const parts = (file.webkitRelativePath || file.name).split("/");
    folder ||= parts.length > 1 ? parts[0] : "";
    files.push({ path: parts.length > 1 ? parts.slice(1).join("/") : parts[0], file });
  }
  return { folder, files };
}

/** A dropped folder walked through the entries API (one folder; files are refused). */
export async function fromDrop(
  items: DataTransferItemList,
): Promise<{ folder: string; files: LocalFile[] } | null> {
  const entries = Array.from(items)
    .map((item) => item.webkitGetAsEntry?.())
    .filter((e): e is FileSystemEntry => !!e);
  if (entries.length !== 1 || !entries[0].isDirectory) return null;
  const root = entries[0] as FileSystemDirectoryEntry;
  const files: LocalFile[] = [];
  const walk = async (dir: FileSystemDirectoryEntry, prefix: string): Promise<void> => {
    const reader = dir.createReader();
    // readEntries returns the children in batches until it returns none.
    for (;;) {
      const batch = await new Promise<FileSystemEntry[]>((resolve, reject) =>
        reader.readEntries(resolve, reject),
      );
      if (batch.length === 0) break;
      for (const entry of batch) {
        const path = prefix ? `${prefix}/${entry.name}` : entry.name;
        if (entry.isDirectory) await walk(entry as FileSystemDirectoryEntry, path);
        else {
          const file = await new Promise<File>((resolve, reject) =>
            (entry as FileSystemFileEntry).file(resolve, reject),
          );
          files.push({ path, file });
        }
      }
    }
  };
  await walk(root, "");
  return { folder: root.name, files };
}

async function guess(file: File): Promise<"shebang" | "elf" | "none"> {
  const head = new Uint8Array(await file.slice(0, 4).arrayBuffer());
  if (head[0] === 0x23 && head[1] === 0x21) return "shebang";
  if (head[0] === 0x7f && head[1] === 0x45 && head[2] === 0x4c && head[3] === 0x46) return "elf";
  return "none";
}

/** Hash the files and look at their first bytes, leaving out `.git`. */
export async function readFiles(
  files: LocalFile[],
  onProgress?: (done: number, total: number) => void,
): Promise<PickedFile[]> {
  const kept = files.filter((f) => !f.path.split("/").includes(".git"));
  const picked: PickedFile[] = [];
  for (const [index, { path, file }] of kept.entries()) {
    onProgress?.(index, kept.length);
    picked.push({ path, file, size: file.size, sha256: await sha256(file), guessed: await guess(file) });
  }
  onProgress?.(kept.length, kept.length);
  return picked.sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
}

/**
 * Executable bits: a choice made in the preview wins; otherwise a path the base version
 * also has keeps its bit there, so downloading a version, editing it and uploading it again
 * does not change modes; a new path is executable when it starts with `#!` or is an ELF
 * binary.
 */
export function withModes(
  picked: PickedFile[],
  base: BaseFile[] | undefined,
  chosen: ReadonlyMap<string, boolean>,
): ModedFile[] {
  const before = new Map((base ?? []).map((f) => [f.path, f]));
  return picked.map((f) => {
    const manual = chosen.get(f.path);
    if (manual !== undefined) return { ...f, executable: manual, mode: "manual" };
    const inherited = before.get(f.path);
    if (inherited) return { ...f, executable: inherited.executable, mode: "base" };
    return { ...f, executable: f.guessed !== "none", mode: f.guessed };
  });
}

export type Change = "added" | "removed" | "changed" | "same";

export interface FileChange {
  path: string;
  change: Change;
}

/** Each path compared with the base: content and executable bit both count. */
export function diffFiles(base: BaseFile[], picked: BaseFile[]): FileChange[] {
  const before = new Map(base.map((f) => [f.path, f]));
  const now = new Set(picked.map((f) => f.path));
  const changes: FileChange[] = picked.map((f) => {
    const old = before.get(f.path);
    const change: Change = !old
      ? "added"
      : old.sha256 === f.sha256 && old.executable === f.executable
        ? "same"
        : "changed";
    return { path: f.path, change };
  });
  for (const f of base) if (!now.has(f.path)) changes.push({ path: f.path, change: "removed" });
  return changes.sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
}

export function countChanges(changes: { change: Change }[]): Record<Change, number> {
  const counts = { added: 0, removed: 0, changed: 0, same: 0 };
  for (const c of changes) counts[c.change] += 1;
  return counts;
}

/** Tasks are the top-level folders: a task changes when any of its files does. */
export function diffTasks(changes: FileChange[]): { task: string; change: Change }[] {
  const tasks = new Map<string, Change[]>();
  for (const c of changes) {
    const task = c.path.split("/")[0];
    tasks.set(task, [...(tasks.get(task) ?? []), c.change]);
  }
  return [...tasks].map(([task, list]) => ({
    task,
    change: list.every((c) => c === "added")
      ? "added"
      : list.every((c) => c === "removed")
        ? "removed"
        : list.every((c) => c === "same")
          ? "same"
          : "changed",
  }));
}

/** Ask which contents the service lacks and upload only those. Returns how many went up. */
export async function uploadMissing(
  picked: PickedFile[],
  onProgress?: (done: number, total: number) => void,
): Promise<number> {
  const shas = [...new Set(picked.map((p) => p.sha256))];
  const { missing } = (await api.post("/attachment-contents/missing", { body: { sha256: shas } }))
    .data;
  const contents = new Map(picked.map((p) => [p.sha256, p.file]));
  for (const [index, sha] of missing.entries()) {
    onProgress?.(index, missing.length);
    await api.upload("/attachment-contents/{sha256}", { sha256: sha }, contents.get(sha)!);
  }
  onProgress?.(missing.length, missing.length);
  return missing.length;
}

/** The file list both publishing calls take. */
export function entries(picked: ModedFile[]): BaseFile[] {
  return picked.map(({ path, sha256, executable }) => ({ path, sha256, executable }));
}
