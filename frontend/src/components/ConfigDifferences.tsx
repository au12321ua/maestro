// What a modified run changed from its source run (04 section 7): one row per changed
// setting, the old value struck through before the new one. Attachments are compared by
// name, so a new snapshot of the same attachment reads "name: label 1 → label 2".
import { useState } from "react";
import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";
import { t } from "@/i18n";
import { snapshotName } from "@/lib/format";

export interface ConfigDifference {
  path: string;
  before: unknown;
  after: unknown;
}

interface AttachmentRef {
  version_id?: string;
  name?: string;
  label?: string | null;
  version?: string | null; // runs accepted before attachment snapshots recorded this
  content_digest?: string;
}

const SHOWN = 12;
const PATH_LABEL: Record<string, string> = t("runs.diff.path", { returnObjects: true });

function pathParts(path: string): string[] {
  return path
    .split("/")
    .slice(1)
    .map((part) => part.replace(/~1/g, "/").replace(/~0/g, "~"));
}

function PathLabel({ path }: { path: string }) {
  const [head, ...rest] = pathParts(path);
  if (head === undefined) return <>{t("runs.diff.whole")}</>;
  return (
    <>
      {PATH_LABEL[head] ?? <code>{head}</code>}
      {rest.map((part, i) => (
        <span key={i}>
          {" › "}
          <code className="break-all">{part}</code>
        </span>
      ))}
    </>
  );
}

function asAttachments(value: unknown): AttachmentRef[] | null {
  if (value == null) return [];
  if (!Array.isArray(value)) return null;
  return value.every((x) => typeof x === "object" && x !== null && "name" in x)
    ? (value as AttachmentRef[])
    : null;
}

function snapshotText(a: AttachmentRef): string {
  return a.label || a.version || snapshotName({ content_digest: a.content_digest ?? "" });
}

function Snapshot({ a }: { a: AttachmentRef }) {
  return (
    <code className="break-all" title={a.content_digest}>
      {snapshotText(a)}
    </code>
  );
}

function AttachmentChanges({ before, after }: { before: AttachmentRef[]; after: AttachmentRef[] }) {
  const old = new Map(before.map((a) => [a.name, a]));
  const kept = new Set(after.map((a) => a.name));
  const lines: React.ReactNode[] = [];
  for (const a of after) {
    const was = old.get(a.name);
    if (!was) {
      lines.push(
        <li key={`+${a.name}`}>
          <span className="font-medium text-emerald-700">{t("runs.diff.added")}</span> {a.name}{" "}
          <Snapshot a={a} />
        </li>,
      );
    } else if (was.content_digest !== a.content_digest) {
      lines.push(
        <li key={`~${a.name}`}>
          {a.name}{" "}
          <span className="text-muted-foreground line-through">
            <Snapshot a={was} />
          </span>
          {" → "}
          <span className="font-medium">
            <Snapshot a={a} />
          </span>
          {was.version_id && a.version_id && (
            <>
              {" "}
              <Link
                className="text-primary underline"
                to={`/attachments?snapshot=${a.version_id}&base=${was.version_id}`}
              >
                {t("runs.diff.fileChanges")}
              </Link>
            </>
          )}
        </li>,
      );
    }
  }
  for (const was of before) {
    if (!kept.has(was.name)) {
      lines.push(
        <li key={`-${was.name}`}>
          <span className="font-medium text-red-700">{t("runs.diff.removed")}</span> {was.name}{" "}
          <Snapshot a={was} />
        </li>,
      );
    }
  }
  if (lines.length === 0) {
    // The same snapshots in another order: the load order is part of the configuration.
    const names = (list: AttachmentRef[]) => list.map((a) => a.name).join(" → ");
    lines.push(
      <li key="order">{t("runs.diff.order", { before: names(before), after: names(after) })}</li>,
    );
  }
  return <ul className="grid gap-0.5">{lines}</ul>;
}

function Value({ value }: { value: unknown }) {
  if (value === null || value === undefined) return <span className="text-muted-foreground">—</span>;
  if (typeof value !== "object") return <>{String(value)}</>;
  const text = JSON.stringify(value);
  return (
    <code className="break-all" title={text.length > 120 ? text : undefined}>
      {text.length > 120 ? `${text.slice(0, 120)}…` : text}
    </code>
  );
}

function Change({ difference }: { difference: ConfigDifference }) {
  const { path, before, after } = difference;
  if (path === "/attachments") {
    const b = asAttachments(before);
    const a = asAttachments(after);
    if (a && b) return <AttachmentChanges before={b} after={a} />;
  }
  return (
    <>
      <span className="text-muted-foreground line-through">
        <Value value={before} />
      </span>
      {" → "}
      <span className="font-medium">
        <Value value={after} />
      </span>
    </>
  );
}

export function ConfigDifferences({ differences }: { differences: ConfigDifference[] }) {
  const [all, setAll] = useState(false);
  if (differences.length === 0) return <>{t("runs.none")}</>;
  const shown = all ? differences : differences.slice(0, SHOWN);
  return (
    <div className="grid gap-1.5 break-normal text-xs">
      <ul className="grid gap-1.5">
        {shown.map((d) => (
          <li key={d.path} className="grid gap-x-3 sm:grid-cols-[11rem_minmax(0,1fr)]">
            <span className="text-muted-foreground">
              <PathLabel path={d.path} />
            </span>
            <div className="min-w-0">
              <Change difference={d} />
            </div>
          </li>
        ))}
      </ul>
      {differences.length > SHOWN && (
        <div>
          <Button size="sm" variant="ghost" className="h-6 px-2 text-xs" onClick={() => setAll(!all)}>
            {all ? t("runs.diff.showLess") : t("runs.diff.showAll", { count: differences.length })}
          </Button>
        </div>
      )}
    </div>
  );
}
