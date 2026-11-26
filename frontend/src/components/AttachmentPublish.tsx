import { useRef, useState } from "react";
import { toast } from "sonner";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { FolderUpload, type Chosen, type FolderBase } from "@/components/FolderUpload";
import { ErrorAlert, SelectField, TextField } from "@/components/common";
import { t } from "@/i18n";
import { api } from "@/lib/api";
import { entries, type LocalFile } from "@/lib/folder";
import { fmtTime, snapshotName } from "@/lib/format";
import { limit, useSession } from "@/lib/hooks";
import type { Attachment } from "@/lib/types";

const EXAMPLE = `{
  "schema_version": 1,
  "name": "my-tools",
  "description": "${t("attachments.publish.exampleDescription")}",
  "effects": ["agent_tools"],
  "compatible_harnesses": ["terminus-2"],
  "provides": { "tools": ["mytool"], "env": [] }
}`;
function folderBase(snapshot: Attachment): FolderBase {
  return { label: `${snapshot.name} · ${snapshotName(snapshot)}`, files: snapshot.files ?? [] };
}

export function AttachmentPublish({ kind, parent, onPublished }: {
  kind: "attachment" | "submission";
  parent?: Attachment;
  onPublished: () => void;
}) {
  const { capabilities } = useSession();
  const [error, setError] = useState<unknown>();
  const [manifestKind, setManifestKind] = useState("attachment");
  const [name, setName] = useState<string | null>(null);
  const [label, setLabel] = useState("");
  const [parentId, setParentId] = useState(parent?.id ?? "");
  const [base, setBase] = useState<FolderBase | undefined>(() => parent && folderBase(parent));
  const [parents, setParents] = useState<Attachment[]>(parent ? [parent] : []);
  const readingParent = useRef(0);

  // The parent determines carried-over executable bits and the preview.
  const chooseParent = async (id: string) => {
    const current = ++readingParent.current;
    setError(undefined);
    setParentId(id);
    setBase(undefined);
    if (!id) return;
    try {
      const { data } = await api.get("/attachments/{attachment_id}", { path: { attachment_id: id } });
      if (current === readingParent.current) setBase(folderBase(data));
    } catch (error) {
      if (current === readingParent.current) setError(error);
    }
  };

  const chooseFolder = async (_folder: string, files: LocalFile[]) => {
    const manifest = files.find((f) => f.path === "manifest.json");
    let found: string | null = null;
    let manifestKind = "attachment";
    if (manifest) {
      try {
        const spec = JSON.parse(await manifest.file.text());
        found = typeof spec?.name === "string" ? spec.name : null;
        manifestKind = spec?.kind ?? "attachment";
      } catch {
        manifestKind = "";
      }
    }
    setManifestKind(manifestKind);
    setName(found);
    // The newest snapshot of the same name is the default parent; one chosen with
    // "base a new snapshot on this" stays when the names match.
    const candidates = found
      ? (await api.get("/attachments", { query: { name: found, kind } })).data
      : [];
    setParents(candidates);
    const parent = candidates.find((item) => item.id === parentId) ?? candidates.at(-1);
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
    setParents([]);
    onPublished();
  };

  return (
    <Card id="attachment-publish" className="scroll-mt-4">
      <CardHeader>
        <CardTitle className="text-base">
          {t("attachments.publish.title")}
        </CardTitle>
        <CardDescription>
          {t("attachments.publish.description")}{" "}
          {t("attachments.kind.manifestHint")}
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-3 text-sm">
        <ErrorAlert error={error} />
        <FolderUpload
          base={base}
          maxFiles={limit(capabilities, "attachment_max_files", 10000)}
          maxBytes={limit(capabilities, "attachment_max_bytes", 1024 ** 3)}
          submitLabel={t("attachments.publish.submit")}
          check={(files) => [
            ...(parentId && !base ? [t("attachments.publish.parentPending")] : []),
            ...(!files.some((f) => f.path === "manifest.json")
              ? [t("attachments.publish.noManifest")]
              : []),
            ...(manifestKind !== kind
              ? [t("attachments.kind.mismatch")]
              : []),
            ...(kind === "submission" &&
            !files.some((f) => f.path === "bootstrap.sh")
              ? [t("attachments.kind.bootstrapRequired")]
              : []),
          ]}
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
                  onChange={(e) =>
                    void chooseParent(e.target.value)
                  }
                >
                  <option value="">
                    {t("attachments.publish.noParent")}
                  </option>
                  {parents
                    .filter((a) => (a.kind ?? "attachment") === kind)
                    .map((s) => (
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
  );
}
