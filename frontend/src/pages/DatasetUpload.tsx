// Uploading converted Harbor task folders as a dataset version (administrators). The folder
// holds one subfolder per task; contents go up through the same channel as attachments, then
// the service validates every task with Harbor and imports the version only if all pass,
// followed by the usual first preparation.
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { ErrorAlert, SelectField, TextField } from "@/components/common";
import { FolderUpload, type Chosen, type FolderBase } from "@/components/FolderUpload";
import { OperationLine } from "@/components/OperationLine";
import { t } from "@/i18n";
import { api } from "@/lib/api";
import { entries, type ModedFile } from "@/lib/folder";
import { fmtTime, versionName } from "@/lib/format";
import { limit, useSession } from "@/lib/hooks";
import type { Dataset, Operation } from "@/lib/types";

const NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;
const NONE = "none";

interface TaskError {
  task_key: string;
  message: string;
}

function taskErrors(op: Operation): TaskError[] {
  const evidence = (op.error as { evidence?: { task_errors?: TaskError[] } } | null)?.evidence;
  return evidence?.task_errors ?? [];
}

/** Files must sit inside a task folder; a file at the top would belong to no task. */
function outsideTasks(files: ModedFile[]): string[] {
  const loose = files.filter((f) => !f.path.includes("/")).map((f) => f.path);
  return loose.length ? [t("datasets.upload.looseFiles", { files: loose.join(", ") })] : [];
}

export function UploadDataset({
  datasets,
  target,
  onClose,
  onImported,
}: {
  datasets: Dataset[];
  /** An upload dataset to add a version to; null for a new dataset. */
  target: Dataset | null;
  onClose: () => void;
  onImported: (versionId?: string) => Promise<void>;
}) {
  const { capabilities } = useSession();
  const [namespace, setNamespace] = useState(target?.namespace ?? "");
  const [name, setName] = useState(target?.name ?? "");
  const [label, setLabel] = useState("");
  // "" until chosen: the dataset's newest version; NONE for no base.
  const [basedOn, setBasedOn] = useState("");
  const [base, setBase] = useState<FolderBase>();
  const [baseError, setBaseError] = useState<unknown>();
  const [operation, setOperation] = useState<string>();
  const [failed, setFailed] = useState<{ op: Operation; tasks: TaskError[] }>();

  const existing = datasets.find((d) => d.namespace === namespace && d.name === name);
  const own =
    existing?.source_kind === "upload"
      ? [...existing.versions].sort((a, b) => (a.created_at < b.created_at ? 1 : -1))
      : [];
  const chosenBase =
    basedOn === NONE ? undefined : (own.find((v) => v.id === basedOn) ?? own[0]);

  useEffect(() => {
    let current = true;
    setBase(undefined);
    setBaseError(undefined);
    if (!chosenBase) return;
    api
      .get("/dataset-versions/{version_id}/files", { path: { version_id: chosenBase.id } })
      .then((res) => {
        if (current)
          setBase({
            label: `${existing!.namespace}/${existing!.name}@${versionName(chosenBase, true)}`,
            files: res.data,
          });
      })
      .catch((err) => current && setBaseError(err));
    return () => {
      current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chosenBase?.id]);

  const blocked = !NAME.test(namespace) || !NAME.test(name)
    ? t("datasets.upload.nameNeeded")
    : existing && existing.source_kind !== "upload"
      ? t("datasets.upload.otherSource", { source: existing.source_kind })
      : undefined;

  const submit = async ({ files }: Chosen) => {
    setFailed(undefined);
    const op = await api.post("/dataset-uploads", {
      body: {
        namespace,
        name,
        files: entries(files),
        label: label.trim() || null,
        based_on: chosenBase?.id ?? null,
      },
    });
    setOperation(op.data.id);
  };

  const done = (op: Operation) => {
    setOperation(undefined);
    const imported = op.kind === "dataset_import" ? op.result : null;
    if (op.status === "succeeded") {
      toast.success(
        imported?.existing ? t("datasets.upload.existing") : t("datasets.upload.added"),
      );
      if (imported?.ignored?.length) toast.info(t("datasets.upload.ignored"));
      void onImported(imported?.dataset_version_id);
      onClose();
    } else {
      setFailed({ op, tasks: taskErrors(op) });
    }
  };

  return (
    <Card className="mb-4" data-testid="dataset-upload">
      <CardHeader>
        <CardTitle className="flex items-center justify-between gap-2 text-base">
          {target
            ? t("datasets.upload.newVersionTitle", { name: `${target.namespace}/${target.name}` })
            : t("datasets.upload.title")}
          <Button size="sm" variant="ghost" onClick={onClose}>
            {t("datasets.collapse")}
          </Button>
        </CardTitle>
        <p className="text-xs text-muted-foreground">{t("datasets.upload.intro")}</p>
      </CardHeader>
      <CardContent className="grid gap-3">
        {!target && (
          <div className="grid gap-3 md:grid-cols-2">
            <TextField
              id="upload-namespace"
              label={t("datasets.upload.namespace")}
              value={namespace}
              onChange={(e) => setNamespace(e.target.value.trim())}
            />
            <TextField
              id="upload-name"
              label={t("datasets.upload.name")}
              value={name}
              onChange={(e) => setName(e.target.value.trim())}
            />
          </div>
        )}
        {!target && existing?.source_kind === "upload" && (
          <p className="text-xs">{t("datasets.upload.addsVersion")}</p>
        )}
        <ErrorAlert error={baseError} />
        {own.length > 0 && (
          <div className="grid gap-3 md:grid-cols-2">
            <TextField
              id="upload-label"
              label={t("datasets.upload.label")}
              value={label}
              placeholder={t("datasets.upload.labelPlaceholder")}
              onChange={(e) => setLabel(e.target.value)}
            />
            <SelectField
              id="upload-based-on"
              label={t("datasets.upload.basedOn")}
              value={chosenBase?.id ?? NONE}
              onChange={(e) => setBasedOn(e.target.value)}
            >
              {own.map((v, i) => (
                <option key={v.id} value={v.id}>
                  {versionName(v, true)} · {fmtTime(v.created_at)}
                  {i === 0 ? t("datasets.upload.newest") : ""}
                </option>
              ))}
              <option value={NONE}>{t("datasets.upload.noBase")}</option>
            </SelectField>
          </div>
        )}
        {own.length === 0 && (
          <TextField
            id="upload-label"
            label={t("datasets.upload.label")}
            value={label}
            placeholder={t("datasets.upload.labelPlaceholder")}
            onChange={(e) => setLabel(e.target.value)}
          />
        )}
        <FolderUpload
          base={base}
          tasks
          maxFiles={limit(capabilities, "dataset_upload_max_files", 10000)}
          maxBytes={limit(capabilities, "dataset_upload_max_bytes", 1024 ** 3)}
          submitLabel={t("datasets.upload.submit")}
          blocked={blocked ?? (operation ? t("datasets.upload.importing") : undefined)}
          check={outsideTasks}
          onSubmit={submit}
        />
        {operation && (
          <OperationLine id={operation} label={t("datasets.upload.operation")} onDone={done} />
        )}
        {failed && (
          <div
            className="grid gap-1 rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-900"
            data-testid="upload-failed"
          >
            <p className="font-medium">
              {failed.tasks.length > 0
                ? t("datasets.upload.tasksInvalid", { count: failed.tasks.length })
                : t("datasets.upload.failed", {
                    message: failed.op.error?.message ?? failed.op.status,
                  })}
            </p>
            {failed.tasks.length > 0 && (
              <ul className="grid gap-0.5 text-xs">
                {failed.tasks.map((e) => (
                  <li key={e.task_key} className="break-words">
                    {t("datasets.upload.taskError", { task: e.task_key, message: e.message })}
                  </li>
                ))}
              </ul>
            )}
            <p className="text-xs">{t("datasets.upload.notRecorded")}</p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
