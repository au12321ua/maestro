import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
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
  JsonBlock,
  Loading,
  LongName,
  PageTitle,
  Pill,
  SelectField,
  TextField,
  TextAreaField,
} from "@/components/common";
import { Trans, t } from "@/i18n";
import { ApiError, api } from "@/lib/api";
import {
  CONFIG_BLOCKER,
  SOURCE_LAYER,
  blockerText,
  commonPrefix,
  fmtTime,
  snapshotName,
} from "@/lib/format";
import { limit, useLoad, useSession } from "@/lib/hooks";
import { DIMENSION, SCENARIO } from "@/lib/comparison";
import type {
  BatchPreview,
  ConfigRequest,
  ConfigResolution,
  ExecutionMode,
  PartialRequest,
  ParameterValue,
  RunCreate,
  Task,
  EffectiveTask,
  Dimension,
  Scenario,
  VariableDimension,
  Collection,
  Dataset,
  DatasetVersion,
} from "@/lib/types";
import { ScenarioPicker } from "@/pages/ComparePage";
import { TaskStatusCells } from "@/components/TaskStatusCells";

function newKey() {
  return crypto.randomUUID();
}

const NESTED = new Set(["resources", "timeouts", "parameters", "harness_params"]);

/** The fields of a full request that differ from the plan's: the rest the plan supplies,
 *  and the resolution reports those with the plan as their source. */
function ownFields(full: ConfigRequest, template: PartialRequest): Partial<ConfigRequest> {
  const own: Record<string, unknown> = {};
  const plan = template as Record<string, unknown>;
  for (const [key, value] of Object.entries(full)) {
    if (value === null || value === undefined) continue;
    if (NESTED.has(key)) {
      const planned = (plan[key] ?? {}) as Record<string, unknown>;
      const changed = Object.fromEntries(
        Object.entries(value as Record<string, unknown>).filter(
          ([k, v]) => v !== null && JSON.stringify(v) !== JSON.stringify(planned[k]),
        ),
      );
      if (Object.keys(changed).length > 0) own[key] = changed;
    } else if (JSON.stringify(value) !== JSON.stringify(plan[key])) {
      own[key] = value;
    }
  }
  return own as Partial<ConfigRequest>;
}

/** Drops unset fields, for saving a request as a plan template. */
function setFields(full: ConfigRequest): Partial<ConfigRequest> {
  const found: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(full)) {
    if (value === null || value === undefined) continue;
    if (NESTED.has(key)) {
      const kept = Object.fromEntries(
        Object.entries(value as Record<string, unknown>).filter(([, v]) => v != null),
      );
      if (Object.keys(kept).length > 0) found[key] = kept;
    } else if (!(Array.isArray(value) && value.length === 0)) {
      found[key] = value;
    }
  }
  return found as Partial<ConfigRequest>;
}

interface DraftMember {
  name: string;
  request: Partial<ConfigRequest>;
  label: string;
}

/** Blank means "inherit": the field is sent as null so the resolver takes the layer below. */
// Which collection dimension each override field belongs to (06 section 8).
const OVERRIDE_DIMENSION: Record<string, string> = {
  repetitions: "repetitions",
  agent_sec: "resources",
  verifier_sec: "resources",
  cpus: "resources",
  memory_mb: "resources",
  bootstrap_sec: "attachments",
};

function newestFirst(versions: DatasetVersion[]): DatasetVersion[] {
  return [...versions].sort((a, b) => b.created_at.localeCompare(a.created_at));
}

/** The most recently imported version with a task that can be submitted. */
function defaultVersion(dataset: Dataset): DatasetVersion | undefined {
  return newestFirst(dataset.versions).find((v) => v.submittable_task_count > 0);
}

function numberOrNull(text: string): number | null {
  return text.trim() === "" ? null : Number(text);
}

export function NewRunPage() {
  const { capabilities } = useSession();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  // A modified run starts from that run's request; its differences are recorded.
  const sourceRunId = params.get("from");
  const plans = useLoad(async () => (await api.get("/plans")).data);
  // From a collection: its fixed fields are filled in and locked, and the run joins it.
  const copyCollectionId = params.get("copy_collection");
  const collectionId = copyCollectionId ? null : params.get("collection");
  const loadedCollectionId = copyCollectionId ?? collectionId;
  const collection = useLoad(
    async () =>
      loadedCollectionId
        ? (await api.get("/collections/{collection_id}", { path: { collection_id: loadedCollectionId } }))
            .data
        : undefined,
    [loadedCollectionId],
  );
  const fixedDimensions = new Set(Object.keys(collection.data?.fixed ?? {}));
  const locked = (dimension: string) => !!collectionId && fixedDimensions.has(dimension);
  const datasets = useLoad(
    async () => (await api.get("/datasets", { query: { limit: 200 } })).data,
  );
  const models = useLoad(async () => (await api.get("/model-configs")).data);
  const harnesses = useLoad(async () => (await api.get("/harness-versions")).data);
  const credentials = useLoad(async () => (await api.get("/credentials")).data);
  const attachments = useLoad(
    async () => (await api.get("/attachments", { query: { include_disabled: false } })).data,
  );
  const versions = useMemo(
    () =>
      (datasets.data ?? []).flatMap((d) =>
        d.versions.map((v) => ({ dataset: d, version: v })),
      ),
    [datasets.data],
  );

  // Dataset first, then one of its versions. A version chosen by a plan, a source run
  // or the user decides the dataset; otherwise the dataset's default version is used.
  const [chosenDataset, setChosenDataset] = useState("");
  const [chosenVersion, setChosenVersion] = useState("");
  const versionOwner = versions.find((v) => v.version.id === chosenVersion)?.dataset;
  const dataset =
    versionOwner ??
    datasets.data?.find((d) => d.id === chosenDataset) ??
    datasets.data?.find((d) => defaultVersion(d)) ??
    datasets.data?.[0];
  const datasetVersions = dataset ? newestFirst(dataset.versions) : [];
  const versionId = chosenVersion || (dataset && defaultVersion(dataset)?.id) || "";

  const tasks = useLoad(
    async () =>
      versionId
        ? (
            await api.get("/dataset-versions/{version_id}/tasks", {
              path: { version_id: versionId },
            })
          ).data
        : ([] as Task[]),
    [versionId],
  );
  const taskPrefix = commonPrefix(tasks.data?.map((t) => t.task_key) ?? []);
  const detail = useLoad(
    async () =>
      versionId
        ? (await api.get("/dataset-versions/{version_id}", { path: { version_id: versionId } }))
            .data
        : undefined,
    [versionId],
  );

  const [name, setName] = useState("");
  const [executionMode, setExecutionMode] = useState<ExecutionMode>("agent");
  const [modelId, setModelId] = useState("");
  const [harnessId, setHarnessId] = useState("");
  const [harnessParams, setHarnessParams] = useState<Record<string, string>>(
    {},
  );
  const [mode, setMode] = useState<"all_eligible" | "explicit">("all_eligible");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [overrides, setOverrides] = useState({
    repetitions: "",
    concurrency: "",
    run_deadline_hours: "",
    agent_sec: "",
    verifier_sec: "",
    bootstrap_sec: "",
    cpus: "",
    memory_mb: "",
  });
  // Attachment snapshot ids in load order.
  const [chosenAttachments, setChosenAttachments] = useState<string[]>([]);
  // An earlier model revision, pinned by an imported request until the model changes.
  const [modelRevision, setModelRevision] = useState<number | null>(null);
  const [planId, setPlanId] = useState("");
  const [planName, setPlanName] = useState("");
  const [collectionName, setCollectionName] = useState("");
  const [collectionScenario, setCollectionScenario] = useState<Scenario>("models");
  const [collectionDescription, setCollectionDescription] = useState("");
  const [collectionVariable, setCollectionVariable] = useState<VariableDimension[]>([]);
  const [copiedCollection, setCopiedCollection] = useState<Collection | null>(null);
  const [copyBaseRun, setCopyBaseRun] = useState("");
  const [draft, setDraft] = useState<DraftMember[]>([]);
  const [batchName, setBatchName] = useState("");
  const [batchPreview, setBatchPreview] = useState<BatchPreview | null>(null);
  const batchKey = useRef<string>(newKey());
  const [parameters, setParameters] = useState<
    Record<string, { value: string; secret: boolean }>
  >({});
  const [clientRequestId, setClientRequestId] = useState("");
  const [resolution, setResolution] = useState<ConfigResolution | null>(null);
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(false);
  // One idempotency key per logical submission; kept while the outcome is unknown.
  const key = useRef<string>(newKey());
  const [pendingKey, setPendingKey] = useState<string | null>(null);

  const model =
    (models.data ?? []).find((m) => m.id === modelId) ?? models.data?.[0];
  const compatible = (harnesses.data ?? []).filter(
    (h) => h.enabled && (!model || h.protocols.includes(model.protocol)),
  );
  const harness = compatible.find((h) => h.id === harnessId) ?? compatible[0];
  const eligible = (tasks.data ?? []).filter((t) => t.can_submit);
  const attachmentById = new Map((attachments.data ?? []).map((a) => [a.id, a]));
  const addable = (attachments.data ?? []).filter(
    (a) =>
      !chosenAttachments.includes(a.id) &&
      (!harness ||
        a.compatible_harnesses.includes("*") ||
        a.compatible_harnesses.includes(harness.agent)),
  );
  const installs = chosenAttachments.some(
    (id) => attachmentById.get(id)?.has_bootstrap,
  );
  const moveAttachment = (index: number, step: number) => {
    const next = [...chosenAttachments];
    const [item] = next.splice(index, 1);
    next.splice(index + step, 0, item);
    changed(setChosenAttachments)(next);
  };
  // Author parameters declared by this version's tasks; required ones need a value.
  const declared = useMemo(() => {
    const found = new Map<
      string,
      { sensitive: boolean; required: boolean; default: string | null }
    >();
    for (const task of tasks.data ?? []) {
      for (const p of task.parameters) {
        const known = found.get(p.name);
        found.set(p.name, {
          sensitive: (known?.sensitive ?? false) || p.sensitive,
          required: (known?.required ?? false) || task.required_parameters.includes(p.name),
          default: known?.default ?? p.default,
        });
      }
    }
    return [...found.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [tasks.data]);

  // Any edit makes the shown resolution outdated: it has to be checked again.
  const changed =
    <T,>(set: (value: T) => void) =>
    (value: T) => {
      set(value);
      setResolution(null);
    };
  const toggle = (id: string) => {
    const next = new Set(selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    changed(setSelected)(next);
  };

  const request = (): ConfigRequest => {
    const params: Record<string, ParameterValue> = {};
    for (const [pname, entry] of Object.entries(parameters)) {
      if (entry.value.trim() === "") continue;
      params[pname] = entry.secret
        ? { credential_id: entry.value.trim() }
        : entry.value;
    }
    const typedHarnessParams: Record<string, string | number | boolean> = {};
    for (const [pname, text] of Object.entries(harnessParams)) {
      if (text.trim() === "") continue;
      const spec = harness?.parameter_schema.parameters[pname];
      typedHarnessParams[pname] =
        spec?.type === "integer" || spec?.type === "number"
          ? Number(text)
          : spec?.type === "boolean"
            ? text === "true"
            : text;
    }
    return {
      dataset_version_id: versionId,
      execution_mode: executionMode,
      task_selection:
        mode === "all_eligible"
          ? { mode }
          : { mode, task_ids: [...selected].sort() },
      ...(executionMode === "agent"
        ? {
            model_config_id: model?.id ?? null,
            model_revision: modelRevision,
            harness_version_id: harness?.id ?? null,
            harness_params: typedHarnessParams,
            attachments: chosenAttachments,
          }
        : {}),
      repetitions: numberOrNull(overrides.repetitions),
      concurrency: numberOrNull(overrides.concurrency),
      run_deadline_hours: numberOrNull(overrides.run_deadline_hours),
      resources: {
        cpus: numberOrNull(overrides.cpus),
        memory_mb: numberOrNull(overrides.memory_mb),
      },
      timeouts: {
        agent_sec: numberOrNull(overrides.agent_sec),
        verifier_sec: numberOrNull(overrides.verifier_sec),
        bootstrap_sec:
          executionMode === "agent" ? numberOrNull(overrides.bootstrap_sec) : null,
      },
      parameters: params,
    };
  };

  const plan = (plans.data ?? []).find((p) => p.id === planId);
  /** What is sent: with a plan, only the fields that differ from it. */
  const outgoing = (): Partial<ConfigRequest> => ({
    ...(plan
      ? { ...ownFields(request(), plan.template), plan_id: plan.id, plan_revision: plan.revision }
      : request()),
    ...(collectionId ? { collection_id: collectionId } : {}),
  });

  /** Fill the form from a (partial) request: a plan's template or a run's request. */
  const applyRequest = (req: PartialRequest) => {
    if (req.dataset_version_id) setChosenVersion(req.dataset_version_id);
    if (req.execution_mode) setExecutionMode(req.execution_mode);
    if (req.model_config_id) setModelId(req.model_config_id);
    setModelRevision(req.model_revision ?? null);
    if (req.harness_version_id) setHarnessId(req.harness_version_id);
    if (req.harness_params)
      setHarnessParams(
        Object.fromEntries(Object.entries(req.harness_params).map(([k, v]) => [k, String(v)])),
      );
    if (req.attachments) setChosenAttachments(req.attachments);
    if (req.task_selection) {
      setMode(req.task_selection.mode ?? "all_eligible");
      setSelected(new Set(req.task_selection.task_ids ?? []));
    }
    const text = (v: number | null | undefined) => (v == null ? "" : String(v));
    setOverrides({
      repetitions: text(req.repetitions),
      concurrency: text(req.concurrency),
      run_deadline_hours: text(req.run_deadline_hours),
      agent_sec: text(req.timeouts?.agent_sec),
      verifier_sec: text(req.timeouts?.verifier_sec),
      bootstrap_sec: text(req.timeouts?.bootstrap_sec),
      cpus: text(req.resources?.cpus),
      memory_mb: text(req.resources?.memory_mb),
    });
    if (req.parameters)
      setParameters(
        Object.fromEntries(
          Object.entries(req.parameters).map(([k, v]) =>
            typeof v === "string"
              ? [k, { value: v, secret: false }]
              : [k, { value: v.credential_id, secret: true }],
          ),
        ),
      );
    setResolution(null);
  };

  // "Use this plan" from the plans page.
  const wanted = params.get("plan");
  const planApplied = useRef(false);
  useEffect(() => {
    const chosen = (plans.data ?? []).find((p) => p.id === wanted);
    if (!chosen || planApplied.current) return;
    planApplied.current = true;
    setPlanId(chosen.id);
    applyRequest(chosen.template);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wanted, plans.data]);

  const collectionApplied = useRef(false);
  useEffect(() => {
    if (!collection.data || collectionApplied.current) return;
    collectionApplied.current = true;
    applyRequest(collection.data.template);
    if (copyCollectionId) {
      setCollectionName(t("newRun.copy.copyName", { name: collection.data.name }));
      setCollectionDescription(collection.data.description);
      setCollectionScenario(collection.data.scenario);
      setCollectionVariable(collection.data.variable_dimensions);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [collection.data]);

  const imported = useRef(false);
  useEffect(() => {
    if (!sourceRunId || imported.current) return;
    imported.current = true;
    void (async () => {
      try {
        const [req, run] = await Promise.all([
          api.get("/runs/{run_id}/request", { path: { run_id: sourceRunId } }),
          api.get("/runs/{run_id}", { path: { run_id: sourceRunId } }),
        ]);
        applyRequest(req.data);
        setName(t("newRun.modifiedName", { name: run.data.name }));
      } catch (err) {
        setError(err);
      }
    })();
    // applyRequest only sets state; it is safe to leave out of the dependencies.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sourceRunId]);

  const savePlan = async () => {
    setError(undefined);
    try {
      const res = await api.post("/plans", {
        body: {
          name: planName.trim(),
          template: setFields(request()),
        },
      });
      toast.success(t("newRun.plan.saved", { name: res.data.name }));
      setPlanName("");
      await plans.reload();
    } catch (err) {
      setError(err);
    }
  };

  const saveCollection = async () => {
    setError(undefined);
    try {
      const res = await api.post("/collections", {
        body: {
          name: collectionName.trim(),
          scenario: collectionScenario,
          base: { request: setFields(request()) },
        },
      });
      toast.success(t("newRun.collection.created", { name: res.data.name }));
      navigate(`/collections/${res.data.id}`);
    } catch (err) {
      setError(err);
    }
  };

  const copyCollection = async (event: FormEvent) => {
    event.preventDefault();
    if (!copyCollectionId || busy) return;
    setBusy(true);
    setError(undefined);
    try {
      const res = await api.post("/collections/{collection_id}/copy", {
        path: { collection_id: copyCollectionId },
        body: {
          name: collectionName.trim(),
          description: collectionDescription,
          scenario: collectionScenario,
          variable_dimensions: collectionScenario === "custom" ? collectionVariable : null,
          base: { request: setFields(request()) },
        },
      });
      if (res.data.not_carried?.length) setCopiedCollection(res.data);
      else navigate(`/collections/${res.data.id}`);
    } catch (err) {
      setError(err);
    } finally {
      setBusy(false);
    }
  };

  const addToDraft = () => {
    const label = [
      executionMode === "agent" ? (model?.name ?? t("newRun.draft.modelFallback")) : "Oracle",
      harness ? `${harness.name} ${harness.version}` : null,
      chosenAttachments.length
        ? t("newRun.draft.attachmentCount", { count: chosenAttachments.length })
        : null,
    ]
      .filter(Boolean)
      .join(" · ");
    setDraft([
      ...draft,
      { name: name.trim() || t("newRun.draft.memberName", { n: draft.length + 1 }), request: outgoing(), label },
    ]);
    setBatchPreview(null);
    toast.success(t("newRun.draft.added"));
  };
  const batchBody = () => ({
    name: batchName.trim(),
    runs: draft.map((m) => ({ name: m.name, ...m.request })),
  });
  const previewBatch = async () => {
    setError(undefined);
    try {
      setBatchPreview((await api.post("/run-batches/preview", { body: batchBody() })).data);
    } catch (err) {
      setError(err);
    }
  };
  const submitBatch = async () => {
    setBusy(true);
    setError(undefined);
    try {
      const res = await api.post("/run-batches", {
        body: batchBody(),
        headers: {
          "Idempotency-Key": batchKey.current,
        },
      });
      batchKey.current = newKey();
      toast.success(
        t("newRun.draft.accepted", { runs: res.data.runs.length, trials: res.data.planned_trials }),
      );
      navigate(`/batches/${res.data.batch_id}`);
    } catch (err) {
      setError(err);
      // An unknown outcome keeps the key, so a retry cannot accept the batch twice.
      if (!(err instanceof ApiError && err.indeterminate)) batchKey.current = newKey();
    } finally {
      setBusy(false);
    }
  };

  const check = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(undefined);
    try {
      const res = await api.post("/config-resolutions", { body: outgoing() });
      setResolution(res.data);
    } catch (err) {
      setError(err);
      setResolution(null);
    } finally {
      setBusy(false);
    }
  };

  const submit = async () => {
    if (!resolution) return;
    setBusy(true);
    setError(undefined);
    const body: RunCreate = {
      name,
      resolution_id: resolution.resolution_id,
      client_request_id: clientRequestId || null,
      source_run_id: sourceRunId,
      collection_id: collectionId,
    };
    try {
      const res = await api.post("/runs", {
        body: body,
        headers: {
          "Idempotency-Key": key.current,
        },
      });
      key.current = newKey();
      setPendingKey(null);
      toast.success(t("newRun.submit.accepted", { count: res.data.planned_trials }));
      navigate(`/runs/${res.data.run_id}`);
    } catch (err) {
      setError(err);
      if (err instanceof ApiError && err.indeterminate) {
        setPendingKey(key.current);
      } else {
        key.current = newKey();
        setPendingKey(null);
        if (
          err instanceof ApiError &&
          err.code.startsWith("CONFIG_RESOLUTION_")
        )
          setResolution(null);
      }
    } finally {
      setBusy(false);
    }
  };

  const fieldErr = (field: string) =>
    error instanceof ApiError ? error.field(field) : undefined;
  const override = (
    field: keyof typeof overrides,
    label: string,
    hint: string,
  ) => (
    <TextField
      id={`run-${field}`}
      label={label}
      type="number"
      min={field.endsWith("_sec") ? 0 : 1}
      step={field.endsWith("_sec") ? "any" : 1}
      placeholder={t("newRun.inherit")}
      value={overrides[field]}
      disabled={locked(OVERRIDE_DIMENSION[field] ?? "")}
      onChange={(e) =>
        changed(setOverrides)({ ...overrides, [field]: e.target.value })
      }
      hint={hint}
      error={fieldErr(field)}
    />
  );

  if (copiedCollection) {
    return (
      <div className="grid gap-4">
        <PageTitle title={t("newRun.copied.title")} />
        <p>
          {t("newRun.copied.summary", {
            name: copiedCollection.name,
            count: copiedCollection.run_count,
          })}
        </p>
        <ul className="list-disc pl-5 text-sm">
          {(copiedCollection.not_carried ?? []).map((r) => (
            <li key={r.run_id}>
              {t("newRun.copied.mismatch", {
                name: r.name,
                dimensions: [...new Set(r.mismatches.map((m) => DIMENSION[m.dimension]))].join(
                  t("newRun.enumSeparator"),
                ),
              })}
            </li>
          ))}
        </ul>
        <div>
          <Button asChild>
            <Link to={`/collections/${copiedCollection.id}`}>{t("newRun.copied.open")}</Link>
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div>
      <PageTitle title={copyCollectionId ? t("newRun.copy.title") : t("newRun.title")}>
        <Button asChild variant="outline" size="sm">
          <Link to={copyCollectionId ? `/collections/${copyCollectionId}` : "/runs"}>{t("newRun.backToList")}</Link>
        </Button>
      </PageTitle>
      {datasets.loading && !datasets.data && <Loading />}
      <ErrorAlert error={datasets.error} title={t("newRun.loadDatasetsFailed")} />
      <ErrorAlert error={collection.error} title={t("newRun.loadCollectionFailed")} />
      {datasets.data && versions.length === 0 && (
        <EmptyNote>{t("newRun.noVersions")}</EmptyNote>
      )}
      {versions.length > 0 && (
        <form className="grid gap-4" onSubmit={copyCollectionId ? copyCollection : check}>
          {copyCollectionId && (
            <Card>
              <CardHeader>
                <CardTitle className="text-base">{t("newRun.copy.spec")}</CardTitle>
              </CardHeader>
              <CardContent className="grid gap-3">
                <p className="text-sm text-muted-foreground">
                  {t("newRun.copy.intro")}
                </p>
                <TextField
                  id="copy-name"
                  label={t("newRun.copy.name")}
                  value={collectionName}
                  onChange={(e) => setCollectionName(e.target.value)}
                  required
                />
                <TextAreaField
                  id="copy-description"
                  label={t("newRun.copy.description")}
                  value={collectionDescription}
                  onChange={(e) => setCollectionDescription(e.target.value)}
                />
                <ScenarioPicker
                  scenario={collectionScenario}
                  variable={collectionVariable}
                  onChange={(s, v) => {
                    setCollectionScenario(s);
                    setCollectionVariable(v);
                  }}
                />
                <SelectField
                  id="copy-base-run"
                  label={t("newRun.copy.baseRun")}
                  value={copyBaseRun}
                  disabled={busy}
                  onChange={async (event) => {
                    const runId = event.target.value;
                    setCopyBaseRun(runId);
                    setBusy(true);
                    setError(undefined);
                    try {
                      if (runId) {
                        const req = await api.get("/runs/{run_id}/request", { path: { run_id: runId } });
                        applyRequest(req.data);
                      } else if (collection.data) applyRequest(collection.data.template);
                    } catch (err) {
                      setError(err);
                    } finally {
                      setBusy(false);
                    }
                  }}
                >
                  <option value="">{t("newRun.copy.baseFixed")}</option>
                  {(collection.data?.members ?? []).filter((m) => !m.deletion).map((m) => (
                    <option key={m.run_id} value={m.run_id}>{m.label ?? m.run_name}</option>
                  ))}
                </SelectField>
              </CardContent>
            </Card>
          )}
          {collectionId && (
            <div className="rounded-md border border-sky-300 bg-sky-50 p-3 text-sm text-sky-900">
              <Trans
                i18nKey="newRun.fromCollection"
                values={{
                  name: collection.data?.name ?? collectionId,
                  dimensions: [...fixedDimensions]
                    .map((d) => DIMENSION[d as Dimension] ?? d)
                    .join(t("newRun.enumSeparator")),
                }}
                components={{ link: <Link to={`/collections/${collectionId}`} className="underline" /> }}
              />
            </div>
          )}
          {sourceRunId && (
            <div className="rounded-md border border-sky-300 bg-sky-50 p-3 text-sm text-sky-900">
              <Trans
                i18nKey="newRun.fromRun"
                values={{ id: sourceRunId }}
                components={{ link: <Link to={`/runs/${sourceRunId}`} className="underline" /> }}
              />
            </div>
          )}
          {!copyCollectionId && <Card>
            <CardHeader>
              <CardTitle className="text-base">{t("newRun.plan.title")}</CardTitle>
              <CardDescription>
                {t("newRun.plan.description")}
                <Link to="/plans" className="ml-1 underline">
                  {t("newRun.plan.manage")}
                </Link>
              </CardDescription>
            </CardHeader>
            <CardContent className="grid gap-4 md:grid-cols-2">
              <SelectField
                id="run-plan"
                label={t("newRun.plan.choose")}
                value={planId}
                onChange={(e) => {
                  const chosen = (plans.data ?? []).find((p) => p.id === e.target.value);
                  setPlanId(e.target.value);
                  if (chosen) applyRequest(chosen.template);
                  else setResolution(null);
                }}
              >
                <option value="">{t("newRun.plan.none")}</option>
                {(plans.data ?? []).map((p) => (
                  <option key={p.id} value={p.id}>
                    {t("newRun.plan.option", {
                      name: p.name,
                      owner: p.owner_username,
                      revision: p.revision,
                    })}
                  </option>
                ))}
              </SelectField>
              <div className="flex items-end gap-2 md:col-span-2">
                <TextField
                  id="collection-from-form"
                  label={t("newRun.collection.fromForm")}
                  placeholder={t("newRun.collection.namePlaceholder")}
                  value={collectionName}
                  onChange={(e) => setCollectionName(e.target.value)}
                  hint={t("newRun.collection.fromFormHint")}
                />
                <select
                  aria-label={t("newRun.collection.scenario")}
                  className="mb-[1.35rem] h-9 rounded-md border bg-background px-2 text-sm"
                  value={collectionScenario}
                  onChange={(e) => setCollectionScenario(e.target.value as Scenario)}
                >
                  {Object.entries(SCENARIO)
                    .filter(([, s]) => s.variable)
                    .map(([key, s]) => (
                      <option key={key} value={key}>
                        {s.label}
                      </option>
                    ))}
                </select>
                <Button
                  type="button"
                  variant="outline"
                  className="mb-[1.35rem]"
                  disabled={!collectionName.trim() || executionMode !== "agent"}
                  onClick={() => void saveCollection()}
                >
                  {t("newRun.collection.create")}
                </Button>
              </div>
              <div className="flex items-end gap-2">
                <TextField
                  id="plan-name"
                  label={t("newRun.plan.saveAs")}
                  placeholder={t("newRun.plan.namePlaceholder")}
                  value={planName}
                  onChange={(e) => setPlanName(e.target.value)}
                />
                <Button
                  type="button"
                  variant="outline"
                  disabled={!planName.trim()}
                  onClick={() => void savePlan()}
                >
                  {t("newRun.plan.save")}
                </Button>
              </div>
            </CardContent>
          </Card>}

          <Card>
            <CardHeader>
              <CardTitle className="text-base">{t("newRun.basic.title")}</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-4 md:grid-cols-2">
              {!copyCollectionId && <TextField
                id="run-name"
                label={t("newRun.basic.runName")}
                value={name}
                onChange={(e) => setName(e.target.value)}
                error={fieldErr("name")}
                required
              />}
              <SelectField
                id="run-dataset"
                label={t("newRun.basic.dataset")}
                value={dataset?.id ?? ""}
                disabled={!!collectionId}
                onChange={(e) => {
                  // A new dataset is a new experiment: its tasks and parameters start empty.
                  changed(setChosenDataset)(e.target.value);
                  setChosenVersion("");
                  setSelected(new Set());
                  setParameters({});
                }}
              >
                {datasets.data?.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.namespace}/{d.name}
                  </option>
                ))}
              </SelectField>
              <SelectField
                id="run-version"
                label={t("newRun.basic.version")}
                value={versionId}
                disabled={!!collectionId}
                onChange={(e) => {
                  // Another version may change tasks and parameters: choose them again.
                  changed(setChosenVersion)(e.target.value);
                  setSelected(new Set());
                  setParameters({});
                }}
                error={fieldErr("dataset_version_id")}
                hint={
                  datasetVersions.length > 0 && !defaultVersion(dataset!)
                    ? t("newRun.basic.noSubmittableVersion")
                    : undefined
                }
              >
                {!versionOwner && chosenVersion && (
                  <option value={chosenVersion}>
                    {t("newRun.basic.unavailableVersion", { id: chosenVersion })}
                  </option>
                )}
                {!versionId && <option value="">{t("newRun.basic.noVersionOption")}</option>}
                {datasetVersions.map((v) => (
                  <option
                    key={v.id}
                    value={v.id}
                    disabled={v.submittable_task_count === 0 && v.id !== versionId}
                  >
                    {t("newRun.basic.versionOption", {
                      ref: v.upstream_ref,
                      time: fmtTime(v.created_at),
                      submittable: v.submittable_task_count,
                      total: v.task_count,
                    })}
                    {v.submittable_task_count === 0 ? t("newRun.basic.versionNoTasks") : ""}
                  </option>
                ))}
              </SelectField>
              <fieldset className="grid gap-1 text-sm md:col-span-2">
                <legend className="mb-1">{t("newRun.basic.executionMode")}</legend>
                <label className="flex items-center gap-2">
                  <input
                    type="radio"
                    name="execution-mode"
                    disabled={!!loadedCollectionId}
                    checked={executionMode === "agent"}
                    onChange={() => changed(setExecutionMode)("agent")}
                  />
                  {t("newRun.basic.agentMode")}
                </label>
                <label className="flex items-center gap-2">
                  <input
                    type="radio"
                    name="execution-mode"
                    disabled={!!loadedCollectionId}
                    checked={executionMode === "oracle"}
                    onChange={() => changed(setExecutionMode)("oracle")}
                  />
                  {t("newRun.basic.oracleMode")}
                </label>
              </fieldset>
              {!copyCollectionId && <TextField
                id="run-client-request"
                label={t("newRun.basic.clientRequestId")}
                value={clientRequestId}
                onChange={(e) => setClientRequestId(e.target.value)}
                error={fieldErr("client_request_id")}
                hint={t("newRun.basic.clientRequestIdHint")}
              />}
            </CardContent>
          </Card>

          {executionMode === "agent" && (
            <Card>
              <CardHeader>
                <CardTitle className="text-base">{t("newRun.model.title")}</CardTitle>
                <CardDescription>{t("newRun.model.description")}</CardDescription>
              </CardHeader>
              <CardContent className="grid gap-4 md:grid-cols-2">
                {models.data && models.data.length === 0 && (
                  <EmptyNote>{t("newRun.model.noModels")}</EmptyNote>
                )}
                {models.data && models.data.length > 0 && (
                  <SelectField
                    id="run-model"
                    label={t("newRun.model.model")}
                    value={model?.id ?? ""}
                    disabled={locked("model")}
                    onChange={(e) => {
                      changed(setModelId)(e.target.value);
                      setModelRevision(null);
                    }}
                    error={fieldErr("model_config_id")}
                  >
                    {models.data.map((m) => (
                      <option key={m.id} value={m.id}>
                        {t("newRun.model.option", {
                          name: m.name,
                          modelId: m.model_id,
                          provider: m.provider,
                          revision: m.revision,
                        })}
                        {m.credential_problem ? t("newRun.model.credentialProblem") : ""}
                      </option>
                    ))}
                  </SelectField>
                )}
                {modelRevision != null && model && modelRevision !== model.revision && (
                  <p className="text-xs text-amber-800 md:col-span-2">
                    {t("newRun.model.pinned", { pinned: modelRevision, current: model.revision })}
                    <button
                      type="button"
                      className="ml-1 underline"
                      onClick={() => changed(setModelRevision)(null)}
                    >
                      {t("newRun.model.useCurrent")}
                    </button>
                  </p>
                )}
                {model && compatible.length === 0 && (
                  <EmptyNote>{t("newRun.model.noHarness")}</EmptyNote>
                )}
                {compatible.length > 0 && (
                  <SelectField
                    id="run-harness"
                    label="Harness"
                    value={harness?.id ?? ""}
                    disabled={locked("harness")}
                    onChange={(e) => {
                      changed(setHarnessId)(e.target.value);
                      setHarnessParams({});
                    }}
                    error={fieldErr("harness_version_id")}
                  >
                    {compatible.map((h) => (
                      <option key={h.id} value={h.id}>
                        {t("newRun.model.harnessOption", { name: h.name, version: h.version, agent: h.agent })}
                      </option>
                    ))}
                  </SelectField>
                )}
                {harness &&
                  Object.entries(harness.parameter_schema.parameters).map(([pname, spec]) => (
                    <TextField
                      key={pname}
                      id={`harness-param-${pname}`}
                      label={t("newRun.model.harnessParam", { name: pname, type: spec.type })}
                      placeholder={
                        spec.default !== undefined
                          ? t("newRun.defaultValue", { value: String(spec.default) })
                          : t("newRun.model.unset")
                      }
                      value={harnessParams[pname] ?? ""}
                      onChange={(e) =>
                        changed(setHarnessParams)({
                          ...harnessParams,
                          [pname]: e.target.value,
                        })
                      }
                      hint={spec.description ?? undefined}
                    />
                  ))}
                <div className="grid gap-2 md:col-span-2">
                  <p className="text-sm font-medium">{t("newRun.attachments.title")}</p>
                  <p className="text-xs text-muted-foreground">
                    <Trans
                      i18nKey="newRun.attachments.intro"
                      components={{ link: <Link to="/attachments" className="mx-1 underline" /> }}
                    />
                  </p>
                  {chosenAttachments.length > 0 && (
                    <ol className="grid gap-1 text-sm">
                      {chosenAttachments.map((id, index) => {
                        const a = attachmentById.get(id);
                        return (
                          <li key={id} className="flex flex-wrap items-center gap-2">
                            <span className="w-5 text-right text-xs text-muted-foreground">
                              {index + 1}.
                            </span>
                            <span className="font-medium">{a?.name ?? id}</span>
                            <span className="font-mono text-xs">{a && snapshotName(a)}</span>
                            {a?.has_bootstrap && <Pill tone="info">{t("newRun.attachments.bootstrap")}</Pill>}
                            <Button
                              type="button"
                              size="sm"
                              variant="ghost"
                              disabled={index === 0}
                              onClick={() => moveAttachment(index, -1)}
                            >
                              {t("newRun.attachments.moveUp")}
                            </Button>
                            <Button
                              type="button"
                              size="sm"
                              variant="ghost"
                              disabled={index === chosenAttachments.length - 1}
                              onClick={() => moveAttachment(index, 1)}
                            >
                              {t("newRun.attachments.moveDown")}
                            </Button>
                            <Button
                              type="button"
                              size="sm"
                              variant="ghost"
                              onClick={() =>
                                changed(setChosenAttachments)(
                                  chosenAttachments.filter((x) => x !== id),
                                )
                              }
                            >
                              {t("newRun.attachments.remove")}
                            </Button>
                          </li>
                        );
                      })}
                    </ol>
                  )}
                  {addable.length > 0 && (
                    <SelectField
                      id="run-attachment-add"
                      label={t("newRun.attachments.add")}
                      value=""
                      onChange={(e) =>
                        e.target.value &&
                        changed(setChosenAttachments)([...chosenAttachments, e.target.value])
                      }
                      error={fieldErr("attachments")}
                    >
                      <option value="">{t("newRun.attachments.choose")}</option>
                      {addable.map((a) => (
                        <option key={a.id} value={a.id}>
                          {t("newRun.attachments.option", {
                            name: a.name,
                            snapshot: snapshotName(a),
                            owner: a.owner_username ?? "—",
                          })}
                        </option>
                      ))}
                    </SelectField>
                  )}
                  {installs &&
                    override(
                      "bootstrap_sec",
                      t("newRun.attachments.bootstrapTimeout"),
                      t("newRun.scale.teamDefault", {
                        default: limit(capabilities, "bootstrap_timeout_sec", 600),
                        max: limit(capabilities, "bootstrap_timeout_sec_max", 3600),
                      }),
                    )}
                </div>
              </CardContent>
            </Card>
          )}

          <Card>
            <CardHeader>
              <CardTitle className="text-base">{t("newRun.scale.title")}</CardTitle>
              <CardDescription>
                {t("newRun.scale.description")}
              </CardDescription>
            </CardHeader>
            <CardContent className="grid gap-4 md:grid-cols-3">
              {override(
                "repetitions",
                t("newRun.scale.repetitions"),
                t("newRun.scale.teamDefault", {
                  default: limit(capabilities, "repetitions_default", 1),
                  max: limit(capabilities, "repetitions_max", 20),
                }),
              )}
              {override(
                "concurrency",
                t("newRun.scale.concurrency"),
                t("newRun.scale.teamDefault", {
                  default: limit(capabilities, "run_default_concurrency", 4),
                  max: limit(capabilities, "run_max_concurrency", 10),
                }),
              )}
              {override(
                "run_deadline_hours",
                t("newRun.scale.runDeadline"),
                t("newRun.scale.teamDefault", {
                  default: limit(capabilities, "run_deadline_hours", 72),
                  max: limit(capabilities, "run_deadline_hours_max", 72),
                }),
              )}
              {override("agent_sec", t("newRun.scale.agentTimeout"), t("newRun.scale.authorTimeoutHint"))}
              {override("verifier_sec", t("newRun.scale.verifierTimeout"), t("newRun.scale.authorTimeoutHint"))}
              {override(
                "cpus",
                t("newRun.scale.cpus"),
                t("newRun.scale.resourceHint"),
              )}
              {override(
                "memory_mb",
                t("newRun.scale.memory"),
                t("newRun.scale.resourceHint"),
              )}
            </CardContent>
          </Card>

          {declared.length > 0 && (
            <Card>
              <CardHeader>
                <CardTitle className="text-base">{t("newRun.params.title")}</CardTitle>
                <CardDescription>
                  {t("newRun.params.description")}
                </CardDescription>
              </CardHeader>
              <CardContent className="grid gap-4 md:grid-cols-2">
                {declared.map(([pname, info]) =>
                  info.sensitive ? (
                    <SelectField
                      key={pname}
                      id={`param-${pname}`}
                      label={`${pname}${info.required ? t("newRun.params.required") : ""}${t("newRun.params.secret")}`}
                      value={parameters[pname]?.value ?? ""}
                      onChange={(e) =>
                        changed(setParameters)({
                          ...parameters,
                          [pname]: { value: e.target.value, secret: true },
                        })
                      }
                      error={fieldErr(pname)}
                    >
                      <option value="">
                        {info.default != null
                          ? t("newRun.params.useDefault")
                          : t("newRun.params.chooseCredential")}
                      </option>
                      {(credentials.data ?? [])
                        .filter((c) => c.kind === "generic_secret")
                        .map((c) => (
                          <option key={c.id} value={c.id}>
                            {t("newRun.params.credentialOption", { name: c.name, provider: c.provider })}
                          </option>
                        ))}
                    </SelectField>
                  ) : (
                    <TextField
                      key={pname}
                      id={`param-${pname}`}
                      label={`${pname}${info.required ? t("newRun.params.required") : ""}`}
                      placeholder={info.default != null ? t("newRun.defaultValue", { value: info.default }) : ""}
                      value={parameters[pname]?.value ?? ""}
                      onChange={(e) =>
                        changed(setParameters)({
                          ...parameters,
                          [pname]: { value: e.target.value, secret: false },
                        })
                      }
                      error={fieldErr(pname)}
                    />
                  ),
                )}
              </CardContent>
            </Card>
          )}

          <Card>
            <CardHeader>
              <CardTitle className="text-base">{t("newRun.tasks.title")}</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-3">
              {detail.data && (
                <p className="text-sm text-muted-foreground">
                  {t("newRun.tasks.summary", {
                    total: detail.data.task_count,
                    admitted: detail.data.admitted_task_count,
                    submittable: detail.data.submittable_task_count,
                  })}
                </p>
              )}
              <div className="flex flex-wrap gap-4 text-sm">
                <label className="flex items-center gap-2">
                  <input
                    type="radio"
                    name="mode"
                    checked={mode === "all_eligible"}
                    onChange={() => changed(setMode)("all_eligible")}
                  />
                  {t("newRun.tasks.allEligible", { count: eligible.length })}
                </label>
                <label className="flex items-center gap-2">
                  <input
                    type="radio"
                    name="mode"
                    checked={mode === "explicit"}
                    onChange={() => changed(setMode)("explicit")}
                  />
                  {t("newRun.tasks.explicit", { count: selected.size })}
                </label>
              </div>
              <ErrorAlert error={tasks.error} title={t("newRun.tasks.loadFailed")} />
              {tasks.loading && !tasks.data && <Loading />}
              {tasks.data && (
                <div className="max-h-96 overflow-auto rounded-md border">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        {mode === "explicit" && <TableHead className="w-10" />}
                        <TableHead>{t("newRun.tasks.task")}</TableHead>
                        <TableHead>{t("newRun.tasks.admission")}</TableHead>
                        <TableHead>{t("newRun.tasks.content")}</TableHead>
                        <TableHead>{t("newRun.tasks.cache")}</TableHead>
                        <TableHead>{t("newRun.tasks.submittable")}</TableHead>
                        <TableHead>{t("newRun.tasks.notes")}</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {tasks.data.map((task) => (
                        <TableRow key={task.id}>
                          {mode === "explicit" && (
                            <TableCell>
                              <input
                                type="checkbox"
                                aria-label={t("newRun.tasks.select", { key: task.task_key })}
                                disabled={!task.can_submit}
                                checked={selected.has(task.id)}
                                onChange={() => toggle(task.id)}
                              />
                            </TableCell>
                          )}
                          <TableCell className="max-w-[24rem] font-mono text-xs">
                            <LongName value={task.task_key} prefix={taskPrefix} />
                            {task.required_parameters.length > 0 && (
                              <div className="text-[11px] text-muted-foreground">
                                {t("newRun.tasks.needsParams", {
                                  names: task.required_parameters.join(t("newRun.enumSeparator")),
                                })}
                              </div>
                            )}
                          </TableCell>
                          <TaskStatusCells task={task} />
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              )}
            </CardContent>
          </Card>

          <ErrorAlert error={error} title={
              copyCollectionId
                ? t("newRun.copy.failed")
                : resolution
                  ? t("newRun.submit.submitFailed")
                  : t("newRun.submit.checkFailed")
            } />
          {copyCollectionId ? <div>
            <Button type="submit" disabled={busy || !collection.data || !collectionName.trim() || !versionId || executionMode !== "agent"}>
              {busy ? t("newRun.copy.busy") : t("newRun.copy.submit")}
            </Button>
          </div> : <div>
            <Button
              type="submit"
              variant={resolution ? "outline" : "default"}
              disabled={busy || !versionId || (mode === "explicit" && selected.size === 0)}
            >
              {busy && !resolution
                ? t("newRun.submit.checking")
                : resolution
                  ? t("newRun.submit.recheck")
                  : t("newRun.submit.check")}
            </Button>{" "}
            <Button
              type="button"
              variant="outline"
              disabled={busy || !versionId || sourceRunId != null}
              title={sourceRunId ? t("newRun.submit.separateHint") : undefined}
              onClick={addToDraft}
            >
              {t("newRun.submit.addToDraft")}
            </Button>
          </div>}

          {resolution && (
            <ResolutionReview
              resolution={resolution}
              busy={busy}
              pendingKey={pendingKey}
              canSubmit={name.trim() !== ""}
              onSubmit={() => void submit()}
            />
          )}

          {draft.length > 0 && (
            <Card>
              <CardHeader>
                <CardTitle className="text-base">
                  {t("newRun.draft.title", { count: draft.length })}
                </CardTitle>
                <CardDescription>
                  {t("newRun.draft.description")}
                </CardDescription>
              </CardHeader>
              <CardContent className="grid gap-3 text-sm">
                <ol className="grid gap-1">
                  {draft.map((m, i) => {
                    const found = batchPreview?.runs[i];
                    return (
                      <li key={i} className="flex flex-wrap items-center gap-2">
                        <span className="w-5 text-right text-xs text-muted-foreground">
                          {i + 1}.
                        </span>
                        <span className="font-medium">{m.name}</span>
                        <span className="text-xs text-muted-foreground">{m.label}</span>
                        {found && (
                          <span className="text-xs">
                            {found.blockers && found.blockers.length > 0 ? (
                              <span className="text-destructive">
                                {found.blockers
                                  .map((b) => CONFIG_BLOCKER[b.code] ?? b.message)
                                  .join(t("common.format.listSeparator"))}
                              </span>
                            ) : (
                              t("newRun.draft.trials", { count: found.planned_trials })
                            )}
                          </span>
                        )}
                        <Button
                          type="button"
                          size="sm"
                          variant="ghost"
                          onClick={() => {
                            setDraft(draft.filter((_, j) => j !== i));
                            setBatchPreview(null);
                          }}
                        >
                          {t("newRun.draft.remove")}
                        </Button>
                      </li>
                    );
                  })}
                </ol>
                {batchPreview && (
                  <p className={batchPreview.can_submit ? "text-emerald-800" : "text-destructive"}>
                    {t("newRun.draft.totalTrials", { count: batchPreview.planned_trials })}
                    {batchPreview.pat_quota &&
                      t("newRun.draft.quota", {
                        needed: batchPreview.pat_quota.needed,
                        left: batchPreview.pat_quota.left,
                      })}
                    {batchPreview.duplicate_configs.length > 0 && t("newRun.draft.duplicates")}
                    {batchPreview.can_submit
                      ? t("newRun.draft.canSubmit")
                      : t("newRun.draft.cannotSubmit")}
                  </p>
                )}
                <div className="flex flex-wrap items-end gap-2">
                  <TextField
                    id="batch-name"
                    label={t("newRun.draft.name")}
                    value={batchName}
                    onChange={(e) => setBatchName(e.target.value)}
                  />
                  <Button type="button" variant="outline" onClick={() => void previewBatch()}>
                    {t("newRun.draft.preview")}
                  </Button>
                  <Button
                    type="button"
                    disabled={busy || !batchName.trim() || !batchPreview?.can_submit}
                    onClick={() => void submitBatch()}
                  >
                    {t("newRun.draft.submit")}
                  </Button>
                </div>
              </CardContent>
            </Card>
          )}
        </form>
      )}
    </div>
  );
}

const FIELD_LABEL: Record<string, string> = {
  "/repetitions": t("newRun.scale.repetitions"),
  "/concurrency": t("newRun.scale.concurrency"),
  "/run_deadline_hours": t("newRun.field.runDeadline"),
  "/resources/cpus": t("newRun.scale.cpus"),
  "/resources/memory_mb": t("newRun.scale.memory"),
  "/timeouts/agent_sec": t("newRun.scale.agentTimeout"),
  "/timeouts/verifier_sec": t("newRun.scale.verifierTimeout"),
  "/model": t("newRun.model.model"),
  "/harness": "Harness",
  "/attachments": t("newRun.field.attachments"),
  "/timeouts/bootstrap_sec": t("newRun.field.bootstrapTimeout"),
};

const TASK_FIELD: Record<string, keyof EffectiveTask> = {
  "/resources/cpus": "cpus",
  "/resources/memory_mb": "memory_mb",
  "/timeouts/agent_sec": "agent_timeout_sec",
  "/timeouts/verifier_sec": "verifier_timeout_sec",
};

function ResolutionReview({
  resolution,
  busy,
  pendingKey,
  canSubmit,
  onSubmit,
}: {
  resolution: ConfigResolution;
  busy: boolean;
  pendingKey: string | null;
  canSubmit: boolean;
  onSubmit: () => void;
}) {
  const effective = resolution.effective_config;
  const value = (path: string): string => {
    if (!effective) return "—";
    if (path === "/model") return resolution.labels.model ?? "—";
    if (path === "/harness") return resolution.labels.harness ?? "—";
    if (path === "/attachments") return resolution.labels.attachments || "—";
    if (path === "/timeouts/bootstrap_sec")
      return String(effective.bootstrap_timeout_sec ?? "—");
    const taskField = TASK_FIELD[path];
    if (taskField) {
      // Per-task values: one value when all tasks agree, otherwise the distinct ones.
      const values = new Set(
        effective.tasks.map((task) => {
          const v = task[taskField];
          return v == null ? t("newRun.resolution.executorDefault") : String(v);
        }),
      );
      return [...values].join(" / ") || "—";
    }
    let node: unknown = effective;
    for (const part of path.split("/").filter(Boolean)) {
      node = (node as Record<string, unknown> | undefined)?.[part];
    }
    if (node && typeof node === "object")
      return "value" in node ? String(node.value) : t("newRun.resolution.credential");
    return node == null ? "—" : String(node);
  };
  const blocked = resolution.blockers.length > 0;

  return (
    <Card className={blocked ? "border-destructive" : "border-emerald-400"}>
      <CardHeader>
        <CardTitle className="text-base">
          {blocked
            ? t("newRun.resolution.blocked")
            : t("newRun.resolution.passed", { count: resolution.planned_trials })}
        </CardTitle>
        <CardDescription>
          {t("newRun.resolution.validUntil", { time: fmtTime(resolution.expires_at) })}
        </CardDescription>
      </CardHeader>
      <CardContent className="grid gap-4 text-sm">
        {blocked && (
          <ul className="grid gap-1">
            {resolution.blockers.map((b, i) => (
              <li key={`${b.path}-${b.code}-${i}`} className="text-destructive">
                <Trans
                  i18nKey="newRun.resolution.blocker"
                  values={{ path: b.path, text: CONFIG_BLOCKER[b.code] ?? b.message, message: b.message }}
                  components={{
                    code: <code className="text-xs" />,
                    note: <span className="text-xs text-muted-foreground" />,
                  }}
                />
              </li>
            ))}
          </ul>
        )}
        {resolution.warnings.map((w, i) => (
          <div
            key={i}
            className="rounded-md border border-amber-300 bg-amber-50 p-2 text-amber-900"
          >
            <Trans
              i18nKey="newRun.resolution.warning"
              values={{ path: w.path, message: w.message }}
              components={{ code: <code className="text-xs" /> }}
            />
          </div>
        ))}
        {effective && (
          <div className="overflow-auto rounded-md border">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t("newRun.resolution.field")}</TableHead>
                  <TableHead>{t("newRun.resolution.value")}</TableHead>
                  <TableHead>{t("newRun.resolution.source")}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {Object.entries(resolution.field_sources).map(
                  ([path, source]) => (
                    <TableRow key={path}>
                      <TableCell>
                        {FIELD_LABEL[path] ?? (
                          <code className="text-xs">{path}</code>
                        )}
                      </TableCell>
                      <TableCell className="font-mono text-xs">
                        {value(path)}
                      </TableCell>
                      <TableCell>
                        <Pill
                          tone={source.layer === "override" ? "warn" : "muted"}
                        >
                          {SOURCE_LAYER[source.layer] ?? source.layer}
                          {source.revision != null
                            ? t("newRun.resolution.revision", { revision: source.revision })
                            : ""}
                        </Pill>
                      </TableCell>
                    </TableRow>
                  ),
                )}
              </TableBody>
            </Table>
          </div>
        )}
        {resolution.baseline_differences.length > 0 && (
          <p className="text-muted-foreground">
            {t("newRun.resolution.baseline")}
            {resolution.baseline_differences
              .map((d) =>
                t("newRun.resolution.baselineItem", {
                  field: FIELD_LABEL[d.path] ?? d.path,
                  value: d.value,
                  lower: d.tasks_lower,
                  higher: d.tasks_higher,
                }),
              )
              .join(t("common.format.listSeparator"))}
          </p>
        )}
        {resolution.excluded_tasks.length > 0 && (
          <div>
            <p className="font-medium">
              {t("newRun.resolution.excluded", { count: resolution.excluded_tasks.length })}
            </p>
            <ul className="mt-1 grid gap-1 text-xs">
              {resolution.excluded_tasks.map((x) => (
                <li key={x.task_id}>
                  <Trans
                    i18nKey="newRun.resolution.excludedTask"
                    values={{
                      key: x.task_key,
                      reasons: x.blockers.map(blockerText).join(t("common.format.listSeparator")),
                    }}
                    components={{ code: <code /> }}
                  />
                </li>
              ))}
            </ul>
          </div>
        )}
        {resolution.required_preparation.length > 0 && (
          <p className="text-muted-foreground">
            {t("newRun.resolution.preparation", { count: resolution.required_preparation.length })}
          </p>
        )}
        <JsonBlock
          value={resolution.effective_config}
          summary={t("newRun.resolution.fullConfig")}
        />
        <p className="font-mono text-xs text-muted-foreground">
          {t("newRun.resolution.digest", { digest: resolution.config_digest })}
        </p>
        {pendingKey && (
          <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-amber-900">
            {t("newRun.resolution.pending")}
            <Pill tone="muted">Idempotency-Key {pendingKey.slice(0, 8)}…</Pill>
          </div>
        )}
        <div>
          <Button
            type="button"
            disabled={busy || blocked || !canSubmit}
            onClick={onSubmit}
          >
            {busy
              ? t("newRun.resolution.submitting")
              : pendingKey
                ? t("newRun.resolution.retry")
                : t("newRun.resolution.submit")}
          </Button>
          {!canSubmit && (
            <span className="ml-2 text-xs text-muted-foreground">
              {t("newRun.resolution.needName")}
            </span>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
